import logging
import json
import aiomysql
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
    elif rank == 'K': return 13
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
                        'current_round': 'pre_game',
                        'players': [],
                        'dealer_hand': [], # Initialize dealer's hand
                        'deck': new_deck.to_output_format(),
                        'board_cards': [],
                        'last_evaluation': None
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

        await self._save_game_state(room_id, game_state)
        return True, "Hands evaluated."

    # --- Central Web Request Handler for the State-Managing Dealer ---
    async def handle_web_game_action(self, request_data: dict) -> tuple[dict, int]:
        """
        Receives raw request data from the web server (bot.py) and dispatches it
        to the appropriate game action method. It loads and saves the game state internally.

        Args:
            request_data (dict): The JSON payload from the web request,
                                 now containing only room_id, action, etc.
                                 (NOT the full game_state).

        Returns:
            tuple: (response_payload: dict, http_status_code: int)
        """
        action = request_data.get('action')
        room_id = request_data.get('room_id')
        guild_id = request_data.get('guild_id')    # Extract guild_id from request
        channel_id = request_data.get('channel_id') # Extract channel_id from request
        
        if not all([action, room_id, guild_id, channel_id]): # Validate all required fields
            logger.error(f"Missing required parameters for handle_web_game_action. Data: {request_data}")
            return {"status": "error", "message": "Missing action, room_id, guild_id, or channel_id."}, 400

        logger.info(f"Backend dealer received action: '{action}' for Room ID: {room_id}, Guild ID: {guild_id}, Channel ID: {channel_id}")

        success = False
        message = "Unknown action."
        updated_game_state = None # Will hold the state after action

        try:
            # Pass guild_id and channel_id to _load_game_state
            # This ensures that if a new game state is initialized, it includes these IDs.
            if action == "get_state":
                updated_game_state = await self._load_game_state(room_id, guild_id, channel_id)
                success = True
                message = "Game state retrieved."
            elif action == "deal_hole_cards":
                success, message = await self.deal_hole_cards(room_id)
            elif action == "deal_dealer_cards": # New action handler
                success, message = await self.deal_dealer_cards(room_id)
            elif action == "deal_flop":
                success, message = await self.deal_flop(room_id)
            elif action == "deal_turn":
                success, message = await self.deal_turn(room_id)
            elif action == "deal_river":
                success, message = await self.deal_river(room_id)
            elif action == "evaluate_hands":
                success, message = await self.evaluate_hands(room_id)
            elif action == "add_player": # New action to add players to a game
                player_data = request_data.get('player_data')
                if not player_data or not isinstance(player_data, dict):
                    return {"status": "error", "message": "Missing or invalid player_data for add_player."}, 400
                # Pass guild_id and channel_id to _add_player_to_game if it needs to initialize a new game state
                success, message = await self._add_player_to_game(room_id, player_data, guild_id, channel_id)
            elif action == "leave_player": # New action to remove a player
                discord_id = request_data.get('discord_id')
                if not discord_id:
                    return {"status": "error", "message": "Missing discord_id for leave_player."}, 400
                success, message = await self._leave_player(room_id, discord_id)
            elif action == "start_new_game": # Action to start a new game (resets state)
                success, message = await self._start_new_game(room_id, guild_id, channel_id) # Pass guild/channel for new game init
            elif action == "start_new_round_pre_flop": # New action to start a new round pre-flop
                success, message = await self._start_new_round_pre_flop(room_id, guild_id, channel_id)
            elif action == "send_message":  # New action for in-game messages
                message_content = request_data.get('message_content')
                sender_id = request_data.get('sender_id')
                if not message_content or not sender_id:
                    return {"status": "error", "message": "Missing message_content or sender_id for send_message."}, 400
                success, message, response_data = await self._handle_in_game_message(room_id, sender_id, message_content)
                if success:
                    return response_data, 200 # Return the specific message data
                else:
                    return {"status": "error", "message": message}, 500
            else:
                logger.warning(f"Received unsupported action: {action}")
                return {"status": "error", "message": "Unsupported action"}, 400

            # After any action, load the latest state to return it
            if success and action != "send_message": # Don't reload state if it's just a message echo
                # Re-load the state to ensure it's the absolute latest from DB, including any updates
                # from the action itself (e.g., player additions, card deals).
                # This re-load will now correctly include guild_id/channel_id if it was a new game.
                updated_game_state = await self._load_game_state(room_id, guild_id, channel_id)
                return updated_game_state, 200
            elif success and action == "send_message":
                 return response_data, 200 # Already handled for send_message
            else:
                return {"status": "error", "message": message}, 500

        except Exception as e:
            logger.error(f"Error processing action '{action}' for room {room_id}: {e}", exc_info=True)
            return {"status": "error", "message": f"Server error: {e}"}, 500

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
                'avatar_url': player_data.get('avatar_url') # Store avatar URL if provided
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
        
        # Clear players' hands, but keep players in their seats
        for player in game_state['players']:
            player['hand'] = []

        logger.info(f"[_start_new_game] Game state reset for room {room_id}. Saving state.")
        await self._save_game_state(room_id, game_state)
        return True, "New game started successfully."

    async def _start_new_round_pre_flop(self, room_id: str, guild_id: str = None, channel_id: str = None) -> tuple[bool, str]:
        """
        Starts a new round, dealing hole cards to players and two cards to the dealer,
        and sets the round to 'pre_flop'.
        """
        logger.info(f"[_start_new_round_pre_flop] Starting new round pre-flop for room {room_id}.")
        
        # 1. Reset the game state like _start_new_game
        success_reset, message_reset = await self._start_new_game(room_id, guild_id, channel_id)
        if not success_reset:
            return False, f"Failed to reset game for new round: {message_reset}"

        # 2. Deal hole cards to players
        success_players, message_players = await self.deal_hole_cards(room_id)
        if not success_players:
            return False, f"Failed to deal hole cards: {message_players}"

        # 3. Deal two cards to the dealer
        success_dealer, message_dealer = await self.deal_dealer_cards(room_id)
        if not success_dealer:
            return False, f"Failed to deal dealer cards: {message_dealer}"

        # 4. Set the current round to 'pre_flop'
        game_state = await self._load_game_state(room_id)
        game_state['current_round'] = 'pre_flop'
        await self._save_game_state(room_id, game_state)
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
        return True, "Message processed.", response_data


# The setup function is needed for bot.py to load this as a cog.
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
