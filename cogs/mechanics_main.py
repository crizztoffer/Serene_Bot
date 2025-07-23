# cogs/mechanics_main.py
import logging
import random
import json
import os # Still imported, but primarily for initial setup/fallback if needed
import aiomysql # For database interaction

# Removed discord.ext.commands as this cog will not use Discord functionality directly
# It will still be loaded as a cog by bot.py, but its internal logic is now decoupled.

logger = logging.getLogger(__name__)

# --- Card and Deck Classes ---
class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank # e.g., "2", "3", ..., "0", "J", "Q", "K", "A"

    def __str__(self):
        # This is the two-character code for display/transfer
        return f"{self.rank}{self.suit[0].upper()}"

    def to_output_format(self):
        """Returns the card in the desired two-character output format."""
        return str(self)

    @staticmethod
    def from_output_format(card_str: str):
        """Reconstructs a Card object from its two-character string format."""
        if len(card_str) < 2:
            raise ValueError(f"Invalid card string format: {card_str}")
        
        rank_char = card_str[:-1]
        suit_char = card_str[-1].lower()

        # Map suit character back to full suit name
        suit_map = {'h': 'Hearts', 'd': 'Diamonds', 'c': 'Clubs', 's': 'Spades'}
        suit = suit_map.get(suit_char)
        if not suit:
            raise ValueError(f"Invalid suit character: {suit_char} in {card_str}")

        return Card(suit, rank_char)

class Deck:
    def __init__(self, cards_data=None):
        """
        Initializes a Deck. If 'cards_data' is provided (from a serialized state,
        expected as a list of two-character strings), it reconstructs the deck.
        Otherwise, it builds a new one.
        """
        if cards_data is None:
            self.cards = []
            self.build()
        else:
            # Reconstruct Card objects from their two-character string representation
            self.cards = [Card.from_output_format(c_str) for c_str in cards_data]

    def build(self):
        """Builds a standard 52-card deck."""
        suits = ["Hearts", "Diamonds", "Clubs", "Spades"]
        # "10" is represented as "0" as per user's requirement for 2-character generation
        ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "0", "J", "Q", "K", "A"]
        self.cards = [Card(suit, rank) for suit in suits for rank in ranks]

    def shuffle(self):
        """Shuffles the deck."""
        random.shuffle(self.cards)

    def deal_card(self):
        """Deals a single card from the top of the deck."""
        if not self.cards:
            logger.warning("Deck is empty. Cannot deal more cards.")
            return None
        return self.cards.pop()

    def to_output_format(self):
        """Converts the deck to a list of two-character strings for serialization."""
        return [card.to_output_format() for card in self.cards]

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


# Reverted to commands.Cog structure to be loadable by bot.py
from discord.ext import commands 

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

    async def _load_game_state(self, room_id: str) -> dict:
        """Loads the game state for a given room_id from the database."""
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
                    # Ensure a new deck is built and shuffled correctly for the initial state
                    new_deck = Deck()
                    new_deck.build()
                    new_deck.shuffle()
                    return {
                        'room_id': room_id,
                        # 'game_type': '1', # REMOVED: game_type is not part of the dynamic game_state JSON
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
                # MODIFIED: Updating 'bot_game_rooms' table and using 'game_state' column
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
        game_state = await self._load_game_state(room_id)
        
        # Ensure players list is not empty for dealing
        if not game_state.get('players'):
            return False, "No players in the game to deal cards."

        deck = Deck(game_state.get('deck', []))
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

        deck = Deck(game_state.get('deck', []))
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

        deck = Deck(game_state.get('deck', []))
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

        deck = Deck(game_state.get('deck', []))
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
        board_cards_obj = [Card.from_output_format(c_str) for c_str in game_state.get('board_cards', [])]

        if len(board_cards_obj) != 5:
            logger.error("Board not complete for evaluation.")
            return False, "Board not complete."

        player_evaluations = []
        for player_data in players_data:
            player_hand_obj = [Card.from_output_format(c_str) for c_str in player_data.get('hand', [])]
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
        
        if not all([action, room_id]):
            logger.error(f"Missing required parameters for handle_web_game_action. Data: {request_data}")
            return {"status": "error", "message": "Missing room_id or action."}, 400

        logger.info(f"Backend dealer received action: '{action}' for Room ID: {room_id}")

        success = False
        message = "Unknown action."
        updated_game_state = None # Will hold the state after action

        try:
            if action == "deal_hole_cards":
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
            elif action == "get_state": # New action to simply get the current state
                success = True
                message = "Game state retrieved."
            else:
                logger.warning(f"Received unsupported action: {action}")
                return {"status": "error", "message": "Unsupported action"}, 400

            # After any action, load the latest state to return it
            if success:
                updated_game_state = await self._load_game_state(room_id)
                return updated_game_state, 200
            else:
                return {"status": "error", "message": message}, 500

        except Exception as e:
            logger.error(f"Error processing action '{action}' for room {room_id}: {e}", exc_info=True)
            return {"status": "error", "message": f"Server error: {e}"}, 500

    async def _add_player_to_game(self, room_id: str, player_data: dict) -> tuple[bool, str]:
        """Adds a player to the game state for a given room_id."""
        game_state = await self._load_game_state(room_id)
        players = game_state.get('players', [])

        # Check if player already exists
        if any(p['discord_id'] == player_data['discord_id'] for p in players):
            return False, "Player already in this game."

        # Add new player, ensuring hand is empty initially
        new_player = {
            'discord_id': player_data['discord_id'],
            'name': player_data['name'],
            'hand': []
        }
        players.append(new_player)
        game_state['players'] = players
        
        await self._save_game_state(room_id, game_state)
        return True, "Player added successfully."


# The setup function is needed for bot.py to load this as a cog.
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
