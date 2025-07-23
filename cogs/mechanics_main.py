# cogs/mechanics_main.py
import logging
import random
import itertools # For poker hand evaluation
import json
import aiohttp # For making webhooks

logger = logging.getLogger(__name__)

# --- Card and Deck Classes ---
class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank # e.g., "2", "3", ..., "0", "J", "Q", "K", "A"

    def __str__(self):
        return f"{self.rank}{self.suit[0].upper()}" # e.g., "KH" for King of Hearts, "0S" for 10 of Spades

    def to_dict(self):
        return {"suit": self.suit, "rank": self.rank}

    @staticmethod
    def from_dict(card_dict):
        """Creates a Card object from its dictionary representation."""
        return Card(card_dict['suit'], card_dict['rank'])

class Deck:
    def __init__(self, cards=None):
        """
        Initializes a Deck. If 'cards' is provided (from a serialized state),
        it reconstructs the deck from those cards. Otherwise, it builds a new one.
        """
        if cards is None:
            self.cards = []
            self.build()
        else:
            # Ensure cards are converted to Card objects from their dict representation
            self.cards = [Card.from_dict(c) for c in cards]

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

    def to_dict(self):
        """Converts the deck to a list of dictionaries for serialization."""
        return [card.to_dict() for card in self.cards]

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

    # Generate all 5-card combinations from the 7 available cards
    best_hand_type = "High Card"
    best_hand_value = 0 # Represents the highest card in the best hand for simplified comparison

    # This part needs a proper poker hand ranking algorithm
    # For a real game, you'd iterate through all 5-card combinations
    # and use a robust comparison function to find the absolute best hand.
    # The current implementation is a very basic demonstration.

    # Example: Just checking for a few basic types from the whole 7 cards, not combinations
    # This is HIGHLY inaccurate for real poker.
    
    # Simplified check for Flush (find if any 5 cards of same suit exist)
    suit_groups = {}
    for r_val, suit_char in processed_cards:
        suit_groups.setdefault(suit_char, []).append(r_val)
    for suit_char, ranks_in_suit in suit_groups.items():
        if len(ranks_in_suit) >= 5:
            # Simple flush found, use highest card in that suit for value
            return "Flush", max(ranks_in_suit)

    # Simplified check for Straight (find if any 5 consecutive ranks exist)
    unique_ranks = sorted(list(set([c[0] for c in processed_cards])), reverse=True)
    # Handle Ace-low straight (5,4,3,2,A)
    if 14 in unique_ranks and 2 in unique_ranks and 3 in unique_ranks and 4 in unique_ranks and 5 in unique_ranks:
        return "Straight", 5 # Value for A-5 straight

    for i in range(len(unique_ranks) - 4):
        is_straight = True
        for j in range(4):
            if unique_ranks[i+j] - unique_ranks[i+j+1] != 1:
                is_straight = False
                break
        if is_straight:
            return "Straight", unique_ranks[i] # Highest card in straight

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
        return "Full House", trips[0] # Primary value by trips, secondary by pair
    if trips:
        return "Three of a Kind", trips[0]
    if len(pairs) >= 2:
        return "Two Pair", pairs[0] # Highest pair
    if pairs:
        return "One Pair", pairs[0]
    
    return "High Card", processed_cards[0][0] # Default to highest card


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot # The bot instance is still passed but not used for Discord communication from this cog

    async def cog_load(self):
        logger.info("MechanicsMain cog loaded successfully.")

    async def cog_unload(self):
        logger.info("MechanicsMain cog unloaded.")

    # Removed: _send_discord_message as this cog should not communicate with Discord directly.

    async def _send_game_state_webhook(self, url: str, game_state: dict):
        """Sends the updated game state to a PHP webhook endpoint."""
        if not url:
            logger.warning("Webhook URL is not provided. Cannot send game state update.")
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=game_state) as response:
                    response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                    logger.info(f"Successfully sent game state update to webhook {url}. Status: {response.status}")
        except aiohttp.ClientError as e:
            logger.error(f"Failed to send game state webhook to {url}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error sending webhook to {url}: {e}", exc_info=True)

    def _get_player_by_id(self, players_data: list, discord_id: str):
        """Helper to find a player dict by discord_id."""
        for player in players_data:
            if player.get('discord_id') == discord_id:
                return player
        return None

    def _get_player_name_by_id(self, players_data: list, discord_id: str):
        """Helper to get a player's name by discord_id."""
        player = self._get_player_by_id(players_data, discord_id)
        return player.get('name', 'Unknown Player') if player else 'Unknown Player'

    async def deal_hole_cards(self, room_id: str, guild_id: str, channel_id: str, game_state: dict):
        """
        Deals two hole cards to each player.
        Args:
            room_id (str): The ID of the game room.
            guild_id (str): The Discord guild ID. (Still passed, but not used for Discord comms)
            channel_id (str): The Discord channel ID. (Still passed, but not used for Discord comms)
            game_state (dict): The current game state from PHP.
        Returns:
            tuple: (updated_game_state, discord_message, success, message)
            Note: discord_message will now always be None or an empty string.
        """
        try:
            # Reconstruct Deck object from its dictionary representation
            deck = Deck(game_state.get('deck', [])) # Pass an empty list if 'deck' is missing
            deck.shuffle()
            players_data = game_state.get('players', [])

            for player in players_data:
                player['hand'] = [] # Clear existing hands
                card1 = deck.deal_card()
                card2 = deck.deal_card()
                if card1 and card2:
                    player['hand'].append(card1.to_dict())
                    player['hand'].append(card2.to_dict())
                else:
                    return game_state, None, False, "Not enough cards." # No Discord message

            game_state['deck'] = deck.to_dict() # Convert Deck object back to dictionary for serialization
            game_state['players'] = players_data
            game_state['board_cards'] = [] # Ensure board is empty
            game_state['current_round'] = "pre_flop"

            # Removed Discord message generation
            discord_message = None # This cog does not send Discord messages

            return game_state, discord_message, True, "Hole cards dealt."
        except Exception as e:
            logger.error(f"Error dealing hole cards for room {room_id}: {e}", exc_info=True)
            return game_state, None, False, f"Failed to deal hole cards: {e}"

    async def deal_flop(self, room_id: str, guild_id: str, channel_id: str, game_state: dict):
        """
        Deals the three community cards (flop).
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_obj = [Card.from_dict(c) for c in game_state.get('board_cards', [])]

            # Burn a card (standard poker practice)
            deck.deal_card()

            flop_cards = []
            for _ in range(3):
                card = deck.deal_card()
                if card:
                    flop_cards.append(card)
                    board_cards_obj.append(card)
                else:
                    return game_state, None, False, "Not enough cards for flop."

            game_state['deck'] = deck.to_dict()
            game_state['board_cards'] = [c.to_dict() for c in board_cards_obj]
            game_state['current_round'] = "flop"

            # Removed Discord message generation
            discord_message = None

            return game_state, discord_message, True, "Flop dealt."
        except Exception as e:
            logger.error(f"Error dealing flop for room {room_id}: {e}", exc_info=True)
            return game_state, None, False, f"Failed to deal flop: {e}"

    async def deal_turn(self, room_id: str, guild_id: str, channel_id: str, game_state: dict):
        """
        Deals the fourth community card (turn).
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_obj = [Card.from_dict(c) for c in game_state.get('board_cards', [])]

            # Burn a card
            deck.deal_card()

            turn_card = deck.deal_card()
            if turn_card:
                board_cards_obj.append(turn_card)
            else:
                return game_state, None, False, "Not enough cards for turn."

            game_state['deck'] = deck.to_dict()
            game_state['board_cards'] = [c.to_dict() for c in board_cards_obj]
            game_state['current_round'] = "turn"

            # Removed Discord message generation
            discord_message = None

            return game_state, discord_message, True, "Turn dealt."
        except Exception as e:
            logger.error(f"Error dealing turn for room {room_id}: {e}", exc_info=True)
            return game_state, None, False, f"Failed to deal turn: {e}"

    async def deal_river(self, room_id: str, guild_id: str, channel_id: str, game_state: dict):
        """
        Deals the fifth and final community card (river).
        """
        try:
            deck = Deck(game_state.get('deck', []))
            board_cards_obj = [Card.from_dict(c) for c in game_state.get('board_cards', [])]

            # Burn a card
            deck.deal_card()

            river_card = deck.deal_card()
            if river_card:
                board_cards_obj.append(river_card)
            else:
                return game_state, None, False, "Not enough cards for river."

            game_state['deck'] = deck.to_dict()
            game_state['board_cards'] = [c.to_dict() for c in board_cards_obj]
            game_state['current_round'] = "river"

            # Removed Discord message generation
            discord_message = None

            return game_state, discord_message, True, "River dealt."
        except Exception as e:
            logger.error(f"Error dealing river for room {room_id}: {e}", exc_info=True)
            return game_state, None, False, f"Failed to deal river: {e}"

    async def evaluate_hands(self, room_id: str, guild_id: str, channel_id: str, game_state: dict):
        """
        Evaluates all players' hands against the community cards to determine the winner(s).
        This will use a very simplified hand evaluation.
        """
        try:
            players_data = game_state.get('players', [])
            board_cards_dicts = game_state.get('board_cards', [])
            board_cards_obj = [Card.from_dict(c) for c in board_cards_dicts]

            if len(board_cards_obj) != 5:
                return game_state, None, False, "Board not complete."

            player_evaluations = []
            for player_data in players_data:
                player_hand_obj = [Card.from_dict(c) for c in player_data.get('hand', [])]
                combined_cards = player_hand_obj + board_cards_obj
                hand_type, hand_value = evaluate_poker_hand(combined_cards) # Simplified evaluation
                player_evaluations.append({
                    "discord_id": player_data['discord_id'],
                    "name": player_data['name'],
                    "hand_type": hand_type,
                    "hand_value": hand_value,
                    "hole_cards": ", ".join(str(c) for c in player_hand_obj)
                })

            # Determine winner(s) based on simplified evaluation
            # This logic needs to be robust for actual poker.
            if not player_evaluations:
                winner_message_content = "No players to evaluate."
            else:
                best_hand_value = -1
                best_hand_type = ""
                winners = []

                for eval_data in player_evaluations:
                    if eval_data['hand_value'] > best_hand_value:
                        best_hand_value = eval_data['hand_value']
                        best_hand_type = eval_data['hand_type']
                        winners = [eval_data]
                    elif eval_data['hand_value'] == best_hand_value and eval_data['hand_type'] == best_hand_type:
                        winners.append(eval_data)

                winner_names = ", ".join([w['name'] for w in winners])
                board_display = ", ".join(str(c) for c in board_cards_obj)

                # Removed Discord message generation specific to showdown
                winner_message_content = f"Showdown for Room ID: {room_id}. Winner(s): {winner_names} with a {best_hand_type}!"


            game_state['current_round'] = "showdown"
            game_state['last_evaluation'] = player_evaluations # Store for PHP

            return game_state, None, True, "Hands evaluated." # No Discord message
        except Exception as e:
            logger.error(f"Error evaluating hands for room {room_id}: {e}", exc_info=True)
            return game_state, None, False, f"Failed to evaluate hands: {e}"


    # --- Central Web Request Handler for the Cog ---
    async def handle_web_game_action(self, request_data: dict, webhook_url: str):
        """
        Receives raw request data from the web server (bot.py) and dispatches it
        to the appropriate game action method within the cog.
        This method will also handle sending the PHP webhook.

        Args:
            request_data (dict): The JSON payload from the web request.
            webhook_url (str): The URL where to send the updated game state back to PHP.

        Returns:
            tuple: (response_payload: dict, http_status_code: int)
        """
        action = request_data.get('action')
        room_id = request_data.get('room_id')
        guild_id = request_data.get('guild_id') # Still received, but not used for Discord comms
        channel_id = request_data.get('channel_id') # Still received, but not used for Discord comms
        current_game_state = request_data.get('game_state')

        if not all([action, room_id, guild_id, channel_id, current_game_state]):
            logger.error(f"Missing required parameters for handle_web_game_action. Data: {request_data}")
            return {"status": "error", "message": "Missing parameters"}, 400

        logger.info(f"Cog received game action: '{action}' for Room ID: {room_id}")

        updated_game_state = current_game_state
        # Removed discord_message variable, as it's no longer needed for direct Discord communication
        success = False
        message = "Unknown game action."

        if action == "deal_hole_cards":
            updated_game_state, _, success, message = await self.deal_hole_cards( # Use _ for unused discord_message
                room_id, guild_id, channel_id, current_game_state
            )
        elif action == "deal_flop":
            updated_game_state, _, success, message = await self.deal_flop(
                room_id, guild_id, channel_id, current_game_state
            )
        elif action == "deal_turn":
            updated_game_state, _, success, message = await self.deal_turn(
                room_id, guild_id, channel_id, current_game_state
            )
        elif action == "deal_river":
            updated_game_state, _, success, message = await self.deal_river(
                room_id, guild_id, channel_id, current_game_state
            )
        elif action == "evaluate_hands":
            updated_game_state, _, success, message = await self.evaluate_hands(
                room_id, guild_id, channel_id, current_game_state
            )
        else:
            logger.warning(f"Received unsupported game action: {action}")
            return {"status": "error", "message": "Unsupported game action"}, 400

        if success:
            # Removed: Send update to Discord (as per user's request)
            # if discord_message:
            #     await self._send_discord_message(channel_id, discord_message)

            # Send updated game state back to PHP via webhook
            if updated_game_state and webhook_url:
                await self._send_game_state_webhook(webhook_url, updated_game_state)
            elif not webhook_url:
                logger.warning("GAME_WEBHOOK_URL is not set. Cannot send game state update to PHP.")

            return {"status": "success", "message": message, "game_state": updated_game_state}, 200
        else:
            return {"status": "error", "message": message, "game_state": updated_game_state}, 500


    # Removed: All Discord Commands as this cog should not initiate Discord interactions.
    # @commands.command(name="pokernewgame")
    # async def poker_new_game_command(self, ctx):
    #     """
    #     Discord command to initiate a new poker game session.
    #     This will trigger the process that sends the button to Discord.
    #     """
    #     try:
    #         from cogs.games.Serene_Texas_Hold_Em import start_game_session
    #         await start_game_session(ctx, self.bot)
    #     except ImportError:
    #         logger.error("Could not import start_game_session from cogs.games.Serene_Texas_Hold_Em. Make sure file exists and is named correctly (Serene_Texas_Hold_Em.py).")
    #         await ctx.send("Error: Poker game initiation function not found. Is the Texas Hold 'Em cog loaded correctly?")
    #     except Exception as e:
    #         logger.error(f"Error initiating poker game session: {e}", exc_info=True)
    #         await ctx.send(f"Failed to start poker game session: {e}. Please check logs.")


async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
