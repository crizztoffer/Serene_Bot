import logging
import json
import aiomysql
import time # Import time for timestamps
from discord.ext import commands

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Texas Hold'em Hand Evaluation Logic (Simplified Placeholder) ---
def get_rank_value(rank):
    """Returns numerical value for poker ranks for comparison."""
    if rank.isdigit():
        if rank == '0': return 10
        return int(rank)
    elif rank == 'J': return 11
    elif rank == 'Q': return 12
    elif rank == 'A': return 14
    return 0

def evaluate_poker_hand(cards):
    """
    Evaluates a 7-card poker hand (5 community + 2 hole) and returns its type and value.
    This is a very simplified placeholder and DOES NOT correctly implement full poker rules.
    """
    if len(cards) < 5:
        return "Not enough cards", 0

    processed_cards = []
    for card in cards:
        processed_cards.append((get_rank_value(card.rank), card.suit[0].upper()))

    suit_groups = {}
    for r_val, suit_char in processed_cards:
        suit_groups.setdefault(suit_char, []).append(r_val)
    for suit_char, ranks_in_suit in suit_groups.items():
        if len(ranks_in_suit) >= 5:
            return "Flush", max(ranks_in_suit)

    unique_ranks = sorted(list(set([c[0] for c in processed_cards])), reverse=True)
    if 14 in unique_ranks and 2 in unique_ranks and 3 in unique_ranks and 4 in unique_ranks and 5 in unique_ranks:
        return "Straight", 5

    for i in range(len(unique_ranks) - 4):
        is_straight = True
        for j in range(4):
            if unique_ranks[i+j] - unique_ranks[i+j+1] != 1:
                is_straight = False
                break
        if is_straight:
            return "Straight", unique_ranks[i]

    rank_counts = {}
    for rank_val, _ in processed_cards:
        rank_counts[rank_val] = rank_counts.get(rank_val, 0) + 1

    quads = []
    trips = []
    pairs = []
    singles = []

    for rank_val, count in rank_counts.items():
        if count == 4: quads.append(rank_val)
        elif count == 3: trips.append(rank_val)
        elif count == 2: pairs.append(rank_val)
        else: singles.append(rank_val)

    quads.sort(reverse=True)
    trips.sort(reverse=True)
    pairs.sort(reverse=True)
    singles.sort(reverse=True)

    if quads:
        return "Four of a Kind", quads[0]
    if trips and pairs:
        return "Full House", trips[0]
    if trips:
        return "Three of a Kind", trips[0]
    if len(pairs) >= 2:
        return "Two Pair", pairs[0]
    if pairs:
        return "One Pair", pairs[0]
    
    return "High Card", processed_cards[0][0]


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot # Bot instance is passed but not used by this pure dealer for Discord comms
        logger.info("MechanicsMain (backend state management) initialized as a Discord Cog.")
    
        # Database credentials - NOW using credentials assigned to bot object
        # These are set in bot.py's on_ready event.
        self.db_user = self.bot.db_user
        self.db_password = self.bot.db_password
        self.db_host = self.bot.db_host
        self.db_name = "serene_users" # Assuming this is the database name

        # Define game constants
        self.PLAYER_TURN_TIME = 60 # seconds for betting rounds
        self.POST_SHOWDOWN_TIME = 10 # seconds for new game countdown

    async def cog_load(self):
        logger.info("MechanicsMain cog loaded successfully.")

    async def cog_unload(self):
        logger.info("MechanicsMain cog unloaded.")

    async def _get_db_connection(self):
        """Helper to get a database connection."""
        if not all([self.db_user, self.db_password, self.db_host, self.db_name]):
            logger.error("Missing DB credentials for MechanicsMain. Check bot.py's on_ready.")
            raise ConnectionError("Database credentials not configured or not assigned to bot object.")
        return await aiomysql.connect(
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            db=self.db_name,
            charset='utf8mb4',
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_game_state(self, room_id: str, guild_id: str = None, channel_id: str = None) -> dict:
        """
        Loads the game state for a given room_id from the database.
        If not found, initializes a new state, using provided guild_id and channel_id.
        Ensures guild_id and channel_id are always present in the returned state.
        """
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE room_id = %s",
                    (room_id,)
                )
                result = await cursor.fetchone()
                
                game_state = {}
                if result and result['game_state']:
                    game_state = json.loads(result['game_state'])
                    logger.info(f"[_load_game_state] Loaded existing game state for room_id: {room_id}. Players count: {len(game_state.get('players', []))}")
                    logger.debug(f"[_load_game_state] Raw DB game_state: {result['game_state']}") # Add this debug log
                else:
                    logger.warning(f"[_load_game_state] No existing game state found for room_id: {room_id}. Initializing new state.")
                    # Initialize with basic structure, including provided guild_id and channel_id
                    new_deck = Deck()
                    new_deck.build()
                    new_deck.shuffle()
                    game_state = {
                        'room_id': room_id,
                        'current_round': 'pre_game', # pre_game, pre_flop, flop, turn, river, showdown
                        'players': [], # Each player will have 'discord_id', 'name', 'hand', 'seat_id', 'avatar_url', 'total_chips', 'current_bet_in_round', 'has_acted_in_round', 'folded'
                        'dealer_hand': [], # Initialize dealer's hand
                        'deck': new_deck.to_output_format(),
                        'board_cards': [],
                        'last_evaluation': None,
                        'current_player_turn_index': -1, # Index in the sorted players list
                        'current_betting_round_pot': 0,
                        'current_round_min_bet': 0, # The amount to call
                        'last_aggressive_action_player_id': None, # Player who last bet or raised
                        'timer_end_time': None, # Unix timestamp (seconds)
                        'dealer_button_position': 0, # Index of the player with the dealer button
                        'small_blind_amount': 5,
                        'big_blind_amount': 10,
                        'game_started_once': False # To track if the game has ever started
                    }
                
                # --- IMPORTANT: Ensure guild_id and channel_id are always present ---
                # If they were missing from the loaded state (e.g., old DB entry)
                # or if a new state was just initialized, set them from the arguments.
                if 'guild_id' not in game_state or game_state['guild_id'] is None:
                    game_state['guild_id'] = guild_id
                    logger.info(f"[_load_game_state] Set guild_id to {guild_id} for room {room_id} (was missing/None).")
                if 'channel_id' not in game_state or game_state['channel_id'] is None:
                    game_state['channel_id'] = channel_id
                    logger.info(f"[_load_game_state] Set channel_id to {channel_id} for room {room_id} (was missing/None).")

                # Ensure new fields are initialized if loading an older state
                game_state.setdefault('current_player_turn_index', -1)
                game_state.setdefault('current_betting_round_pot', 0)
                game_state.setdefault('current_round_min_bet', 0)
                game_state.setdefault('last_aggressive_action_player_id', None)
                game_state.setdefault('timer_end_time', None)
                game_state.setdefault('dealer_button_position', 0)
                game_state.setdefault('small_blind_amount', 5)
                game_state.setdefault('big_blind_amount', 10)
                game_state.setdefault('game_started_once', False)

                for player in game_state.get('players', []):
                    player.setdefault('total_chips', 1000) # Default starting chips
                    player.setdefault('current_bet_in_round', 0)
                    player.setdefault('has_acted_in_round', False)
                    player.setdefault('folded', False)

                return game_state
        except Exception as e:
            logger.error(f"Error loading game state for room {room_id}: {e}", exc_info=True)
            raise # Re-raise to be caught by the handler
        finally:
            if conn:
                conn.close()

    async def _save_game_state(self, room_id: str, game_state: dict):
        """Saves the game state for a given room_id to the database."""
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                game_state_json = json.dumps(game_state)
                logger.debug(f"[_save_game_state] Saving game_state for room {room_id}: {game_state_json}") # Add this debug log
                
                # Changed from INSERT...ON DUPLICATE KEY UPDATE to a pure UPDATE
                await cursor.execute(
                    "UPDATE bot_game_rooms SET game_state = %s WHERE room_id = %s",
                    (game_state_json, room_id)
                )
                await conn.commit() # Explicitly commit the transaction

                if cursor.rowcount == 0:
                    logger.error(f"[_save_game_state] Failed to update game state for room_id: {room_id}. Room not found in DB. Game state: {game_state_json}")
                    # Optionally, you could raise an exception here if a room not existing is a critical error
                    # raise ValueError(f"Game room {room_id} not found for update.")
                else:
                    logger.info(f"Game state updated for room_id: {room_id} in bot_game_rooms.")
        except Exception as e:
            logger.error(f"Error saving game state for room {room_id}: {e}", exc_info=True)
            raise # Re-raise to be caught by the handler
        finally:
            if conn:
                conn.close()

    async def deal_hole_cards(self, room_id: str) -> tuple[bool, str]:
        """Deals two hole cards to each player for the specified room_id."""
        # Note: _load_game_state will now ensure guild_id and channel_id are present if new state
        game_state = await self._load_game_state(room_id)
        
        # Ensure players list is not empty for dealing
        if not game_state.get('players'):
            # For a single-player game, this check might need to be adjusted
            # If a game can start with 0 players, this should return True or be removed.
            # Assuming at least one player must be seated to deal cards.
            return False, "No players in the game to deal cards."

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        # Shuffle only if it's a new game or if the deck hasn't been shuffled yet for this round
        # For simplicity, we'll re-shuffle here if it's 'pre_game'
        if game_state['current_round'] == 'pre_game' or not deck.cards:
            deck.build() # Rebuild a full deck
            deck.shuffle()
            logger.info(f"Deck rebuilt and shuffled for room {room_id}.")
            
        players_data = game_state.get('players', [])

        for player in players_data:
            player['hand'] = [] # Clear existing hands
            player['folded'] = False # Reset folded status
            player['current_bet_in_round'] = 0 # Reset current bet
            player['has_acted_in_round'] = False # Reset acted status
            card1 = deck.deal_card()
            card2 = deck.deal_card()
            if card1 and card2:
                player['hand'].append(card1.to_output_format())
                player['hand'].append(card2.to_output_format())
            else:
                logger.error("Not enough cards to deal hole cards.")
                return False, "Not enough cards."

        game_state['deck'] = deck.to_output_format()
        game_state['players'] = players_data
        game_state['board_cards'] = [] # Ensure board is empty for a new deal
        # game_state['current_round'] = "pre_flop" # This will now be set by _start_new_round_pre_flop if applicable

        await self._save_game_state(room_id, game_state)
        return True, "Hole cards dealt."

    async def deal_dealer_cards(self, room_id: str) -> tuple[bool, str]:
        """Deals two cards to the dealer for the specified room_id."""
        game_state = await self._load_game_state(room_id)

        deck = Deck(game_state.get('deck', []))

        # Ensure dealer_hand is initialized
        if 'dealer_hand' not in game_state or not isinstance(game_state['dealer_hand'], list):
            game_state['dealer_hand'] = []
        else:
            game_state['dealer_hand'].clear() # Clear existing dealer hand for a new deal

        card1 = deck.deal_card()
        card2 = deck.deal_card()
        
        if card1 and card2:
            game_state['dealer_hand'].append(card1.to_output_format())
            game_state['dealer_hand'].append(card2.to_output_format())
            logger.info(f"Dealer cards dealt for room {room_id}.")
        else:
            logger.error("Not enough cards to deal dealer's hand.")
            return False, "Not enough cards to deal dealer's hand."

        game_state['deck'] = deck.to_output_format()
        await self._save_game_state(room_id, game_state)
        return True, "Dealer's cards dealt."


    async def deal_flop(self, room_id: str) -> tuple[bool, str]:
        """Deals the three community cards (flop) for the specified room_id."""
        game_state = await self._load_game_state(room_id)
        if game_state['current_round'] != 'pre_flop':
            return False, f"Cannot deal flop. Current round is {game_state['current_round']}."

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        flop_cards_obj = []
        for _ in range(3):
            card = deck.deal_card()
            if card:
                flop_cards_obj.append(card)
                board_cards_output.append(card.to_output_format())
            else:
                return False, "Not enough cards for flop."

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "flop"
        
        await self._save_game_state(room_id, game_state)
        return True, "Flop dealt."

    async def deal_turn(self, room_id: str) -> tuple[bool, str]:
        """Deals the fourth community card (turn) for the specified room_id."""
        game_state = await self._load_game_state(room_id)
        if game_state['current_round'] != 'flop':
            return False, f"Cannot deal turn. Current round is {game_state['current_round']}."

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        turn_card = deck.deal_card()
        if turn_card:
            board_cards_output.append(turn_card.to_output_format())
        else:
            return False, "Not enough cards for turn."

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "turn"

        await self._save_game_state(room_id, game_state)
        return True, "Turn dealt."

    async def deal_river(self, room_id: str) -> tuple[bool, str]:
        """Deals the fifth and final community card (river) for the specified room_id."""
        game_state = await self._load_game_state(room_id)
        if game_state['current_round'] != 'turn':
            return False, f"Cannot deal river. Current round is {game_state['current_round']}."

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        river_card = deck.deal_card()
        if river_card:
            board_cards_output.append(river_card.to_output_format())
        else:
            return False, "Not enough cards for river."

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "river"

        await self._save_game_state(room_id, game_state)
        return True, "River dealt."

    async def evaluate_hands(self, room_id: str) -> tuple[bool, str]:
        """Evaluates all players' hands against the community cards for the specified room_id."""
        game_state = await self._load_game_state(room_id)
        if game_state['current_round'] != 'river':
            return False, f"Cannot evaluate hands. Current round is {game_state['current_round']}."

        players_data = game_state.get('players', [])
        board_cards_obj = [Card.from_output_format(c_str) for c_str in game_state.get('board_cards', [])] # Use Card from game_models

        if len(board_cards_obj) != 5:
            logger.error("Board not complete for evaluation.")
            return False, "Board not complete."

        player_evaluations = []
        for player_data in players_data:
            player_hand_obj = [Card.from_output_format(c_str) for c_str in player_data.get('hand', [])] # Use Card from game_models
            combined_cards = player_hand_obj + board_cards_obj
            hand_type, hand_value = evaluate_poker_hand(combined_cards)
            player_evaluations.append({
                "discord_id": player_data['discord_id'],
                "name": player_data['name'],
                "hand_type": hand_type,
                "hand_value": hand_value,
                "hole_cards": [c.to_output_format() for c in player_hand_obj]
            })

        game_state['current_round'] = "showdown"
        game_state['last_evaluation'] = player_evaluations
        game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME # 10-second timer after showdown

        await self._save_game_state(room_id, game_state)
        return True, "Hands evaluated."

    async def broadcast_game_state(self, room_id: str, game_state: dict, echo_message: dict = None):
        """
        Broadcasts the current game state to all connected WebSocket clients in the room.
        Optionally includes an echo_message for chat.
        """
        if room_id not in self.bot.ws_rooms:
            logger.warning(f"No WebSocket clients found for room_id: {room_id}. Cannot broadcast.")
            return

        payload = {
            "game_state": game_state
        }
        if echo_message:
            payload["echo_message"] = echo_message
            logger.info(f"Broadcasting game state with echo_message for room {room_id}.")
        
        # Add a debug log to see the current_round being broadcast
        logger.debug(f"Broadcasting game state with current_round: {game_state.get('current_round', 'N/A')} for room {room_id}.")

        message_json = json.dumps(payload)
        
        for websocket in list(self.bot.ws_rooms[room_id]): # Iterate over a copy to avoid issues if clients disconnect
            try:
                await websocket.send_str(message_json)
            except Exception as e:
                logger.error(f"Error sending WebSocket message to client in room {room_id}: {e}", exc_info=True)
                # Optionally remove disconnected websocket here, or rely on on_disconnect
                # self.bot.ws_rooms[room_id].remove(websocket) # This might be handled by aiohttp's ws_handler

    # --- Helper for getting sorted players ---
    def _get_sorted_players(self, game_state: dict) -> list:
        """Returns a list of players sorted by their seat_id."""
        players = game_state.get('players', [])
        # Filter out players without a seat_id or folded players if needed for active turn
        active_players = [p for p in players if p.get('seat_id') and not p.get('folded', False)]
        return sorted(active_players, key=lambda p: int(p['seat_id'].replace('seat_', '')))

    # --- Helper for getting next active player turn ---
    def _get_next_active_player_index(self, game_state: dict, current_index: int) -> int:
        """
        Finds the index of the next active player in the sorted list,
        skipping folded players. Returns -1 if no active players.
        """
        sorted_players = self._get_sorted_players(game_state)
        if not sorted_players:
            # If no players at all, return -1. This is fine for single-player if the game
            # logic handles a single player's turn correctly without needing a "next" player.
            return -1

        num_players = len(sorted_players)
        
        # Determine the starting point for finding the next player
        start_search_index = (current_index + 1) % num_players if current_index != -1 else 0

        for i in range(num_players):
            idx = (start_search_index + i) % num_players
            player = sorted_players[idx]
            # Player is active if not folded and has not yet acted (or needs to match a new bet)
            # The 'has_acted_in_round' needs to be carefully managed. For now, focus on folded.
            if not player.get('folded', False):
                return idx
        return -1 # No active players found

    # --- Helper to start a player's turn ---
    async def _start_player_turn(self, room_id: str, game_state: dict):
        """Sets the timer for the current player's turn."""
        sorted_players = self._get_sorted_players(game_state)
        current_player_index = game_state['current_player_turn_index']

        # Allow starting turn even if only one player, as long as index is valid
        if not sorted_players or current_player_index == -1 or current_player_index >= len(sorted_players):
            # For a single player, if current_player_turn_index is -1, set it to 0
            if len(sorted_players) == 1 and current_player_index == -1:
                game_state['current_player_turn_index'] = 0
                current_player_index = 0
                logger.info(f"Setting initial turn for single player {sorted_players[current_player_index]['name']}.")
            else:
                logger.error(f"Invalid current_player_turn_index: {current_player_index} with {len(sorted_players)} players. Cannot start turn.")
                game_state['timer_end_time'] = None
                game_state['current_player_turn_index'] = -1
                return

        game_state['timer_end_time'] = int(time.time()) + self.PLAYER_TURN_TIME
        logger.info(f"Starting turn for player {sorted_players[current_player_index]['name']}. Timer ends at {game_state['timer_end_time']}")
        await self._save_game_state(room_id, game_state)


    # --- Helper to apply blinds ---
    async def _apply_blinds(self, game_state: dict):
        """Applies small and big blinds to players."""
        sorted_players = self._get_sorted_players(game_state)
        num_players = len(sorted_players)

        # Removed: if num_players < 2: logger.warning("Not enough players to apply blinds."); return
        # Allowing blinds logic to run even with one player.
        # For a single-player game, blinds might not be relevant or need custom logic.
        # Current logic will try to apply blinds to non-existent players if num_players < 2,
        # leading to index errors if not handled.
        # For now, we'll assume a single player might implicitly post both blinds, or blinds are skipped.
        # If the game is truly single-player, this section might need to be re-thought or skipped entirely.
        if num_players == 0:
            logger.warning("No players to apply blinds. Skipping.")
            return

        # Determine positions relative to the dealer button
        dealer_pos = game_state['dealer_button_position']
        
        # Small blind is next to dealer, big blind is after small blind
        small_blind_pos_idx = (dealer_pos + 1) % num_players
        big_blind_pos_idx = (dealer_pos + 2) % num_players

        # Ensure indices are valid before accessing players
        small_blind_player = sorted_players[small_blind_pos_idx] if small_blind_pos_idx < num_players else None
        big_blind_player = sorted_players[big_blind_pos_idx] if big_blind_pos_idx < num_players else None

        if small_blind_player:
            # Deduct small blind
            small_blind_amount = min(game_state['small_blind_amount'], small_blind_player['total_chips'])
            small_blind_player['total_chips'] -= small_blind_amount
            small_blind_player['current_bet_in_round'] += small_blind_amount
            game_state['current_betting_round_pot'] += small_blind_amount
            logger.info(f"Player {small_blind_player['name']} posts small blind: ${small_blind_amount}")
            small_blind_player['has_acted_in_round'] = True # Mark as acted

        if big_blind_player:
            # Deduct big blind
            big_blind_amount = min(game_state['big_blind_amount'], big_blind_player['total_chips'])
            big_blind_player['total_chips'] -= big_blind_amount
            big_blind_player['current_bet_in_round'] += big_blind_amount
            game_state['current_betting_round_pot'] += big_blind_amount
            logger.info(f"Player {big_blind_player['name']} posts big blind: ${big_blind_amount}")
            big_blind_player['has_acted_in_round'] = True # Big blind has acted by posting

        # Set the minimum bet for this round to the big blind amount
        # If only one player, this might be 0 or a default.
        if big_blind_player:
            game_state['current_round_min_bet'] = game_state['big_blind_amount']
        else:
            game_state['current_round_min_bet'] = 0 # No big blind, min bet is 0

        # Update players list in game_state (ensure changes to player dicts are reflected)
        for i, player_in_state in enumerate(game_state['players']):
            if small_blind_player and player_in_state['discord_id'] == small_blind_player['discord_id']:
                game_state['players'][i] = small_blind_player
            elif big_blind_player and player_in_state['discord_id'] == big_blind_player['discord_id']:
                game_state['players'][i] = big_blind_player


    async def _start_betting_round(self, room_id: str, game_state: dict):
        """Initializes variables for a new betting round."""
        logger.info(f"Starting new betting round for {game_state['current_round']}")
        
        # Reset betting related stats for all active players
        for player in game_state['players']:
            if not player.get('folded', False):
                player['current_bet_in_round'] = 0
                player['has_acted_in_round'] = False
        
        game_state['current_round_min_bet'] = 0 # Reset for new round, will be set by blinds or first bet
        game_state['last_aggressive_action_player_id'] = None # Reset for new round

        # Determine who starts the betting for this round
        sorted_players = self._get_sorted_players(game_state)
        num_players = len(sorted_players)

        if num_players == 0:
            logger.warning("No players to start betting round. Skipping.")
            # If no players, perhaps end the game or wait. For now, just return.
            return

        if game_state['current_round'] == 'pre_flop':
            # Pre-flop: Action starts after big blind (i.e., player after big blind)
            dealer_pos = game_state['dealer_button_position']
            big_blind_pos_idx = (dealer_pos + 2) % num_players
            first_player_index = (big_blind_pos_idx + 1) % num_players
            
            # Apply blinds (logic updated to handle single player gracefully)
            await self._apply_blinds(game_state)
            
            # If only one player, that player is effectively the only one who can act.
            # The first_player_index logic should still correctly point to them.
            if num_players == 1:
                first_player_index = 0 # The single player is always the first to act
            # Blinds logic is complex. For now, players who posted blinds are considered to have acted
            # for the amount of their blind. They will need to act again if there's a raise.
            # The 'has_acted_in_round' flag will be reset for all players (except folded)
            # at the start of each betting round and then set to True when they act.
            # The player after the big blind starts the action.

        else: # Flop, Turn, River betting rounds: Action starts with the first active player after the dealer button
            dealer_pos = game_state['dealer_button_position']
            first_player_index = (dealer_pos + 1) % num_players
            # Find the next *active* player after the dealer button
            first_player_index = self._get_next_active_player_index(game_state, first_player_index - 1) 

        if first_player_index != -1:
            game_state['current_player_turn_index'] = first_player_index
            await self._start_player_turn(room_id, game_state)
        else:
            logger.warning(f"No active players to start betting round in room {room_id}. Advancing phase.")
            # If no active players, round might end immediately or game over.
            await self._advance_game_phase(room_id, game_state)


    async def _end_betting_round(self, room_id: str, game_state: dict):
        """Collects bets into the main pot and prepares for the next phase."""
        logger.info(f"Ending betting round for {game_state['current_round']}.")
        
        # Collect all current_bet_in_round into the main pot
        for player in game_state['players']:
            game_state['current_betting_round_pot'] += player['current_bet_in_round']
            player['current_bet_in_round'] = 0 # Reset for next betting round
            player['has_acted_in_round'] = False # Reset acted status for next round

        game_state['current_round_min_bet'] = 0 # Reset min bet for next round
        game_state['last_aggressive_action_player_id'] = None # Reset aggressive action

        await self._save_game_state(room_id, game_state)


    def _check_round_completion(self, game_state: dict) -> bool:
        """
        Checks if the current betting round is complete.
        A round is complete if:
        1. Only one player is not folded. (This player wins the pot)
        2. All active players (not folded) have acted AND matched the highest bet.
           If there was no aggressive action (all checks), then all must have acted.
        """
        sorted_players = self._get_sorted_players(game_state)
        active_players = [p for p in sorted_players if not p.get('folded', False)]

        # For single-player, if the player is active, the round can be considered complete
        # once they have acted (e.g., checked, bet).
        if len(active_players) <= 1: # This condition already handles 0 or 1 active players
            logger.info("Betting round complete: 1 or fewer active players remaining.")
            return True # Round ends if only one player or no players left (e.g., all folded)

        highest_bet_in_round = max([p.get('current_bet_in_round', 0) for p in active_players])

        # Check if all active players have acted
        all_active_players_acted = all(p.get('has_acted_in_round', False) for p in active_players)

        # Check if all active players have matched the highest bet
        all_matched_highest_bet = all(p.get('current_bet_in_round', 0) == highest_bet_in_round for p in active_players)

        # If there was no aggressive action (i.e., highest_bet_in_round is 0), then everyone just needs to have acted (checked).
        if highest_bet_in_round == 0:
            return all_active_players_acted
        else:
            # If there was a bet/raise, everyone must have acted and matched the highest bet.
            # A special case: if a player went all-in for less than the highest bet,
            # others don't need to bet more than that all-in amount to call.
            # For simplicity, we'll assume 'all_matched_highest_bet' covers this for now.
            return all_active_players_acted and all_matched_highest_bet


    async def _advance_game_phase(self, room_id: str, game_state: dict):
        """Moves the game to the next phase (flop, turn, river, showdown)."""
        logger.info(f"Advancing game phase from {game_state['current_round']}")
        
        # Collect bets into main pot before advancing phase
        await self._end_betting_round(room_id, game_state) # This also saves the state

        # Reload game_state after _end_betting_round has saved it
        game_state = await self._load_game_state(room_id)

        next_round = None
        if game_state['current_round'] == 'pre_flop':
            success, msg = await self.deal_flop(room_id)
            next_round = 'flop'
        elif game_state['current_round'] == 'flop':
            success, msg = await self.deal_turn(room_id)
            next_round = 'turn'
        elif game_state['current_round'] == 'turn':
            success, msg = await self.deal_river(room_id)
            next_round = 'river'
        elif game_state['current_round'] == 'river':
            success, msg = await self.evaluate_hands(room_id)
            next_round = 'showdown'
        elif game_state['current_round'] == 'showdown':
            # After showdown, automatically start new round after its timer expires (handled by frontend triggering auto_action_timeout)
            # The timer for post-showdown is set in evaluate_hands.
            # For single player, this will immediately start a new round after showdown.
            success, msg = await self._start_new_round_pre_flop(room_id, game_state['guild_id'], game_state['channel_id'])
            next_round = 'pre_flop' # If successful, it moves to pre_flop

        if not success:
            logger.error(f"Failed to advance game phase: {msg}")
            # Handle error: perhaps notify players, or reset game.
            return

        # Reload game_state after dealing cards for the new phase
        game_state = await self._load_game_state(room_id)

        # Start the new betting round (if applicable)
        if next_round in ['flop', 'turn', 'river', 'pre_flop']: # Pre-flop also starts a betting round
            await self._start_betting_round(room_id, game_state) # This will set the first player's turn and timer
        
        # If it's showdown, the timer is already set in evaluate_hands.
        # No new betting round starts after showdown, only a countdown to the next game.

        # Save the final state after phase advance (already done in _start_betting_round or evaluate_hands)
        # but ensure current_round is correctly set if not done by sub-functions.
        if game_state['current_round'] != next_round: # Only update if sub-function didn't set it
             game_state['current_round'] = next_round
             await self._save_game_state(room_id, game_state) # Save final state if round changed


    # --- New WebSocket Request Handler ---
    async def handle_websocket_game_action(self, request_data: dict):
        """
        Receives raw request data from a WebSocket client and dispatches it
        to the appropriate game action method.
        After processing, it broadcasts the updated game state to all clients.
        """
        action = request_data.get('action')
        room_id = request_data.get('room_id')
        guild_id = request_data.get('guild_id')    
        channel_id = request_data.get('channel_id') 
        sender_id = request_data.get('sender_id') # Assuming sender_id is always present in WS requests

        if not all([action, room_id, guild_id, channel_id, sender_id]):
            logger.error(f"Missing required parameters for handle_websocket_game_action. Data: {request_data}")
            # Broadcast an error message back to the sender if possible, or log.
            # For simplicity, we'll just log and return for now.
            return

        logger.info(f"Backend dealer received WS action: '{action}' for Room ID: {room_id}, Sender: {sender_id}")

        success = False
        message = "Unknown action."
        echo_message_data = None # To hold chat message data if applicable

        try:
            # Load the current game state at the beginning of processing any action
            game_state = await self._load_game_state(room_id, guild_id, channel_id)
            logger.debug(f"Current round loaded at start of handle_websocket_game_action: {game_state.get('current_round', 'N/A')}")

            if action == "get_state":
                success = True
                message = "Game state retrieved."
                # No need to modify game_state, just broadcast its current form
            elif action == "add_player":
                player_data = request_data.get('player_data')
                if not player_data or not isinstance(player_data, dict):
                    logger.error("Missing or invalid player_data for add_player.")
                    return # No broadcast for invalid request
                success, message = await self._add_player_to_game(room_id, player_data, guild_id, channel_id)
            elif action == "leave_player":
                discord_id = request_data.get('discord_id')
                if not discord_id:
                    logger.error("Missing discord_id for leave_player.")
                    return
                success, message = await self._leave_player(room_id, discord_id)
            elif action == "start_new_round_pre_flop":
                # For single-player, we don't need to check initiator or timer for auto-start after showdown.
                # The game should just start if the button is pressed.
                # Removed: is_initiator = (sender_id == request_data.get('initiator_id'))
                # Removed: if (not game_state.get('game_started_once', False) and is_initiator) or \
                # Removed:    (game_state.get('current_round') == 'showdown' and int(time.time()) >= game_state.get('timer_end_time', 0)):
                
                # Allow starting if current_round is 'pre_game' or 'showdown' (after a previous game)
                if game_state.get('current_round') in ['pre_game', 'showdown']:
                    success, message = await self._start_new_round_pre_flop(room_id, guild_id, channel_id)
                    if success:
                        # After successful start, ensure game_started_once is true and save
                        # This flag is important for the frontend's "Play Game" button visibility
                        game_state_after_start = await self._load_game_state(room_id) # Reload to get latest state
                        if not game_state_after_start.get('game_started_once', False):
                            game_state_after_start['game_started_once'] = True 
                            await self._save_game_state(room_id, game_state_after_start) # Save this flag
                else:
                    logger.warning(f"Attempt to start new round failed: Game is already in progress or not in a startable state ({game_state.get('current_round')}). {request_data}")
                    return # No broadcast for invalid action
            elif action == "player_action":
                player_id = request_data.get('player_id')
                action_type = request_data.get('action_type')
                amount = request_data.get('amount', 0) # For bet/raise
                
                if not all([player_id, action_type]):
                    logger.error("Missing player_id or action_type for player_action.")
                    return
                success, message = await self._handle_player_action(room_id, player_id, action_type, amount)
            elif action == "auto_action_timeout":
                player_id = request_data.get('player_id')
                if not player_id:
                    logger.error("Missing player_id for auto_action_timeout.")
                    return
                success, message = await self._auto_action_on_timeout(room_id, player_id)
            elif action == "send_message":
                message_content = request_data.get('message_content')
                if not message_content:
                    logger.error("Missing message_content for send_message.")
                    return
                success, message, response_data_from_handler = await self._handle_in_game_message(room_id, sender_id, message_content)
                if success:
                    echo_message_data = response_data_from_handler.get('echo_message')
                    # The game_state from _handle_in_game_message is already the latest,
                    # but we'll reload it below to ensure consistency after any potential save.
            else:
                logger.warning(f"Received unsupported WS action: {action}")
                return # No broadcast for unsupported action

            # After any action (if successful), reload the latest state and broadcast it.
            # This ensures all clients get the most up-to-date game_state.
            if success:
                updated_game_state = await self._load_game_state(room_id, guild_id, channel_id)
                logger.debug(f"Current round before save/broadcast: {updated_game_state.get('current_round', 'N/A')}")
                await self.broadcast_game_state(room_id, updated_game_state, echo_message_data)
                logger.info(f"Action '{action}' processed and state broadcast for room {room_id}.")
            else:
                logger.warning(f"Action '{action}' failed for room {room_id}: {message}")
                # Optionally, broadcast an error message back to the sender only.
                # For now, just log.

        except Exception as e:
            logger.error(f"Error processing WS action '{action}' for room {room_id}: {e}", exc_info=True)
            # Optionally, broadcast a general error to the room or specific sender.


    async def _handle_player_action(self, room_id: str, player_id: str, action_type: str, amount: int = 0) -> tuple[bool, str]:
        """
        Processes a player's action (call, check, bet, raise, fold).
        Updates game state, advances turn, and triggers next phase if round completes.
        """
        game_state = await self._load_game_state(room_id)
        sorted_players = self._get_sorted_players(game_state)
        
        # Find the actual player object in the game_state['players'] list
        # to ensure updates persist correctly.
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state:
            return False, "Player not found in game."

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 else None

        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            return False, "It's not your turn."
        
        if player_in_state.get('folded', False):
            return False, "You have already folded."

        min_bet_to_call = game_state['current_round_min_bet'] - player_in_state['current_bet_in_round']
        
        message = ""
        success = False

        if action_type == 'fold':
            player_in_state['folded'] = True
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} folded."
            success = True
            logger.info(message)
        elif action_type == 'check':
            if min_bet_to_call > 0:
                return False, "Cannot check, a bet has been made. You must call or raise."
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} checked."
            success = True
            logger.info(message)
        elif action_type == 'call':
            if player_in_state['total_chips'] < min_bet_to_call:
                return False, f"Not enough chips to call ${min_bet_to_call}. You have ${player_in_state['total_chips']}."
            
            bet_amount = min_bet_to_call
            player_in_state['total_chips'] -= bet_amount
            player_in_state['current_bet_in_round'] += bet_amount
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} called ${bet_amount}."
            success = True
            logger.info(message)
        elif action_type == 'bet' or action_type == 'raise':
            if amount <= min_bet_to_call:
                return False, f"Bet/Raise amount must be greater than current call amount (${min_bet_to_call})."
            if player_in_state['total_chips'] < amount:
                return False, f"Not enough chips to bet/raise ${amount}. You have ${player_in_state['total_chips']}."
            
            player_in_state['total_chips'] -= amount
            player_in_state['current_bet_in_round'] += amount
            game_state['current_round_min_bet'] = player_in_state['current_bet_in_round'] # New highest bet
            game_state['last_aggressive_action_player_id'] = player_id # Mark who made the aggressive action

            # When a player bets/raises, all other players (who haven't folded) need to act again.
            # So, reset 'has_acted_in_round' for all players except the current one and folded ones.
            for p in game_state['players']:
                if p['discord_id'] != player_id and not p.get('folded', False):
                    p['has_acted_in_round'] = False
            player_in_state['has_acted_in_round'] = True # Current player has acted

            message = f"{player_in_state['name']} {action_type}d ${amount}."
            success = True
            logger.info(message)
        elif action_type == 'all_in':
            # Player goes all-in with remaining chips
            amount = player_in_state['total_chips']
            if amount == 0:
                return False, "You have no chips to go all-in."

            player_in_state['total_chips'] = 0
            player_in_state['current_bet_in_round'] += amount
            
            # If all-in amount is greater than current min bet, it becomes the new min bet
            if player_in_state['current_bet_in_round'] > game_state['current_round_min_bet']:
                game_state['current_round_min_bet'] = player_in_state['current_bet_in_round']
                game_state['last_aggressive_action_player_id'] = player_id
                # Reset acted status for others if this all-in is a raise
                for p in game_state['players']:
                    if p['discord_id'] != player_id and not p.get('folded', False):
                        p['has_acted_in_round'] = False
            
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} went All-In with ${amount}!"
            success = True
            logger.info(message)
        else:
            return False, "Invalid player action type."

        if success:
            # Update the player in the main game_state players list (already done by reference if player_in_state was used)
            # Ensure the game_state is saved here as well.
            await self._save_game_state(room_id, game_state)

            # Check for round completion
            if self._check_round_completion(game_state):
                logger.info(f"Betting round {game_state['current_round']} completed. Advancing phase.")
                await self._advance_game_phase(room_id, game_state)
            else:
                # Advance turn to next active player if round not complete
                next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
                if next_player_idx != -1:
                    game_state['current_player_turn_index'] = next_player_idx
                    await self._start_player_turn(room_id, game_state)
                else:
                    # This case should ideally not happen if _check_round_completion is correct
                    # but as a fallback, if no next player, advance phase.
                    logger.warning("No next active player found, but round not marked complete. Advancing phase as fallback.")
                    await self._advance_game_phase(room_id, game_state)
            return True, message
        return success, message

    async def _auto_action_on_timeout(self, room_id: str, player_id: str) -> tuple[bool, str]:
        """
        Performs an automatic action (call/fold) for a player whose turn timed out.
        """
        game_state = await self._load_game_state(room_id)
        sorted_players = self._get_sorted_players(game_state)
        
        # Find the actual player object in the game_state['players'] list
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state:
            return False, "Player not found in game for timeout action."

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 else None

        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            return False, "Timeout action for incorrect player or not their turn."
        
        if int(time.time()) < game_state.get('timer_end_time', 0):
            return False, "Player's turn has not timed out yet."

        min_bet_to_call = game_state['current_round_min_bet'] - player_in_state['current_bet_in_round']
        
        action_message = ""
        if min_bet_to_call > 0:
            # If there's a bet to call, auto-fold
            player_in_state['folded'] = True
            action_message = f"{player_in_state['name']} automatically folded due to timeout."
        else:
            # If no bet to call, auto-check/call (if current bet is 0)
            player_in_state['has_acted_in_round'] = True
            action_message = f"{player_in_state['name']} automatically checked/called due to timeout."
        
        # Update the player in the main game_state players list (already done by reference)
        await self._save_game_state(room_id, game_state) # Save state after auto-action

        # Now, check for round completion and advance turn/phase
        if self._check_round_completion(game_state):
            logger.info(f"Betting round {game_state['current_round']} completed after timeout. Advancing phase.")
            await self._advance_game_phase(room_id, game_state)
        else:
            next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
            if next_player_idx != -1:
                game_state['current_player_turn_index'] = next_player_idx
                await self._start_player_turn(room_id, game_state)
            else:
                logger.warning("No next active player after timeout, but round not marked complete. Advancing phase as fallback.")
                await self._advance_game_phase(room_id, game_state)

        return True, action_message


    async def _add_player_to_game(self, room_id: str, player_data: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str]:
        """
        Adds a player to the game state for a given room_id, including their chosen seat_id.
        Ensures a player cannot sit in an occupied seat or sit in multiple seats.
        """
        logger.info(f"[_add_player_to_game] Attempting to add player for room {room_id} with data: {player_data}")
        # Pass guild_id and channel_id to _load_game_state so it can initialize a new state correctly if needed
        game_state = await self._load_game_state(room_id, guild_id, channel_id)
        players = game_state.get('players', [])
        logger.info(f"[_add_player_to_game] Current players in game state: {players}")
        
        player_discord_id = player_data['discord_id']
        player_name = player_data['name']
        seat_id = player_data.get('seat_id') # Get the seat_id from player_data

        if not seat_id:
            logger.warning(f"[_add_player_to_game] No seat_id provided for player {player_name}.")
            return False, "Seat ID is required to add a player."

        # Check if player already exists and is seated
        existing_player = next((p for p in players if p['discord_id'] == player_discord_id), None)
        if existing_player:
            logger.info(f"[_add_player_to_game] Player {player_name} ({player_discord_id}) already exists in game state.")
            # Ensure 'seat_id' exists in existing_player before comparing
            if existing_player.get('seat_id') == seat_id:
                logger.info(f"[_add_player_to_game] Player {player_name} already in seat {seat_id}.")
                return False, f"Player {player_name} is already in seat {seat_id}."
            else:
                # If player is already seated in a *different* seat, prevent new seating
                if existing_player.get('seat_id'):
                    logger.warning(f"[_add_player_to_game] Player {player_name} is trying to sit in seat {seat_id} but is already in seat {existing_player.get('seat_id')}.")
                    return False, f"Player {player_name} is already seated elsewhere. Please leave your current seat first."
                else:
                    # This case means player exists but has no seat_id (e.g., from an old state), allow them to take a seat
                    existing_player['seat_id'] = seat_id
                    existing_player['name'] = player_name # Update name in case it changed
                    existing_player['avatar_url'] = player_data.get('avatar_url') # Ensure avatar is updated too
                    logger.info(f"[_add_player_to_game] Player {player_name} updated with seat {seat_id}.")
        else:
            # Check if the target seat is already occupied by *any* player
            if any(p.get('seat_id') == seat_id for p in players):
                logger.warning(f"[_add_player_to_game] Seat {seat_id} is already occupied.")
                return False, f"Seat {seat_id} is already occupied by another player."

            # Add new player with seat_id, ensuring hand is empty initially
            new_player = {
                'discord_id': player_discord_id,
                'name': player_name,
                'hand': [],
                'seat_id': seat_id, # Store the chosen seat ID
                'avatar_url': player_data.get('avatar_url'), # Store avatar URL if provided
                'total_chips': 1000, # Default starting chips
                'current_bet_in_round': 0,
                'has_acted_in_round': False,
                'folded': False
            }
            players.append(new_player)
            logger.info(f"[_add_player_to_game] New player {player_name} added to game state. Current players list: {players}")

        game_state['players'] = players # This line ensures the updated 'players' list is assigned back to game_state
        
        logger.info(f"[_add_player_to_game] Saving game state for room {room_id} after player update. Players count: {len(game_state['players'])}")
        await self._save_game_state(room_id, game_state)

        # NEW DEBUGGING STEP: Read state directly from DB after saving
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE room_id = %s",
                    (room_id,)
                )
                result = await cursor.fetchone()
                if result and result['game_state']:
                    loaded_state_after_save = json.loads(result['game_state'])
                    logger.info(f"[_add_player_to_game] State directly from DB after save: Players count: {len(loaded_state_after_save.get('players', []))}. Full state: {loaded_state_after_save}")
                else:
                    logger.warning(f"[_add_player_to_game] No state found in DB immediately after save for room {room_id}.")
        except Exception as e:
            logger.error(f"[_add_player_to_game] Error verifying DB state after save: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

        logger.info(f"[_add_player_to_game] Player {player_name} added/updated in seat {seat_id} in room {room_id}. State saved.")
        return True, "Player added successfully."

    async def _leave_player(self, room_id: str, discord_id: str) -> tuple[bool, str]:
        """Removes a player from the game state for a given room_id."""
        logger.info(f"[_leave_player] Attempting to remove player {discord_id} from room {room_id}.")
        game_state = await self._load_game_state(room_id)
        
        initial_player_count = len(game_state.get('players', []))
        game_state['players'] = [p for p in game_state.get('players', []) if p['discord_id'] != discord_id]
        
        if len(game_state['players']) < initial_player_count:
            logger.info(f"[_leave_player] Player {discord_id} removed from room {room_id}. Saving state.")
            await self._save_game_state(room_id, game_state)
            return True, "Player left successfully."
        else:
            logger.warning(f"[_leave_player] Player {discord_id} not found in room {room_id}.")
            return False, "Player not found in this game."

    async def _start_new_game(self, room_id: str, guild_id: str = None, channel_id: str = None) -> tuple[bool, str]:
        """Resets the game state to start a new game in the specified room."""
        logger.info(f"[_start_new_game] Starting new game for room {room_id}.")
        # Load current state to preserve guild/channel IDs, passing them to _load_game_state
        game_state = await self._load_game_state(room_id, guild_id, channel_id)

        new_deck = Deck()
        new_deck.build()
        new_deck.shuffle()

        # Reset relevant game state variables
        game_state['current_round'] = 'pre_game'
        game_state['deck'] = new_deck.to_output_format()
        game_state['board_cards'] = []
        game_state['dealer_hand'] = [] # Clear dealer's hand
        game_state['last_evaluation'] = None
        game_state['current_player_turn_index'] = -1
        game_state['current_betting_round_pot'] = 0
        game_state['current_round_min_bet'] = 0
        game_state['last_aggressive_action_player_id'] = None
        game_state['timer_end_time'] = None # No timer in pre_game
        # game_state['dealer_button_position'] is not reset here, it rotates per round

        # Clear players' hands and reset betting/folded status, but keep players in their seats
        for player in game_state['players']:
            player['hand'] = []
            player['current_bet_in_round'] = 0
            player['has_acted_in_round'] = False
            player['folded'] = False

        logger.info(f"[_start_new_game] Game state reset for room {room_id}. Saving state.")
        await self._save_game_state(room_id, game_state)
        return True, "New game started successfully."

    async def _start_new_round_pre_flop(self, room_id: str, guild_id: str = None, channel_id: str = None) -> tuple[bool, str]:
        """
        Starts a new round, dealing hole cards to players and two cards to the dealer,
        and sets the round to 'pre_flop'.
        """
        logger.info(f"[_start_new_round_pre_flop] Starting new round pre-flop for room {room_id}.")
        
        # 1. Reset the game state like _start_new_game (clears hands, resets betting states, etc.)
        success_reset, message_reset = await self._start_new_game(room_id, guild_id, channel_id)
        if not success_reset:
            return False, f"Failed to reset game for new round: {message_reset}"

        # Reload game_state after reset to get the latest version
        game_state = await self._load_game_state(room_id)

        # 2. Rotate dealer button
        sorted_players = self._get_sorted_players(game_state)
        if len(sorted_players) == 0: # If no players are seated, cannot start a round.
            return False, "Cannot start new round, no players available."
        
        # If there's at least one player, proceed with rotation.
        game_state['dealer_button_position'] = (game_state['dealer_button_position'] + 1) % len(sorted_players)
        logger.info(f"Dealer button moved to player at index {game_state['dealer_button_position']}")
        

        # 3. Deal hole cards to players
        success_players, message_players = await self.deal_hole_cards(room_id)
        if not success_players:
            return False, f"Failed to deal hole cards: {message_players}"

        # 4. Deal two cards to the dealer
        success_dealer, message_dealer = await self.deal_dealer_cards(room_id)
        if not success_dealer:
            return False, f"Failed to deal dealer cards: {message_dealer}"

        # Reload game_state after card deals
        game_state = await self._load_game_state(room_id)

        # 5. Set the current round to 'pre_flop'
        game_state['current_round'] = 'pre_flop'
        logger.info(f"[_start_new_round_pre_flop] Set current_round to {game_state['current_round']} for room {room_id}.")
        
        # 6. Start the first betting round (applies blinds and sets first player turn)
        await self._start_betting_round(room_id, game_state) # This will also save the state and set the timer

        logger.info(f"[_start_new_round_pre_flop] New round for room {room_id} successfully moved to pre_flop.")
        
        return True, "New round started, hole cards and dealer cards dealt, moved to pre_flop."

    async def _handle_in_game_message(self, room_id: str, sender_id: str, message_content: str) -> tuple[bool, str, dict]:
        """
        Handles an in-game message by echoing it back.
        In a real game, this might involve more complex logic like
        broadcasting to other players, logging, or checking for commands.
        """
        logger.info(f"[_handle_in_game_message] Received message for room {room_id} from {sender_id}: '{message_content}'")

        # Load game state to get sender's name (optional but good for context)
        game_state = await self._load_game_state(room_id)
        sender_name = "Unknown Player"
        for player in game_state.get('players', []):
            if player['discord_id'] == sender_id:
                sender_name = player['name']
                break

        response_data = {
            "status": "success",
            "message": "Message received and echoed.",
            "echo_message": {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "content": message_content
            },
            "game_state": game_state # Include the current game state
        }
        return True, "Message processed." , response_data


# The setup function is needed for bot.py to load this as a cog.
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
