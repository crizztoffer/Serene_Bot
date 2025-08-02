import logging
import json
import aiomysql
import time # Import time for timestamps
from discord.ext import commands
from itertools import combinations

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Texas Hold'em Hand Evaluation Logic (Improved) ---
HAND_RANKINGS = {
    "High Card": 0,
    "One Pair": 1,
    "Two Pair": 2,
    "Three of a Kind": 3,
    "Straight": 4,
    "Flush": 5,
    "Full House": 6,
    "Four of a Kind": 7,
    "Straight Flush": 8,
    "Royal Flush": 9
}

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
    Evaluates the best possible 5-card poker hand from a set of 7 cards.
    Returns a tuple: (hand_name: str, score_vector: tuple[int])
    The score_vector can be used to break ties between equal hands.
    """
    def rank_value(card):
        return get_rank_value(card.rank)

    def is_straight(ranks):
        ranks = sorted(list(set(ranks)), reverse=True)
        # Check for ace-low straight (A, 5, 4, 3, 2)
        if set([14, 2, 3, 4, 5]).issubset(set(ranks)):
            return True, 5
        for i in range(len(ranks) - 4):
            window = ranks[i:i + 5]
            if all(window[j] - window[j+1] == 1 for j in range(4)):
                return True, window[0]
        return False, None

    def classify_hand(hand):
        ranks = sorted([rank_value(c) for c in hand], reverse=True)
        suits = [c.suit[0].upper() for c in hand]

        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        count_groups = sorted(rank_counts.items(), key=lambda x: (-x[1], -x[0]))
        grouped_ranks = [r for r, _ in count_groups]

        is_flush = len(set(suits)) == 1
        straight, high_straight = is_straight(ranks)

        if is_flush and straight:
            if high_straight == 14:
                return "Royal Flush", (HAND_RANKINGS["Royal Flush"],)
            return "Straight Flush", (HAND_RANKINGS["Straight Flush"], high_straight)

        if count_groups[0][1] == 4:
            return "Four of a Kind", (HAND_RANKINGS["Four of a Kind"], count_groups[0][0], grouped_ranks[1])

        if count_groups[0][1] == 3 and count_groups[1][1] >= 2:
            return "Full House", (HAND_RANKINGS["Full House"], count_groups[0][0], count_groups[1][0])

        if is_flush:
            return "Flush", (HAND_RANKINGS["Flush"], *ranks)

        if straight:
            return "Straight", (HAND_RANKINGS["Straight"], high_straight)

        if count_groups[0][1] == 3:
            return "Three of a Kind", (HAND_RANKINGS["Three of a Kind"], count_groups[0][0], *grouped_ranks[1:3])

        if count_groups[0][1] == 2 and count_groups[1][1] == 2:
            return "Two Pair", (HAND_RANKINGS["Two Pair"], count_groups[0][0], count_groups[1][0], grouped_ranks[2])

        if count_groups[0][1] == 2:
            return "One Pair", (HAND_RANKINGS["One Pair"], count_groups[0][0], *grouped_ranks[1:4])

        return "High Card", (HAND_RANKINGS["High Card"], *ranks)

    best_score = (-1,)
    best_hand_name = ""
    for combo in combinations(cards, 5):
        hand_name, score = classify_hand(combo)
        if score > best_score:
            best_score = score
            best_hand_name = hand_name

    return best_hand_name, best_score


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
            autocommit=False, # Changed to False for explicit commit control
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_game_state(self, room_id: str, guild_id: str = None, channel_id: str = None) -> dict:
        """
        Loads the game state for a given room_id from the database.
        If not found, initializes a new state, using provided guild_id.
        Ensures guild_id is always present in the returned state.
        Fetches kekchipz for each player from discord_users table.
        """
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                # Use TRIM to handle potential whitespace issues in the database entry
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE TRIM(room_id) = %s",
                    (room_id,)
                )
                result = await cursor.fetchone()
                
                game_state = {}
                if result and result['game_state']:
                    game_state = json.loads(result['game_state'])
                    logger.info(f"[_load_game_state] Loaded existing game state for room_id: {room_id}. Players count: {len(game_state.get('players', []))}")
                    logger.debug(f"[_load_game_state] Raw DB game_state: {result['game_state']}")
                    
                    # Proactively check for and correct data inconsistency
                    if 'guild_id' not in game_state or game_state['guild_id'] is None:
                        logger.warning(f"[_load_game_state] Correcting missing guild_id for room {room_id}. Value from message: {guild_id}")
                        game_state['guild_id'] = guild_id
                        
                    if 'channel_id' not in game_state or game_state['channel_id'] is None:
                        logger.warning(f"[_load_game_state] Correcting missing channel_id for room {room_id}. Value from message: {channel_id}")
                        game_state['channel_id'] = channel_id
                        
                else:
                    logger.warning(f"[_load_game_state] No existing game state found for room_id: {room_id}. Initializing new state.")
                    # Initialize with basic structure
                    new_deck = Deck()
                    new_deck.build()
                    new_deck.shuffle()
                    game_state = {
                        'room_id': room_id,
                        'current_round': 'pre_game',
                        'players': [],
                        'dealer_hand': [],
                        'deck': new_deck.to_output_format(),
                        'board_cards': [],
                        'last_evaluation': None,
                        'current_player_turn_index': -1,
                        'current_betting_round_pot': 0,
                        'current_round_min_bet': 0,
                        'last_aggressive_action_player_id': None,
                        'timer_end_time': None,
                        'dealer_button_position': 0,
                        'small_blind_amount': 5,
                        'big_blind_amount': 10,
                        'game_started_once': False,
                        'guild_id': guild_id,
                        'channel_id': channel_id # Set channel_id on new state
                    }
    
                # Ensure required fields are present for backward compatibility
                # This is the primary fix to handle old/inconsistent data
                game_state.setdefault('current_player_turn_index', -1)
                game_state.setdefault('current_betting_round_pot', 0)
                game_state.setdefault('current_round_min_bet', 0)
                game_state.setdefault('last_aggressive_action_player_id', None)
                game_state.setdefault('timer_end_time', None)
                game_state.setdefault('dealer_button_position', 0)
                game_state.setdefault('small_blind_amount', 5)
                game_state.setdefault('big_blind_amount', 10)
            
                # Fetch kekchipz for each player using guild_id instead of channel_id
                # And refresh their display name and avatar from Discord
                guild = self.bot.get_guild(int(guild_id)) if guild_id else None
                for player in game_state.get('players', []):
                    player_discord_id = player['discord_id']
                    if game_state['guild_id'] and player_discord_id:
                        await cursor.execute(
                            "SELECT kekchipz FROM discord_users WHERE discord_id = %s AND guild_id = %s",
                            (player_discord_id, game_state['guild_id'])
                        )
                        kekchipz_result = await cursor.fetchone()
                        if kekchipz_result and 'kekchipz' in kekchipz_result:
                            player['kekchipz_overall'] = kekchipz_result['kekchipz']
                            logger.debug(f"[_load_game_state] Fetched kekchipz {player['kekchipz_overall']} for player {player_discord_id}.")
                        else:
                            player['kekchipz_overall'] = 0
                            logger.warning(f"[_load_game_state] Kekchipz not found for player {player_discord_id} in guild {game_state['guild_id']}. Setting to 0.")
                    else:
                        player['kekchipz_overall'] = 0
                        logger.warning(f"[_load_game_state] Missing guild_id or discord_id for player {player_discord_id}. Cannot fetch kekchipz. Setting to 0.")
    
                    # --- NEWLY ADDED: Refresh player name and avatar from Discord ---
                    if guild:
                        try:
                            member = await guild.fetch_member(int(player_discord_id))
                            player['name'] = member.display_name
                            player['avatar_url'] = str(member.avatar.url) if member.avatar else str(member.default_avatar.url)
                            logger.debug(f"[_load_game_state] Refreshed Discord info for player {player_discord_id}: {player['name']}")
                        except Exception as e:
                            logger.warning(f"Could not refresh member info for {player_discord_id} from Discord: {e}. Using stored data.")
                    # --- END OF NEWLY ADDED CODE ---

                    player.setdefault('total_chips', 1000)
                    player.setdefault('current_bet_in_round', 0)
                    player.setdefault('has_acted_in_round', False)
                    player.setdefault('folded', False)
                    player.setdefault('hand_revealed', False) # Backwards compatibility
    
            await conn.commit() # Commit after all read operations
            return game_state
    
        except Exception as e:
            logger.error(f"Error loading game state for room {room_id}: {e}", exc_info=True)
            # Rollback if an error occurs
            if conn:
                await conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    async def _save_game_state(self, room_id: str, game_state: dict):
        """Saves the game state for a given room_id to the database."""
        if not isinstance(game_state, dict):
            logger.error(f"[_save_game_state] Attempted to save non-dict game_state for room {room_id}. Type: {type(game_state)}. State: {game_state}")
            return # Prevent saving incorrect data type

        room_id_from_state = game_state.get("room_id")
        if room_id_from_state and room_id_from_state != room_id:
            logger.warning(f"[_save_game_state] room_id mismatch: argument={room_id}, game_state={room_id_from_state}")
            room_id = room_id_from_state
    
        room_id = str(room_id).strip()
        game_state_json = json.dumps(game_state)
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                # Use TRIM() on both sides of the comparison to be safe
                update_query = "UPDATE bot_game_rooms SET game_state = %s WHERE TRIM(room_id) = %s"
                logger.info(f"[_save_game_state] Attempting UPDATE for room_id: '{room_id}'")
                await cursor.execute(update_query, (game_state_json, room_id))

                if cursor.rowcount == 0:
                    logger.error(f"[_save_game_state] UPDATE failed: No rows were affected for room_id: '{room_id}'. This suggests the room does not exist or there is a data inconsistency. Game state: {game_state_json}")
                    # Re-raise the exception to propagate the error.
                    raise ValueError(f"Game room '{room_id}' not found for update, or update failed.")
                else:
                    logger.info(f"[_save_game_state] Successfully updated {cursor.rowcount} row(s) for room_id: '{room_id}'.")
            
            await conn.commit() # Explicitly commit the transaction
        except Exception as e:
            logger.error(f"Error saving game state for room '{room_id}': {e}", exc_info=True)
            # Rollback if an error occurs
            if conn:
                await conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
                if logger.handlers:
                    logger.handlers[0].flush() # Ensure the logs are flushed even on error


    async def deal_hole_cards(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals two hole cards to each player for the specified room_id."""
        
        # Ensure players list is not empty for dealing
        if not game_state.get('players'):
            logger.warning(f"[deal_hole_cards] No players in game for room {room_id}. Cannot deal hole cards.")
            return False, "No players in the game to deal cards.", game_state

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        # Shuffle only if it's a new game or if the deck hasn't been shuffled yet for this round
        if game_state['current_round'] == 'pre_game' or not deck.cards:
            deck.build() # Rebuild a full deck
            deck.shuffle()
            logger.info(f"[deal_hole_cards] Deck rebuilt and shuffled for room {room_id}.")
            
        players_data = game_state.get('players', [])
        logger.debug(f"[deal_hole_cards] Players before dealing: {len(players_data)}")

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
                logger.debug(f"[deal_hole_cards] Dealt cards to {player['name']}: {player['hand']}")
            else:
                logger.error(f"[deal_hole_cards] Not enough cards to deal hole cards for player {player['name']}.")
                return False, "Not enough cards.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['players'] = players_data
        game_state['board_cards'] = [] # Ensure board is empty for a new deal

        logger.info(f"[deal_hole_cards] Hole cards dealt for room {room_id}.")
        return True, "Hole cards dealt.", game_state

    async def deal_dealer_cards(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals two cards to the dealer for the specified room_id."""
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
            logger.info(f"[deal_dealer_cards] Dealer cards dealt for room {room_id}.")
        else:
            logger.error(f"[deal_dealer_cards] Not enough cards to deal dealer's hand for room {room_id}.")
            return False, "Not enough cards to deal dealer's hand.", game_state

        game_state['deck'] = deck.to_output_format()
        return True, "Dealer's cards dealt.", game_state


    async def deal_flop(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals the three community cards (flop) for the specified room_id."""
        if game_state['current_round'] != 'pre_flop':
            logger.warning(f"[deal_flop] Cannot deal flop. Current round is {game_state['current_round']} for room {room_id}.")
            return False, f"Cannot deal flop. Current round is {game_state['current_round']}.", game_state

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
                logger.error(f"[deal_flop] Not enough cards for flop in room {room_id}.")
                return False, "Not enough cards for flop.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "flop"
        
        logger.info(f"[deal_flop] Flop dealt for room {room_id}. Current round set to {game_state['current_round']}.")
        return True, "Flop dealt.", game_state

    async def deal_turn(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals the fourth community card (turn) for the specified room_id."""
        if game_state['current_round'] != 'flop':
            logger.warning(f"[deal_turn] Cannot deal turn. Current round is {game_state['current_round']} for room {room_id}.")
            return False, f"Cannot deal turn. Current round is {game_state['current_round']}.", game_state

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        turn_card = deck.deal_card()
        if turn_card:
            board_cards_output.append(turn_card.to_output_format())
        else:
            logger.error(f"[deal_turn] Not enough cards for turn in room {room_id}.")
            return False, "Not enough cards for turn.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "turn"

        logger.info(f"[deal_turn] Turn dealt for room {room_id}. Current round set to {game_state['current_round']}.")
        return True, "Turn dealt.", game_state

    async def deal_river(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals the fifth and final community card (river) for the specified room_id."""
        if game_state['current_round'] != 'turn':
            logger.warning(f"[deal_river] Cannot deal river. Current round is {game_state['current_round']} for room {room_id}.")
            return False, f"Cannot deal river. Current round is {game_state['current_round']}.", game_state

        deck = Deck(game_state.get('deck', [])) # Use Deck from game_models
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        river_card = deck.deal_card()
        if river_card:
            board_cards_output.append(river_card.to_output_format())
        else:
            logger.error(f"[deal_river] Not enough cards for river in room {room_id}.")
            return False, "Not enough cards for river.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "river"

        logger.info(f"[deal_river] River dealt for room {room_id}. Current round set to {game_state['current_round']}.")
        return True, "River dealt.", game_state

    async def evaluate_hands(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """
        Evaluates all players' hands against the community cards, determines the winner(s),
        and adds this information to the game state.
        """
        if game_state['current_round'] != 'river':
            logger.warning(f"[evaluate_hands] Cannot evaluate hands. Current round is {game_state['current_round']} for room {room_id}.")
            return False, f"Cannot evaluate hands. Current round is {game_state['current_round']}.", game_state

        players_data = game_state.get('players', [])
        board_cards_obj = [Card.from_output_format(c_str) for c_str in game_state.get('board_cards', [])] # Use Card from game_models
        active_players = [p for p in players_data if not p.get('folded', False)]

        if len(board_cards_obj) != 5:
            logger.error(f"[evaluate_hands] Board not complete for evaluation in room {room_id}.")
            return False, "Board not complete.", game_state

        player_evaluations = []
        best_score = (-1,)
        winning_players = []

        for player_data in active_players:
            player_hand_obj = [Card.from_output_format(c_str) for c_str in player_data.get('hand', [])] # Use Card from game_models
            combined_cards = player_hand_obj + board_cards_obj
            hand_type, hand_score_vector = evaluate_poker_hand(combined_cards)

            player_evaluations.append({
                "discord_id": player_data['discord_id'],
                "name": player_data['name'],
                "hand_type": hand_type,
                "hand_score_vector": hand_score_vector,
                "hole_cards": [c.to_output_format() for c in player_hand_obj],
                "is_winner": False # Will be set below
            })
            
            # Check for winner
            if hand_score_vector > best_score:
                best_score = hand_score_vector
                winning_players = [player_data['discord_id']]
            elif hand_score_vector == best_score:
                winning_players.append(player_data['discord_id'])

        # Sort the evaluations from best to worst hand
        player_evaluations.sort(key=lambda x: x['hand_score_vector'], reverse=True)

        # Mark the winners in the sorted evaluation data
        for eval_data in player_evaluations:
            if eval_data['discord_id'] in winning_players:
                eval_data['is_winner'] = True
        
        winning_hand_name = "N/A"
        if player_evaluations:
            winning_hand_name = player_evaluations[0]['hand_type']

        game_state['current_round'] = "showdown"
        game_state['last_evaluation'] = {
            "evaluations": player_evaluations,
            "winning_info": {
                "hand_type": winning_hand_name,
                "score_vector": best_score,
                "winners": winning_players
            }
        }
        game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME # 10-second timer after showdown

        # Calculate winnings per player (equal split of the pot for now)
        winnings = game_state.get('current_betting_round_pot', 0)
        num_winners = len(winning_players)
        winnings_per_player = winnings // num_winners if num_winners > 0 else 0

        # Update kekchipz in the database for each winner
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                for winner_id in winning_players:
                    await cursor.execute(
                        "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE discord_id = %s AND guild_id = %s",
                        (winnings_per_player, winner_id, game_state['guild_id'])
                    )
                    logger.info(f"[evaluate_hands] Updated kekchipz for winner {winner_id} by +{winnings_per_player}.")
            
            # Explicitly commit the transaction for this operation
            await conn.commit()
            logger.info("[evaluate_hands] Kekchipz updates successfully committed to the database.")

        except Exception as e:
            logger.error(f"[evaluate_hands] Failed to update kekchipz for winners: {e}", exc_info=True)
            # Rollback if an error occurs
            if conn:
                await conn.rollback()
        finally:
            if conn:
                conn.close()

        logger.info(f"[evaluate_hands] Hands evaluated for room {room_id}. Current round set to {game_state['current_round']}. Winner(s): {winning_players}")
        return True, "Hands evaluated.", game_state

    async def broadcast_game_state(self, room_id: str, game_state: dict, echo_message: dict = None):
        """
        Broadcasts the current game state to all connected WebSocket clients in the room.
        Optionally includes an echo_message for chat.
        """
        if room_id not in self.bot.ws_rooms:
            logger.warning(f"[broadcast_game_state] No WebSocket clients found for room_id: {room_id}. Cannot broadcast.")
            return

        payload = {
            "game_state": game_state
        }
        if echo_message:
            payload["echo_message"] = echo_message
            logger.info(f"[broadcast_game_state] Broadcasting game state with echo_message for room {room_id}.")
        
        # Add a debug log to see the current_round being broadcast
        logger.debug(f"[broadcast_game_state] Broadcasting game state with current_round: {game_state.get('current_round', 'N/A')} for room {room_id}.")

        message_json = json.dumps(payload)
        
        for websocket in list(self.bot.ws_rooms[room_id]): # Iterate over a copy to avoid issues if clients disconnect
            try:
                await websocket.send_str(message_json)
            except Exception as e:
                logger.error(f"[broadcast_game_state] Error sending WebSocket message to client in room {room_id}: {e}", exc_info=True)
                # Optionally remove disconnected websocket here, or rely on on_disconnect
                # self.bot.ws_rooms[room_id].remove(websocket) # This might be handled by aiohttp's ws_handler

    async def _award_pot_to_last_player(self, room_id: str, game_state: dict) -> dict:
        """Awards the pot to the last remaining player and ends the hand."""
        logger.info(f"[_award_pot_to_last_player] Awarding pot to last player in room {room_id}.")
        
        # Collect all bets into the main pot
        total_pot = game_state.get('current_betting_round_pot', 0)
        for p in game_state['players']:
            total_pot += p.get('current_bet_in_round', 0)
            p['current_bet_in_round'] = 0

        game_state['current_betting_round_pot'] = total_pot

        winner = next((p for p in game_state['players'] if not p.get('folded', False)), None)
        
        if not winner:
            logger.error(f"[_award_pot_to_last_player] Could not find a winner in room {room_id}.")
            # Reset for next round anyway
            game_state['current_round'] = 'showdown'
            game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME
            return game_state

        winner_id = winner['discord_id']
        winnings = game_state['current_betting_round_pot']

        # Update kekchipz in the database for the winner
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE discord_id = %s AND guild_id = %s",
                    (winnings, winner_id, game_state['guild_id'])
                )
                logger.info(f"[_award_pot_to_last_player] Updated kekchipz for winner {winner_id} by +{winnings}.")
            await conn.commit()
        except Exception as e:
            logger.error(f"[_award_pot_to_last_player] Failed to update kekchipz for winner: {e}", exc_info=True)
            if conn: await conn.rollback()
        finally:
            if conn: conn.close()

        game_state['current_round'] = "showdown"
        game_state['last_evaluation'] = {
            "evaluations": [],
            "winning_info": {
                "hand_type": "Win by Default",
                "score_vector": (0,),
                "winners": [winner_id],
                "message": f"{winner['name']} wins ${winnings} as the last player remaining."
            }
        }
        game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME
        
        logger.info(f"[_award_pot_to_last_player] Hand ended. Winner: {winner['name']}.")
        return game_state

    async def _reveal_board_and_dealer(self, room_id: str, game_state: dict) -> dict:
        """For single player games, reveals the rest of the board and dealer hand when player folds post-flop."""
        logger.info(f"[_reveal_board_and_dealer] Revealing board and dealer for single player fold in room {room_id}.")

        # Deal remaining cards
        if game_state['current_round'] == 'flop':
            _, _, game_state = await self.deal_turn(room_id, game_state)
            _, _, game_state = await self.deal_river(room_id, game_state)
        elif game_state['current_round'] == 'turn':
            _, _, game_state = await self.deal_river(room_id, game_state)

        game_state['current_round'] = "showdown"
        game_state['last_evaluation'] = {
            "winning_info": {
                "message": "Player folded. Revealing dealer hand and board."
            }
        }
        game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME
        
        return game_state

    # --- Helper for getting sorted players ---
    def _get_sorted_players(self, game_state: dict) -> list:
        """Returns a list of players sorted by their seat_id."""
        players = game_state.get('players', [])
        active_players = [p for p in players if p.get('seat_id') and not p.get('folded', False)]
        logger.debug(f"[_get_sorted_players] Found {len(active_players)} active players.")
        return sorted(active_players, key=lambda p: int(p['seat_id'].replace('seat_', '')))

    # --- Helper for getting next active player turn ---
    def _get_next_active_player_index(self, game_state: dict, current_index: int) -> int:
        """
        Finds the index of the next active player in the sorted list,
        skipping folded players. Returns -1 if no active players.
        """
        sorted_players = self._get_sorted_players(game_state)
        if not sorted_players:
            logger.debug("[_get_next_active_player_index] No sorted players found.")
            return -1

        num_players = len(sorted_players)
        
        # Determine the starting point for finding the next player
        start_search_index = (current_index + 1) % num_players if current_index != -1 else 0
        logger.debug(f"[_get_next_active_player_index] Starting search from index {start_search_index} for {num_players} players.")

        for i in range(num_players):
            idx = (start_search_index + i) % num_players
            player = sorted_players[idx]
            # Player is active if not folded and has not yet acted (or needs to match a new bet)
            # The 'has_acted_in_round' needs to be carefully managed. For now, focus on folded.
            if not player.get('folded', False):
                logger.debug(f"[_get_next_active_player_index] Next active player found at index {idx}: {player['name']}.")
                return idx
        logger.debug("[_get_next_active_action_player_id] No active players found after full iteration.")
        return -1 # No active players found

    # --- Helper to start a player's turn ---
    async def _start_player_turn(self, room_id: str, game_state: dict) -> dict:
        """Sets the timer for the current player's turn."""
        sorted_players = self._get_sorted_players(game_state)
        current_player_index = game_state['current_player_turn_index']

        # Allow starting turn even if only one player, as long as index is valid
        if not sorted_players:
            logger.error(f"[_start_player_turn] No sorted players available for room {room_id}. Cannot start turn.")
            game_state['timer_end_time'] = None
            game_state['current_player_turn_index'] = -1
            return game_state

        if len(sorted_players) == 1 and current_player_index == -1:
            game_state['current_player_turn_index'] = 0
            current_player_index = 0
            logger.info(f"[_start_player_turn] Setting initial turn for single player {sorted_players[current_player_index]['name']} in room {room_id}.")
        elif current_player_index == -1 or current_player_index >= len(sorted_players):
            logger.error(f"[_start_player_turn] Invalid current_player_turn_index: {current_player_index} with {len(sorted_players)} players in room {room_id}. Cannot start turn.")
            game_state['timer_end_time'] = None
            game_state['current_player_turn_index'] = -1
            return game_state

        game_state['timer_end_time'] = int(time.time()) + self.PLAYER_TURN_TIME
        logger.info(f"[_start_player_turn] Starting turn for player {sorted_players[current_player_index]['name']}. Timer ends at {game_state['timer_end_time']}")
        return game_state


    # --- Helper to apply blinds ---
    async def _apply_blinds(self, game_state: dict):
        """Applics small and big blinds to players."""
        sorted_players = self._get_sorted_players(game_state)
        num_players = len(sorted_players)

        if num_players == 0:
            logger.warning("[_apply_blinds] No players to apply blinds. Skipping.")
            return

        # Determine positions relative to the dealer button
        dealer_pos = game_state['dealer_button_position']
        
        # Small blind is next to dealer, big blind is after small blind
        small_blind_pos_idx = (dealer_pos + 1) % num_players
        big_blind_pos_idx = (dealer_pos + 2) % num_players

        # Use .get() with a default value to prevent KeyError
        small_blind_amount = game_state.get('small_blind_amount', 5)
        big_blind_amount = game_state.get('big_blind_amount', 10)

        # Ensure indices are valid before accessing players
        small_blind_player = sorted_players[small_blind_pos_idx] if num_players > small_blind_pos_idx else None
        big_blind_player = sorted_players[big_blind_pos_idx] if num_players > big_blind_pos_idx else None

        if small_blind_player:
            # Deduct small blind
            small_blind_amount = min(small_blind_amount, small_blind_player['total_chips'])
            small_blind_player['total_chips'] -= small_blind_amount
            small_blind_player['current_bet_in_round'] += small_blind_amount
            game_state['current_betting_round_pot'] += small_blind_amount
            logger.info(f"[_apply_blinds] Player {small_blind_player['name']} posts small blind: ${small_blind_amount}")
            small_blind_player['has_acted_in_round'] = True # Mark as acted

        if big_blind_player:
            # Deduct big blind
            big_blind_amount = min(big_blind_amount, big_blind_player['total_chips'])
            big_blind_player['total_chips'] -= big_blind_amount
            big_blind_player['current_bet_in_round'] += big_blind_amount
            game_state['current_betting_round_pot'] += big_blind_amount
            logger.info(f"[_apply_blinds] Player {big_blind_player['name']} posts big blind: ${big_blind_amount}")
            big_blind_player['has_acted_in_round'] = True # Big blind has acted by posting

        # Set the minimum bet for this round to the big blind amount
        # If only one player, this might be 0 or a default.
        if big_blind_player:
            game_state['current_round_min_bet'] = big_blind_amount
        else:
            game_state['current_round_min_bet'] = 0 # No big blind, min bet is 0
            logger.info(f"[_apply_blinds] No big blind player, current_round_min_bet set to {game_state['current_round_min_bet']}.")


        # Update players list in game_state (ensure changes to player dicts are reflected)
        # Iterate through original players list and update references
        for i, player_in_state in enumerate(game_state['players']):
            if small_blind_player and player_in_state['discord_id'] == small_blind_player['discord_id']:
                game_state['players'][i] = small_blind_player
            elif big_blind_player and player_in_state['discord_id'] == big_blind_player['discord_id']:
                game_state['players'][i] = big_blind_player


    async def _start_betting_round(self, room_id: str, game_state: dict) -> dict:
        """Initializes variables for a new betting round."""
        logger.info(f"[_start_betting_round] Starting new betting round for {game_state['current_round']} in room {room_id}.")
        
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
        logger.debug(f"[_start_betting_round] Number of sorted players: {num_players}")

        if num_players == 0:
            logger.warning("[_start_betting_round] No players to start betting round. Skipping.")
            return game_state

        if game_state['current_round'] == 'pre_flop':
            # Pre-flop: Action starts after big blind (i.e., player after big blind)
            dealer_pos = game_state['dealer_button_position']
            big_blind_pos_idx = (dealer_pos + 2) % num_players
            first_player_index = (big_blind_pos_idx + 1) % num_players
            
            # Apply blinds (logic updated to handle single player gracefully)
            await self._apply_blinds(game_state)
            
            # If only one player, that player is effectively the only one who can act.
            if num_players == 1:
                first_player_index = 0 
                logger.debug("[_start_betting_round] Single player game: first_player_index set to 0.")

        else: # Flop, Turn, River betting rounds: Action starts with the first active player after the dealer button
            dealer_pos = game_state['dealer_button_position']
            first_player_index = (dealer_pos + 1) % num_players
            # Find the next *active* player after the dealer button
            first_player_index = self._get_next_active_player_index(game_state, first_player_index - 1) 
            logger.debug(f"[_start_betting_round] Non-pre_game round: first_player_index determined as {first_player_index}.")


        if first_player_index != -1:
            game_state['current_player_turn_index'] = first_player_index
            logger.debug(f"[_start_betting_round] Setting current_player_turn_index to {first_player_index}.")
            game_state = await self._start_player_turn(room_id, game_state)
        else:
            logger.warning(f"[_start_betting_round] No active players to start betting round in room {room_id}. Advancing phase.")
            game_state = await self._advance_game_phase(room_id, game_state)
        return game_state


    async def _end_betting_round(self, room_id: str, game_state: dict) -> dict:
        """Collects bets into the main pot and prepares for the next phase."""
        logger.info(f"[_end_betting_round] Ending betting round for {game_state['current_round']} in room {room_id}.")
        
        # Collect all current_bet_in_round into the main pot
        for player in game_state['players']:
            game_state['current_betting_round_pot'] += player['current_bet_in_round']
            player['current_bet_in_round'] = 0 # Reset for next betting round
            player['has_acted_in_round'] = False # Reset acted status for next round

        game_state['current_round_min_bet'] = 0 # Reset min bet for next round
        game_state['last_aggressive_action_player_id'] = None # Reset aggressive action

        return game_state


    def _check_round_completion(self, game_state: dict) -> bool:
        """
        Checks if the current betting round is complete.
        A round is complete if:
        1. Only one player is not folded. (This player wins the pot)
        2. All active players (not folded) have had a chance to act and have either:
           a) Matched the highest current bet (called).
           b) Gone all-in for less than the highest bet.
           c) Checked (if no bet has been made).
           d) Folded.
        And the action has returned to the player who made the last aggressive action,
        or there was no aggressive action and everyone has acted once.
        """
        sorted_players = self._get_sorted_players(game_state)
        active_players = [p for p in sorted_players if not p.get('folded', False)]
        logger.debug(f"[_check_round_completion] Active players count: {len(active_players)}")

        if len(active_players) <= 1:
            logger.info("[_check_round_completion] Betting round complete: 1 or fewer active players remaining.")
            return True

        highest_bet_in_round = max([p.get('current_bet_in_round', 0) for p in active_players])
        logger.debug(f"[_check_round_completion] Highest bet in round: {highest_bet_in_round}")

        # Determine if all active players have 'settled' their action relative to the highest bet
        all_settled = True
        for player in active_players:
            # If player has not acted yet, round is not complete
            if not player.get('has_acted_in_round', False):
                logger.debug(f"[_check_round_completion] Player {player.get('name', 'N/A')} has not acted yet. Round not complete.")
                all_settled = False
                break
            
            # If player's current bet is less than highest and they still have chips,
            # they need to act again (unless they are the one who made the highest bet).
            if player.get('current_bet_in_round', 0) < highest_bet_in_round and player.get('total_chips', 0) > 0:
                logger.debug(f"[_check_round_completion] Player {player.get('name', 'N/A')} has not matched highest bet and still has chips. Round not complete.")
                all_settled = False
                break

        logger.debug(f"[_check_round_completion] All active players settled (acted and matched/all-in): {all_settled}")

        if not all_settled:
            return False # Not all players have completed their action for this bet level

        # Now, consider the turn cycle
        current_player_index = game_state['current_player_turn_index']
        current_player_id = sorted_players[current_player_index]['discord_id'] if current_player_index != -1 and current_player_index < len(sorted_players) else None
        last_aggressive_action_player_id = game_state['last_aggressive_action_player_id']

        logger.debug(f"[_check_round_completion] Current player ID: {current_player_id}, Last aggressive action player ID: {last_aggressive_action_player_id}")

        # Case 1: No aggressive action (all checks/calls up to the initial big blind)
        if last_aggressive_action_player_id is None:
            # If everyone has settled, and there was no raise, the round is complete.
            # This covers scenarios where everyone checks or everyone calls the big blind.
            logger.info(f"[_check_round_completion] Betting round complete: No aggressive action, all settled.")
            return True
        
        # Case 2: There was an aggressive action (bet or raise)
        # The round is complete if all active players have settled, AND the action has returned
        # to the player who made the last aggressive action (meaning everyone after them has responded).
        if current_player_id == last_aggressive_action_player_id:
            logger.info(f"[_check_round_completion] Betting round complete: Action returned to last aggressive player {current_player_id}.")
            return True
        
        # Edge case: The last aggressive player folded after their action.
        # If everyone else has settled, the round should also end.
        last_aggressive_player_obj = next((p for p in game_state['players'] if p['discord_id'] == last_aggressive_action_player_id), None)
        if last_aggressive_player_obj and last_aggressive_player_obj.get('folded', False):
             logger.info(f"[_check_round_completion] Betting round complete: Last aggressive player {last_aggressive_action_player_id} folded.")
             return True

        return False

    async def _advance_game_phase(self, room_id: str, game_state: dict) -> dict:
        """Moves the game to the next phase (flop, turn, river, showdown) or handles win by default."""
        logger.info(f"[_advance_game_phase] Advancing game phase from {game_state['current_round']} for room {room_id}.")

        active_players = [p for p in game_state['players'] if not p.get('folded', False)]
        num_active_players = len(active_players)

        # Handle win by default (multiplayer) or single player fold scenarios
        if num_active_players <= 1:
            game_state = await self._end_betting_round(room_id, game_state) # Collect final bets first
            if num_active_players == 1:
                return await self._award_pot_to_last_player(room_id, game_state)
            elif num_active_players == 0: # Single player game and player folded
                if game_state['current_round'] == 'pre_flop':
                    logger.info(f"[_advance_game_phase] Single player folded pre-flop. Ending hand for room {room_id}.")
                    game_state['current_round'] = 'showdown'
                    game_state['last_evaluation'] = {
                        "winning_info": {
                            "message": "You folded. A new hand will begin shortly."
                        }
                    }
                    game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME
                    return game_state
                else: # Post-flop fold
                    logger.info(f"[_advance_game_phase] Single player folded post-flop. Revealing board for room {room_id}.")
                    return await self._reveal_board_and_dealer(room_id, game_state)

        # Original logic for advancing betting rounds
        game_state = await self._end_betting_round(room_id, game_state)

        next_round = None
        success = False
        msg = ""

        if game_state['current_round'] == 'pre_flop':
            success, msg, game_state = await self.deal_flop(room_id, game_state)
            next_round = 'flop'
        elif game_state['current_round'] == 'flop':
            success, msg, game_state = await self.deal_turn(room_id, game_state)
            next_round = 'turn'
        elif game_state['current_round'] == 'turn':
            success, msg, game_state = await self.deal_river(room_id, game_state)
            next_round = 'river'
        elif game_state['current_round'] == 'river':
            success, msg, game_state = await self.evaluate_hands(room_id, game_state)
            next_round = 'showdown'
        elif game_state['current_round'] == 'showdown':
            success, msg, game_state = await self._start_new_round_pre_flop(room_id, game_state, game_state['guild_id'], game_state['channel_id'])
            next_round = 'pre_flop'
            
        if not success:
            logger.error(f"[_advance_game_phase] Failed to advance game phase from {game_state['current_round']}: {msg}")
            return game_state

        # Start the new betting round (if applicable)
        if next_round in ['pre_flop','flop', 'turn', 'river']:
            logger.debug(f"[_advance_game_phase] Starting betting round for {next_round}.")
            game_state = await self._start_betting_round(room_id, game_state)
        
        logger.debug(f"[_advance_game_phase] Final game state after phase advance. Current round: {game_state.get('current_round', 'N/A')}")
        return game_state


    # --- New WebSocket Request Handler ---
    async def handle_websocket_game_action(self, request_data: dict):
        """
        Receives raw request data from a WebSocket client and dispatches it
        to the appropriate game action method.
        After processing, it broadcasts the updated game state to all clients.
        """
        logger.info(f"[handle_websocket_game_action] Processing WebSocket action: {request_data.get('action')}") # Added this line to confirm entry
        action = request_data.get('action')
        room_id = request_data.get('room_id')
        guild_id = request_data.get('guild_id')    
        sender_id = request_data.get('sender_id') # Assuming sender_id is always present in WS requests
        channel_id = request_data.get('channel_id') # Now making channel_id an optional parameter

        # Only require the mandatory parameters for every message
        if not all([action, room_id, guild_id, sender_id]):
            logger.error(f"[handle_websocket_game_action] Initial WebSocket message missing critical parameters. Data: {request_data}")
            return

        logger.info(f"[handle_websocket_game_action] Backend dealer received WS action: '{action}' for Room ID: {room_id}, Sender: {sender_id}")

        success = False
        message = "Unknown action."
        echo_message_data = None # To hold chat message data if applicable
        game_state = {} # Initialize game_state here

        try:
            # Load the current game state once at the beginning
            game_state = await self._load_game_state(room_id, guild_id, channel_id)
            logger.debug(f"[handle_websocket_game_action] Current round loaded at start: {game_state.get('current_round', 'N/A')}")

            # Define which actions modify the game state
            mutating_actions = [
                "add_player", 
                "leave_player", 
                "start_new_round_pre_flop", 
                "player_action", 
                "auto_action_timeout",
            ]

            if action == "get_state":
                success = True
                message = "Game state retrieved."
            elif action == "add_player":
                player_data = request_data.get('player_data')
                if not player_data or not isinstance(player_data, dict):
                    logger.error("[handle_websocket_game_action] Missing or invalid player_data for add_player.")
                    return
                success, message, game_state = await self._add_player_to_game(room_id, player_data, game_state, guild_id, channel_id)
            elif action == "leave_player":
                discord_id = request_data.get('discord_id')
                if not discord_id:
                    logger.error("[handle_websocket_game_action] Missing discord_id for leave_player.")
                    return
                success, message, game_state = await self._leave_player(room_id, discord_id, game_state)
            elif action == "start_new_round_pre_flop":
                if game_state.get('current_round') in ['pre_game', 'showdown']:
                    logger.info(f"[handle_websocket_game_action] Attempting to start new round from {game_state.get('current_round')} for room {room_id}.")
                    success, message, game_state = await self._start_new_round_pre_flop(room_id, game_state, guild_id, channel_id)
                    if success:
                        if not game_state.get('game_started_once', False):
                            game_state['game_started_once'] = True 
                else:
                    logger.warning(f"[handle_websocket_game_action] Attempt to start new round failed: Game is already in progress or not in a startable state ({game_state.get('current_round')}) for room {room_id}. {request_data}")
                    return
            elif action == "player_action":
                player_id = request_data.get('player_id')
                action_type = request_data.get('action_type')
                amount = request_data.get('amount', 0)
                
                if not all([player_id, action_type]):
                    logger.error("[handle_websocket_game_action] Missing player_id or action_type for player_action.")
                    return
                success, message, game_state = await self._handle_player_action(room_id, player_id, action_type, amount, game_state)
            elif action == "auto_action_timeout":
                player_id = request_data.get('player_id')
                if not player_id:
                    logger.error("[handle_websocket_game_action] Missing player_id for auto_action_timeout.")
                    return
                success, message, game_state = await self._auto_action_on_timeout(room_id, player_id, game_state)
            elif action == "send_message":
                message_content = request_data.get('message_content')
                if not message_content:
                    logger.error("[handle_websocket_game_action] Missing message_content for send_message.")
                    return
                success, message, response_data_from_handler = await self._handle_in_game_message(room_id, sender_id, message_content, game_state)
                if success:
                    echo_message_data = response_data_from_handler.get('echo_message')
                    game_state = response_data_from_handler.get('game_state', game_state) # Ensure game_state is updated from handler
            else:
                logger.warning(f"[handle_websocket_game_action] Received unsupported WS action: {action} for room {room_id}.")
                return

            # After action is processed, handle saving and broadcasting based on success
            if success:
                if action in mutating_actions:
                    # Save the game state if it was a successful, mutating action
                    await self._save_game_state(room_id, game_state)
                    logger.info(f"[handle_websocket_game_action] Action '{action}' processed and state saved for room {room_id}.")
                
                # Always broadcast after a successful action (mutating or non-mutating)
                logger.debug(f"[handle_websocket_game_action] Current round before broadcast: {game_state.get('current_round', 'N/A')}")
                await self.broadcast_game_state(room_id, game_state, echo_message_data)
                logger.info(f"[handle_websocket_game_action] State broadcast for room {room_id}.")
            else:
                # Log a warning if the action failed
                logger.warning(f"[handle_websocket_game_action] Action '{action}' failed for room {room_id}: {message}")

        except Exception as e:
            # This will catch any unexpected exceptions and log them
            logger.error(f"[handle_websocket_game_action] An unhandled exception occurred while processing action '{action}' for room {room_id}: {e}", exc_info=True)
            # Flush the log handler to ensure the error is written to the console.
            if logger.handlers:
                logger.handlers[0].flush()
            # Re-raise the exception to allow the calling function to handle it as well.
            raise


    async def _handle_player_action(self, room_id: str, player_id: str, action_type: str, amount: int = 0, game_state: dict = None) -> tuple[bool, str, dict]:
        """
        Processes a player's action (call, check, bet, raise, fold).
        Updates game state, advances turn, and triggers next phase if round completes.
        """
        if game_state is None:
            logger.error(f"[_handle_player_action] game_state not provided for room {room_id}.")
            return False, "Internal error: Game state not provided.", game_state

        sorted_players = self._get_sorted_players(game_state)
        
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state:
            logger.warning(f"[_handle_player_action] Player {player_id} not found in game {room_id}.")
            return False, "Player not found in game.", game_state

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 and len(sorted_players) > game_state['current_player_turn_index'] else None

        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            logger.warning(f"[_handle_player_action] It's not player {player_id}'s turn in room {room_id}. Current turn: {current_player_turn_obj['discord_id'] if current_player_turn_obj else 'None'}.")
            return False, "It's not your turn.", game_state
        
        if player_in_state.get('folded', False):
            logger.warning(f"[_handle_player_action] Player {player_id} already folded in room {room_id}.")
            return False, "You have already folded.", game_state

        min_bet_to_call = game_state.get('current_round_min_bet', 0) - player_in_state.get('current_bet_in_round', 0)
        logger.debug(f"[_handle_player_action] Player {player_id} action: {action_type}, amount: {amount}, min_bet_to_call: {min_bet_to_call}.")
        
        message = ""
        success = False

        if action_type == 'fold':
            player_in_state['folded'] = True
            player_in_state['hand_revealed'] = True # Reveal hand on fold
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} folded."
            success = True
            logger.info(message)
        elif action_type == 'check':
            if min_bet_to_call > 0:
                logger.warning(f"[_handle_player_action] Player {player_id} attempted to check when min_bet_to_call is {min_bet_to_call}.")
                return False, "Cannot check, a bet has been made. You must call or raise.", game_state
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} checked."
            success = True
            logger.info(message)
        elif action_type == 'call':
            if player_in_state['total_chips'] < min_bet_to_call:
                logger.warning(f"[_handle_player_action] Player {player_id} attempted to call ${min_bet_to_call} but only has ${player_in_state['total_chips']}.")
                return False, f"Not enough chips to call ${min_bet_to_call}. You have ${player_in_state['total_chips']}.", game_state
            
            bet_amount = min_bet_to_call
            player_in_state['total_chips'] -= bet_amount
            player_in_state['current_bet_in_round'] += bet_amount
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} called ${bet_amount}."
            success = True
            logger.info(message)
        elif action_type == 'bet' or action_type == 'raise':
            if amount <= min_bet_to_call:
                logger.warning(f"[_handle_player_action] Player {player_id} attempted to {action_type} with amount ${amount} which is not greater than min_bet_to_call ${min_bet_to_call}.")
                return False, f"Bet/Raise amount must be greater than current call amount (${min_bet_to_call}).", game_state
            if player_in_state['total_chips'] < amount:
                logger.warning(f"[_handle_player_action] Player {player_id} attempted to {action_type} with amount ${amount} but only has ${player_in_state['total_chips']}.")
                return False, f"Not enough chips to bet/raise ${amount}. You have ${player_in_state['total_chips']}.", game_state
            
            player_in_state['total_chips'] -= amount
            player_in_state['current_bet_in_round'] += amount
            game_state['current_round_min_bet'] = player_in_state['current_bet_in_round'] # New highest bet
            game_state['last_aggressive_action_player_id'] = player_id # Mark who made the aggressive action

            for p in game_state['players']:
                if p['discord_id'] != player_id and not p.get('folded', False):
                    p['has_acted_in_round'] = False
            player_in_state['has_acted_in_round'] = True # Current player has acted

            message = f"{player_in_state['name']} {action_type}d ${amount}."
            success = True
            logger.info(message)
        elif action_type == 'all_in':
            amount = player_in_state['total_chips']
            if amount == 0:
                logger.warning(f"[_handle_player_action] Player {player_id} attempted to go all-in with 0 chips.")
                return False, "You have no chips to go all-in.", game_state

            player_in_state['total_chips'] = 0
            player_in_state['current_bet_in_round'] += amount
            
            if player_in_state['current_bet_in_round'] > game_state['current_round_min_bet']:
                game_state['current_round_min_bet'] = player_in_state['current_bet_in_round']
                game_state['last_aggressive_action_player_id'] = player_id
                for p in game_state['players']:
                    if p['discord_id'] != player_id and not p.get('folded', False):
                        p['has_acted_in_round'] = False
            
            player_in_state['has_acted_in_round'] = True
            message = f"{player_in_state['name']} went All-In with ${amount}!"
            success = True
            logger.info(message)
        else:
            logger.warning(f"[_handle_player_action] Invalid player action type: {action_type}.")
            return False, "Invalid player action type.", game_state

        if success:
            if self._check_round_completion(game_state):
                logger.info(f"[_handle_player_action] Betting round {game_state['current_round']} completed. Advancing phase.")
                game_state = await self._advance_game_phase(room_id, game_state)
            else:
                next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
                if next_player_idx != -1:
                    game_state['current_player_turn_index'] = next_player_idx
                    logger.debug(f"[_handle_player_action] Advancing turn to player at index {next_player_idx}.")
                    game_state = await self._start_player_turn(room_id, game_state)
                else:
                    logger.warning(f"[_handle_player_action] No next active player found, but round not marked complete. Advancing phase as fallback for room {room_id}.")
                    game_state = await self._advance_game_phase(room_id, game_state)
            return True, message, game_state
        return success, message, game_state

    async def _auto_action_on_timeout(self, room_id: str, player_id: str, game_state: dict = None) -> tuple[bool, str, dict]:
        """
        Performs an automatic action (call/fold) for a player whose turn timed out.
        """
        if game_state is None:
            logger.error(f"[_auto_action_on_timeout] game_state not provided for room {room_id}.")
            return False, "Internal error: Game state not provided.", game_state

        sorted_players = self._get_sorted_players(game_state)
        
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state:
            logger.warning(f"[_auto_action_on_timeout] Player {player_id} not found in game for timeout action in room {room_id}.")
            return False, "Player not found in game for timeout action.", game_state

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 and len(sorted_players) > game_state['current_player_turn_index'] else None

        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            logger.warning(f"[_auto_action_on_timeout] Timeout action for incorrect player ({player_id}) or not their turn in room {room_id}. Current turn: {current_player_turn_obj['discord_id'] if current_player_turn_obj else 'None'}.")
            return False, "Timeout action for incorrect player or not their turn.", game_state
        
        if int(time.time()) < game_state.get('timer_end_time', 0):
            logger.warning(f"[_auto_action_on_timeout] Player {player_id}'s turn has not timed out yet in room {room_id}.")
            return False, "Player's turn has not timed out yet.", game_state

        min_bet_to_call = game_state.get('current_round_min_bet', 0) - player_in_state.get('current_bet_in_round', 0)
        logger.debug(f"[_auto_action_on_timeout] Player {player_id} timeout action. min_bet_to_call: {min_bet_to_call}.")
        
        action_message = ""
        if min_bet_to_call > 0:
            player_in_state['folded'] = True
            player_in_state['hand_revealed'] = True # Also reveal hand on timeout fold
            action_message = f"{player_in_state['name']} automatically folded due to timeout."
        else:
            player_in_state['has_acted_in_round'] = True
            action_message = f"{player_in_state['name']} automatically checked due to timeout."
        
        logger.info(f"[_auto_action_on_timeout] Player {player_id} auto-action completed in room {room_id}. Message: {action_message}.")

        if self._check_round_completion(game_state):
            logger.info(f"[_auto_action_on_timeout] Betting round {game_state['current_round']} completed after timeout. Advancing phase for room {room_id}.")
            game_state = await self._advance_game_phase(room_id, game_state)
        else:
            next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
            if next_player_idx != -1:
                game_state['current_player_turn_index'] = next_player_idx
                logger.debug(f"[_auto_action_on_timeout] Advancing turn to player at index {next_player_idx}.")
                game_state = await self._start_player_turn(room_id, game_state)
            else:
                logger.warning(f"[_auto_action_on_timeout] No next active player found, but round not marked complete. Advancing phase as fallback for room {room_id}.")
                game_state = await self._advance_game_phase(room_id, game_state)

        return True, action_message, game_state


    async def _add_player_to_game(self, room_id: str, player_data: dict, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """
        Adds a player to the game state for a given room_id, including their chosen seat_id.
        Ensures a player cannot sit in an occupied seat or sit in multiple seats.
        """
        logger.info(f"[_add_player_to_game] Attempting to add player for room {room_id} with data: {player_data}")
        players = game_state.get('players', [])
        logger.info(f"[_add_player_to_game] Current players in game state: {players}")
        
        player_discord_id = player_data['discord_id']
        player_name = player_data['name']
        seat_id = player_data.get('seat_id')

        if not seat_id:
            logger.warning(f"[_add_player_to_game] No seat_id provided for player {player_name}.")
            return False, "Seat ID is required to add a player.", game_state

        existing_player = next((p for p in players if p['discord_id'] == player_discord_id), None)
        if existing_player:
            logger.info(f"[_add_player_to_game] Player {player_name} ({player_discord_id}) already exists in game state.")
            if existing_player.get('seat_id') == seat_id:
                logger.info(f"[_add_player_to_game] Player {player_name} already in seat {seat_id}.")
                return False, f"Player {player_name} is already in seat {seat_id}.", game_state
            else:
                if existing_player.get('seat_id'):
                    logger.warning(f"[_add_player_to_game] Player {player_name} is trying to sit in seat {seat_id} but is already in seat {existing_player.get('seat_id')}.")
                    return False, f"Player {player_name} is already seated elsewhere. Please leave your current seat first.", game_state
                else:
                    existing_player['seat_id'] = seat_id
                    existing_player['name'] = player_name
                    existing_player['avatar_url'] = player_data.get('avatar_url')
                    logger.info(f"[_add_player_to_game] Player {player_name} updated with seat {seat_id}.")
        else:
            if any(p.get('seat_id') == seat_id for p in players):
                logger.warning(f"[_add_player_to_game] Seat {seat_id} is already occupied.")
                return False, f"Seat {seat_id} is already occupied by another player.", game_state

            # --- NEWLY ADDED: Fetch player name and avatar from Discord ---
            guild = self.bot.get_guild(int(guild_id)) if guild_id else None
            avatar_url = None
            if guild:
                try:
                    member = await guild.fetch_member(int(player_discord_id))
                    player_name = member.display_name
                    avatar_url = str(member.avatar.url) if member.avatar else str(member.default_avatar.url)
                    logger.debug(f"[_add_player_to_game] Fetched Discord info for new player {player_discord_id}: {player_name}")
                except Exception as e:
                    logger.warning(f"Could not fetch member info for new player {player_discord_id} from Discord: {e}. Using provided name.")
            # --- END OF NEWLY ADDED CODE ---

            new_player = {
                'discord_id': player_discord_id,
                'name': player_name,
                'hand': [],
                'seat_id': seat_id,
                'avatar_url': avatar_url, # Use fetched avatar_url
                'total_chips': 1000, # This will be updated with kekchipz from _load_game_state
                'current_bet_in_round': 0,
                'has_acted_in_round': False,
                'folded': False,
                'hand_revealed': False, # Initialize revealed status
                'kekchipz_overall': 0 # Initialize, will be fetched in _load_game_state
            }
            players.append(new_player)
            logger.info(f"[_add_player_to_game] New player {player_name} added to game state. Current players list: {players}")

        game_state['players'] = players
        
        logger.info(f"[_add_player_to_game] Player {player_name} added/updated in seat {seat_id} in room {room_id}.")
        return True, "Player added successfully.", game_state

    async def _leave_player(self, room_id: str, discord_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Removes a player from the game state for a given room_id."""
        logger.info(f"[_leave_player] Attempting to remove player {discord_id} from room {room_id}.")
        
        initial_player_count = len(game_state.get('players', []))
        game_state['players'] = [p for p in game_state.get('players', []) if p['discord_id'] != discord_id]
        
        if len(game_state['players']) < initial_player_count:
            logger.info(f"[_leave_player] Player {discord_id} removed from room {room_id}.")
            return True, "Player left successfully.", game_state
        else:
            logger.warning(f"[_leave_player] Player {discord_id} not found in room {room_id}.")
            return False, "Player not found in this game.", game_state

    async def _start_new_game(self, room_id: str, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """Resets the game state to start a new game in the specified room."""
        logger.info(f"[_start_new_game] Starting new game for room {room_id}.")

        new_deck = Deck()
        new_deck.build()
        new_deck.shuffle()

        game_state['current_round'] = 'pre_flop'
        game_state['deck'] = new_deck.to_output_format()
        game_state['board_cards'] = []
        game_state['dealer_hand'] = []
        game_state['last_evaluation'] = None
        game_state['current_player_turn_index'] = -1
        game_state['current_betting_round_pot'] = 0
        game_state['current_round_min_bet'] = 0
        game_state['last_aggressive_action_player_id'] = None
        game_state['timer_end_time'] = None

        for player in game_state['players']:
            player['hand'] = []
            player['current_bet_in_round'] = 0
            player['has_acted_in_round'] = False
            player['folded'] = False
            player['hand_revealed'] = False # Reset revealed status
            # When starting a new game, reset total_chips to their overall kekchipz balance
            # This assumes kekchipz_overall has been loaded into the player dict by _load_game_state
            player['total_chips'] = player.get('kekchipz_overall', 1000) # Use fetched kekchipz, or default to 1000

        logger.info(f"[_start_new_game] Game state reset for room {room_id}.")
        return True, "New game started successfully.", game_state

    async def _start_new_round_pre_flop(self, room_id: str, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """
        Starts a new round, dealing hole cards to players and two cards to the dealer,
        and sets the round to 'pre_game'.
        """
        logger.info(f"[_start_new_round_pre_flop] Starting new round pre-game for room {room_id}.")
        
        success_reset, message_reset, game_state = await self._start_new_game(room_id, game_state, guild_id, channel_id)
        if not success_reset:
            logger.error(f"[_start_new_round_pre_flop] Failed to reset game for new round: {message_reset} for room {room_id}.")
            return False, f"Failed to reset game for new round: {message_reset}", game_state

        all_players = game_state.get('players', [])
        seated_players = [p for p in all_players if p.get('seat_id')]

        if len(seated_players) == 0:
            logger.warning(f"[_start_new_round_pre_flop] No players seated in room {room_id}. Cannot start new round.")
            return False, "Cannot start new round, no players available.", game_state
        
        game_state['dealer_button_position'] = (game_state.get('dealer_button_position', -1) + 1) % len(seated_players)
        logger.info(f"[_start_new_round_pre_flop] Dealer button moved to player at index {game_state['dealer_button_position']} for room {room_id}.")
        
        success_players, message_players, game_state = await self.deal_hole_cards(room_id, game_state)
        if not success_players:
            logger.error(f"[_start_new_round_pre_flop] Failed to deal hole cards: {message_players} for room {room_id}.")
            return False, f"Failed to deal hole cards: {message_players}", game_state

        success_dealer, message_dealer, game_state = await self.deal_dealer_cards(room_id, game_state)
        if not success_dealer:
            logger.error(f"[_start_new_round_pre_flop] Failed to deal dealer cards: {message_dealer} for room {room_id}.")
            return False, f"Failed to deal dealer cards: {message_dealer}", game_state

        await self._start_betting_round(room_id, game_state)

        logger.info(f"[_start_new_round_pre_flop] New round for room {room_id} successfully moved to pre_game.")
        
        return True, "New round started, hole cards and dealer cards dealt, moved to pre_game.", game_state

    async def _handle_in_game_message(self, room_id: str, sender_id: str, message_content: str, game_state: dict) -> tuple[bool, str, dict]:
        """
        Handles an in-game message by echoing it back.
        In a real game, this might involve more complex logic like
        broadcasting to other players, logging, or checking for commands.
        """
        logger.info(f"[_handle_in_game_message] Received message for room {room_id} from {sender_id}: '{message_content}'")

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
    try:
        await bot.add_cog(MechanicsMain(bot))
    except Exception as e:
        # Catch and log any errors during cog setup
        logging.error(f"An error occurred during the setup of MechanicsMain cog: {e}", exc_info=True)
        # Flush the log handler to ensure the error is written
        if logging.getLogger().handlers:
            logging.getLogger().handlers[0].flush()
