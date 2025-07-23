# cogs/mechanics_main.py
import logging
import random
import itertools # For poker hand evaluation
import json
import aiohttp # For making webhooks (still needed if PHP webhook is used, but removed from this file's logic)

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
# IMPORTANT: This is a very simplified poker hand evaluator.
# For a real poker game, you would need a much more robust and accurate
# implementation that correctly ranks all 5-card combinations from 7 cards.
# Consider using a dedicated poker evaluation library for production.

def get_rank_value(rank):
    """Returns numerical value for poker ranks for comparison."""
    if rank.isdigit():
        # Handle "0" as 10 for evaluation purposes
        if rank == '0': return 10
        return int(rank)
    elif rank == 'J': return 11
    elif rank == 'Q': return 12
    elif rank == 'K': return 13
    elif rank == 'A': return 14 # Ace high for evaluation
    return 0 # Should not happen with valid ranks

def evaluate_poker_hand(cards):
    """
    Evaluates a 7-card poker hand (5 community + 2 hole) and returns its type and value.
    This is a very simplified placeholder and DOES NOT correctly implement full poker rules.
    It primarily checks for basic hand types and uses the highest card as a tie-breaker.
    """
    if len(cards) < 5:
        return "Not enough cards", 0

    # Convert cards to a format easier for evaluation: [(rank_value, suit_char), ...]
    processed_cards = []
    for card in cards:
        processed_cards.append((get_rank_value(card.rank), card.suit[0].upper()))

    # Simplified check for Flush (find if any 5 cards of same suit exist)
    suit_groups = {}
    for r_val, suit_char in processed_cards:
        suit_groups.setdefault(suit_char, []).append(r_val)
    for suit_char, ranks_in_suit in suit_groups.items():
        if len(ranks_in_suit) >= 5:
            return "Flush", max(ranks_in_suit)

    # Simplified check for Straight (find if any 5 consecutive ranks exist)
    unique_ranks = sorted(list(set([c[0] for c in processed_cards])), reverse=True)
    if 14 in unique_ranks and 2 in unique_ranks and 3 in unique_ranks and 4 in unique_ranks and 5 in unique_ranks:
        return "Straight", 5 # Value for A-5 straight

    for i in range(len(unique_ranks) - 4):
        is_straight = True
        for j in range(4):
            if unique_ranks[i+j] - unique_ranks[i+j+1] != 1:
                is_straight = False
                break
        if is_straight:
            return "Straight", unique_ranks[i]

    # Simplified check for Pairs/Trips/Quads
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
    if trips and pairs: # Full House
        return "Full House", trips[0]
    if trips:
        return "Three of a Kind", trips[0]
    if len(pairs) >= 2:
        return "Two Pair", pairs[0]
    if pairs:
        return "One Pair", pairs[0]
    
    return "High Card", processed_cards[0][0]


class MechanicsMain(object): # Changed to inherit from object, not commands.Cog
    # Removed __init__(self, bot) as bot instance is no longer needed in this pure dealer
    # This class will be instantiated directly by bot.py's handler.

    def __init__(self):
        logger.info("MechanicsMain (pure dealer) initialized.")

    # Removed: _send_discord_message as this cog should not communicate with Discord directly.
    # Removed: _send_game_state_webhook as this cog should not send webhooks to PHP.
    # PHP will poll this directly.

    # Helper functions (kept for internal logic, but not exposed for Discord/PHP)
    def _get_player_by_id(self, players_data: list, discord_id: str):
        for player in players_data:
            if player.get('discord_id') == discord_id:
                return player
        return None

    def _get_player_name_by_id(self, players_data: list, discord_id: str):
        player = self._get_player_by_id(players_data, discord_id)
        return player.get('name', 'Unknown Player') if player else 'Unknown Player'

    async def deal_hole_cards(self, game_state: dict):
        """
        Deals two hole cards to each player within the provided game_state.
        Modifies game_state in place and returns it.
        """
        try:
            deck = Deck(game_state.get('deck', []))
            deck.shuffle()
            players_data = game_state.get('players', [])

            for player in players_data:
                player['hand'] = [] # Clear existing hands
                card1 = deck.deal_card()
                card2 = deck.deal_card()
                if card1 and card2:
                    player['hand'].append(card1.to_output_format()) # Store as 2-char string
                    player['hand'].append(card2.to_output_format()) # Store as 2-char string
                else:
                    logger.error("Not enough cards to deal hole cards.")
                    return False, "Not enough cards."

            game_state['deck'] = deck.to_output_format() # Store as list of 2-char strings
            game_state['players'] = players_data
            game_state['board_cards'] = [] # Ensure board is empty
            game_state['current_round'] = "pre_flop"

            return True, "Hole cards dealt."
        except Exception as e:
            logger.error(f"Error dealing hole cards: {e}", exc_info=True)
            return False, f"Failed to deal hole cards: {e}"

    async def deal_flop(self, game_state: dict):
        """
        Deals the three community cards (flop).
        Modifies game_state in place and returns it.
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_output = game_state.get('board_cards', []) # Expects/stores as 2-char strings

            # Burn a card (standard poker practice)
            deck.deal_card()

            flop_cards_obj = []
            for _ in range(3):
                card = deck.deal_card()
                if card:
                    flop_cards_obj.append(card)
                    board_cards_output.append(card.to_output_format())
                else:
                    logger.error("Not enough cards for flop.")
                    return False, "Not enough cards."

            game_state['deck'] = deck.to_output_format()
            game_state['board_cards'] = board_cards_output
            game_state['current_round'] = "flop"

            return True, "Flop dealt."
        except Exception as e:
            logger.error(f"Error dealing flop: {e}", exc_info=True)
            return False, f"Failed to deal flop: {e}"

    async def deal_turn(self, game_state: dict):
        """
        Deals the fourth community card (turn).
        Modifies game_state in place and returns it.
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_output = game_state.get('board_cards', [])

            # Burn a card
            deck.deal_card()

            turn_card = deck.deal_card()
            if turn_card:
                board_cards_output.append(turn_card.to_output_format())
            else:
                logger.error("Not enough cards for turn.")
                return False, "Not enough cards."

            game_state['deck'] = deck.to_output_format()
            game_state['board_cards'] = board_cards_output
            game_state['current_round'] = "turn"

            return True, "Turn dealt."
        except Exception as e:
            logger.error(f"Error dealing turn: {e}", exc_info=True)
            return False, f"Failed to deal turn: {e}"

    async def deal_river(self, game_state: dict):
        """
        Deals the fifth and final community card (river).
        Modifies game_state in place and returns it.
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_output = game_state.get('board_cards', [])

            # Burn a card
            deck.deal_card()

            river_card = deck.deal_card()
            if river_card:
                board_cards_output.append(river_card.to_output_format())
            else:
                logger.error("Not enough cards for river.")
                return False, "Not enough cards."

            game_state['deck'] = deck.to_output_format()
            game_state['board_cards'] = board_cards_output
            game_state['current_round'] = "river"

            return True, "River dealt."
        except Exception as e:
            logger.error(f"Error dealing river: {e}", exc_info=True)
            return False, f"Failed to deal river: {e}"

    async def evaluate_hands(self, game_state: dict):
        """
        Evaluates all players' hands against the community cards.
        Modifies game_state in place (adds 'last_evaluation') and returns it.
        """
        try:
            players_data = game_state.get('players', [])
            # Convert board_cards from 2-char strings back to Card objects for evaluation
            board_cards_obj = [Card.from_output_format(c_str) for c_str in game_state.get('board_cards', [])]

            if len(board_cards_obj) != 5:
                logger.error("Board not complete for evaluation.")
                return False, "Board not complete."

            player_evaluations = []
            for player_data in players_data:
                # Convert player's hand from 2-char strings back to Card objects for evaluation
                player_hand_obj = [Card.from_output_format(c_str) for c_str in player_data.get('hand', [])]
                combined_cards = player_hand_obj + board_cards_obj
                hand_type, hand_value = evaluate_poker_hand(combined_cards) # Simplified evaluation
                player_evaluations.append({
                    "discord_id": player_data['discord_id'],
                    "name": player_data['name'],
                    "hand_type": hand_type,
                    "hand_value": hand_value,
                    "hole_cards": [c.to_output_format() for c in player_hand_obj] # Store as 2-char string
                })

            game_state['current_round'] = "showdown"
            game_state['last_evaluation'] = player_evaluations # Store for PHP

            return True, "Hands evaluated."
        except Exception as e:
            logger.error(f"Error evaluating hands: {e}", exc_info=True)
            return False, f"Failed to evaluate hands: {e}"


    # --- Central Web Request Handler for the Pure Dealer ---
    async def handle_web_game_action(self, request_data: dict):
        """
        Receives raw request data from the web server (bot.py) and dispatches it
        to the appropriate game action method within this pure dealer.
        It modifies the 'game_state' in place and returns only that updated state.

        Args:
            request_data (dict): The JSON payload from the web request.
            (webhook_url parameter removed as no outgoing webhooks from this module)

        Returns:
            tuple: (response_payload: dict, http_status_code: int)
        """
        action = request_data.get('action')
        # room_id, guild_id, channel_id are received but not used by this pure dealer
        # as it doesn't interact with Discord or manage game rooms itself.
        current_game_state = request_data.get('game_state')

        if not all([action, current_game_state]): # Simplified validation
            logger.error(f"Missing required parameters for handle_web_game_action. Data: {request_data}")
            return {"status": "error", "message": "Missing parameters"}, 400

        logger.info(f"Pure dealer received action: '{action}'")

        # The game_state is modified in place by the dealing functions
        success = False
        message = "Unknown action."

        if action == "deal_hole_cards":
            success, message = await self.deal_hole_cards(current_game_state)
        elif action == "deal_flop":
            success, message = await self.deal_flop(current_game_state)
        elif action == "deal_turn":
            success, message = await self.deal_turn(current_game_state)
        elif action == "deal_river":
            success, message = await self.deal_river(current_game_state)
        elif action == "evaluate_hands":
            success, message = await self.evaluate_hands(current_game_state)
        else:
            logger.warning(f"Received unsupported action: {action}")
            return {"status": "error", "message": "Unsupported action"}, 400

        if success:
            # Return ONLY the updated game_state directly
            return current_game_state, 200
        else:
            return {"status": "error", "message": message}, 500


# Removed Discord Commands as this cog should not initiate Discord interactions.
# Removed setup function, as this is no longer a commands.Cog but a pure Python object.
# The `bot.py` will need to instantiate this class directly in its handler.

# To integrate this with bot.py's `load_cogs` and `get_cog`:
# We need to make it a commands.Cog again, but ensure no Discord methods are called.
# The __init__ will take `bot` but not use it.
# The `handle_web_game_action` will be called directly on an instance.
# Let's revert the class to inherit from commands.Cog and keep setup,
# but ensure no Discord interactions are present in its methods.

from discord.ext import commands # Re-import commands for cog setup

class MechanicsMain(commands.Cog, name="MechanicsMain"): # Reverted to commands.Cog
    def __init__(self, bot):
        self.bot = bot # Bot instance is passed but not used by this pure dealer
        logger.info("MechanicsMain (pure dealer) initialized as a Discord Cog.")

    async def cog_load(self):
        logger.info("MechanicsMain cog loaded successfully.")

    async def cog_unload(self):
        logger.info("MechanicsMain cog unloaded.")

    # ... (all dealing and evaluation methods as above) ...
    # The methods above (deal_hole_cards, deal_flop, etc.) are now instance methods
    # that take `self` but do not use `self.bot` for Discord communication.

    # The handle_web_game_action method remains the same as defined above.
    async def handle_web_game_action(self, request_data: dict):
        # ... (implementation as shown above) ...
        action = request_data.get('action')
        current_game_state = request_data.get('game_state')

        if not all([action, current_game_state]):
            logger.error(f"Missing required parameters for handle_web_game_action. Data: {request_data}")
            return {"status": "error", "message": "Missing parameters"}, 400

        logger.info(f"Pure dealer received action: '{action}'")

        success = False
        message = "Unknown action."

        if action == "deal_hole_cards":
            success, message = await self.deal_hole_cards(current_game_state)
        elif action == "deal_flop":
            success, message = await self.deal_flop(current_game_state)
        elif action == "deal_turn":
            success, message = await self.deal_turn(current_game_state)
        elif action == "deal_river":
            success, message = await self.deal_river(current_game_state)
        elif action == "evaluate_hands":
            success, message = await self.evaluate_hands(current_game_state)
        else:
            logger.warning(f"Received unsupported action: {action}")
            return {"status": "error", "message": "Unsupported action"}, 400

        if success:
            return current_game_state, 200
        else:
            return {"status": "error", "message": message}, 500

# The setup function is needed for bot.py to load this as a cog.
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
