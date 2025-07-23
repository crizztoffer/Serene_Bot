import logging
import json
import aiomysql 
from discord.ext import commands # Reverted to commands.Cog structure to be loadable by bot.py

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
        """
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                # Querying 'bot_game_rooms' table and using 'game_state' column
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE room_id = %s",
                    (room_id,)
                )
                result = await cursor.fetchone()
                if result and result['game_state']: # Accessing by 'game_state'
                    # Decode the JSON string from the database
                    return json.loads(result['game_state'])
                else:
                    logger.warning(f"No existing game state found for room_id: {room_id} in bot_game_rooms. Initializing new state.")
                    # Return a basic initial state if not found
                    new_deck = Deck() # Use Deck from game_models
                    new_deck.build()
                    new_deck.shuffle()
                    return {
                        'room_id': room_id,
                        'guild_id': guild_id,   # IMPORTANT: Include guild_id
                        'channel_id': channel_id, # IMPORTANT: Include channel_id
                        'current_round': 'pre_game',
                        'players': [], # Players will be added/updated by frontend/game logic
                        'deck': new_deck.to_output_format(), # Fresh, shuffled deck output format
                        'board_cards': [],
                        'last_evaluation': None
                    }
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
                # Updating 'bot_game_rooms' table and using 'game_state' column
                await cursor.execute(
                    "INSERT INTO bot_game_rooms (room_id, game_state) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE game_state = %s",
                    (room_id, game_state_json, game_state_json)
                )
                logger.info(f"Game state saved for room_id: {room_id} in bot_game_rooms.")
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
        game_state['current_round'] = "pre_flop"

        await self._save_game_state(room_id, game_state)
        return True, "Hole cards dealt."

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
                success, message = await self._add_player_to_game(room_id, player_data)
            else:
                logger.warning(f"Received unsupported action: {action}")
                return {"status": "error", "message": "Unsupported action"}, 400

            # After any action, load the latest state to return it
            if success:
                # Re-load the state to ensure it's the absolute latest from DB, including any updates
                # from the action itself (e.g., player additions, card deals).
                # This re-load will now correctly include guild_id/channel_id if it was a new game.
                updated_game_state = await self._load_game_state(room_id, guild_id, channel_id) 
                return updated_game_state, 200
            else:
                return {"status": "error", "message": message}, 500

        except Exception as e:
            logger.error(f"Error processing action '{action}' for room {room_id}: {e}", exc_info=True)
            return {"status": "error", "message": f"Server error: {e}"}, 500

    async def _add_player_to_game(self, room_id: str, player_data: dict) -> tuple[bool, str]:
        """
        Adds a player to the game state for a given room_id, including their chosen seat_id.
        Ensures a player cannot sit in an occupied seat or sit in multiple seats.
        """
        logger.info(f"[_add_player_to_game] Attempting to add player for room {room_id} with data: {player_data}")
        game_state = await self._load_game_state(room_id) # This will now load with guild_id/channel_id if new
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
                logger.warning(f"[_add_player_to_game] Player {player_name} is trying to sit in seat {seat_id} but is already in seat {existing_player.get('seat_id', 'an unknown seat')}.")
                return False, f"Player {player_name} is already seated elsewhere. Please leave your current seat first."

        # Check if the target seat is already occupied by *any* player
        if any(p.get('seat_id') == seat_id for p in players):
            logger.warning(f"[_add_player_to_game] Seat {seat_id} is already occupied.")
            return False, f"Seat {seat_id} is already occupied by another player."

        # Add new player with seat_id, ensuring hand is empty initially
        new_player = {
            'discord_id': player_discord_id,
            'name': player_name,
            'hand': [],
            'seat_id': seat_id # Store the chosen seat ID
        }
        players.append(new_player)
        game_state['players'] = players
        
        logger.info(f"[_add_player_to_game] New player {player_name} added to game state, saving...")
        await self._save_game_state(room_id, game_state)
        logger.info(f"[_add_player_to_game] Player {player_name} added to seat {seat_id} in room {room_id}. State saved.")
        return True, "Player added successfully."


# The setup function is needed for bot.py to load this as a cog.
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
