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
        If not found, initializes a new state.
        Fetches the latest kekchipz, display_name, and avatar_url for each player 
        from the discord_users table to ensure data is current.
        """
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE TRIM(room_id) = %s",
                    (room_id,)
                )
                result = await cursor.fetchone()
                
                game_state = {}
                if result and result['game_state']:
                    game_state = json.loads(result['game_state'])
                    logger.info(f"[_load_game_state] Loaded existing game state for room_id: {room_id}. Players count: {len(game_state.get('players', []))}")
                    if 'guild_id' not in game_state or game_state['guild_id'] is None:
                        game_state['guild_id'] = guild_id
                    if 'channel_id' not in game_state or game_state['channel_id'] is None:
                        game_state['channel_id'] = channel_id
                else:
                    logger.warning(f"[_load_game_state] No existing game state found for room_id: {room_id}. Initializing new state.")
                    new_deck = Deck()
                    new_deck.build()
                    new_deck.shuffle()
                    game_state = {
                        'room_id': room_id, 'current_round': 'pre_game', 'players': [], 'dealer_hand': [],
                        'deck': new_deck.to_output_format(), 'board_cards': [], 'last_evaluation': None,
                        'current_player_turn_index': -1, 'current_betting_round_pot': 0, 'current_round_min_bet': 0,
                        'last_aggressive_action_player_id': None, 'timer_end_time': None, 'dealer_button_position': 0,
                        'small_blind_amount': 5, 'big_blind_amount': 10, 'game_started_once': False,
                        'guild_id': guild_id, 'channel_id': channel_id
                    }
    
                # Ensure required fields are present for backward compatibility
                game_state.setdefault('current_player_turn_index', -1)
                game_state.setdefault('current_betting_round_pot', 0)
                game_state.setdefault('current_round_min_bet', 0)
                game_state.setdefault('last_aggressive_action_player_id', None)
                game_state.setdefault('timer_end_time', None)
                game_state.setdefault('dealer_button_position', 0)
                game_state.setdefault('small_blind_amount', 5)
                game_state.setdefault('big_blind_amount', 10)
            
                # Fetch/Update kekchipz, display_name, and avatar_url for each player from the database.
                # This ensures the game state always has the latest user info from the source of truth.
                for player in game_state.get('players', []):
                    player_discord_id = player.get('discord_id')
                    
                    # Set defaults for player object before fetching to ensure keys exist
                    player.setdefault('name', 'Unknown')
                    player.setdefault('avatar_url', None) # Or a default avatar URL
                    player.setdefault('total_chips', 1000)
                    player.setdefault('current_bet_in_round', 0)
                    player.setdefault('has_acted_in_round', False)
                    player.setdefault('folded', False)
                    player.setdefault('kekchipz_overall', 0)

                    if game_state.get('guild_id') and player_discord_id:
                        await cursor.execute(
                            "SELECT kekchipz, display_name, avatar_url FROM discord_users WHERE discord_id = %s AND guild_id = %s",
                            (player_discord_id, game_state['guild_id'])
                        )
                        user_data_result = await cursor.fetchone()
                        if user_data_result:
                            player['kekchipz_overall'] = user_data_result.get('kekchipz', 0)
                            # Update name and avatar from the database as the source of truth
                            player['name'] = user_data_result.get('display_name', player['name'])
                            player['avatar_url'] = user_data_result.get('avatar_url', player['avatar_url'])
                            logger.debug(f"[_load_game_state] Fetched data for player {player_discord_id}: kekchipz={player['kekchipz_overall']}, name={player['name']}.")
                        else:
                            # If no record found, kekchipz_overall remains its default (0)
                            logger.warning(f"[_load_game_state] User record not found for player {player_discord_id} in guild {game_state['guild_id']}. Using defaults.")
                    else:
                        # If no guild_id or player_id, kekchipz_overall remains its default (0)
                        logger.warning(f"[_load_game_state] Missing guild_id or discord_id for player. Cannot fetch user data. Using defaults.")
    
            await conn.commit()
            return game_state
    
        except Exception as e:
            logger.error(f"Error loading game state for room {room_id}: {e}", exc_info=True)
            if conn:
                await conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    async def _save_game_state(self, room_id: str, game_state: dict):
        """
        Saves the game state for a given room_id to the database.
        The game_state dictionary passed here should already be updated with the latest player info.
        """
        room_id_from_state = game_state.get("room_id")
        if room_id_from_state and room_id_from_state != room_id:
            logger.warning(f"[_save_game_state] room_id mismatch: argument={room_id}, game_state={room_id_from_state}")
            room_id = room_id_from_state
    
        room_id = str(room_id).strip()
        # The game_state object now contains the updated player names and avatars from _load_game_state
        game_state_json = json.dumps(game_state)
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                update_query = "UPDATE bot_game_rooms SET game_state = %s WHERE TRIM(room_id) = %s"
                logger.info(f"[_save_game_state] Attempting UPDATE for room_id: '{room_id}'")
                await cursor.execute(update_query, (game_state_json, room_id))

                if cursor.rowcount == 0:
                    logger.error(f"[_save_game_state] UPDATE failed: No rows were affected for room_id: '{room_id}'. This suggests the room does not exist or there is a data inconsistency.")
                    raise ValueError(f"Game room '{room_id}' not found for update, or update failed.")
                else:
                    logger.info(f"[_save_game_state] Successfully updated {cursor.rowcount} row(s) for room_id: '{room_id}'.")
            
            await conn.commit()
        except Exception as e:
            logger.error(f"Error saving game state for room '{room_id}': {e}", exc_info=True)
            if conn:
                await conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
                if logger.handlers:
                    logger.handlers[0].flush()


    async def deal_hole_cards(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals two hole cards to each player for the specified room_id."""
        
        if not game_state.get('players'):
            logger.warning(f"[deal_hole_cards] No players in game for room {room_id}. Cannot deal hole cards.")
            return False, "No players in the game to deal cards.", game_state

        deck = Deck(game_state.get('deck', []))
        if game_state['current_round'] == 'pre_game' or not deck.cards:
            deck.build()
            deck.shuffle()
            logger.info(f"[deal_hole_cards] Deck rebuilt and shuffled for room {room_id}.")
            
        players_data = game_state.get('players', [])
        logger.debug(f"[deal_hole_cards] Players before dealing: {len(players_data)}")

        for player in players_data:
            player['hand'] = []
            player['folded'] = False
            player['current_bet_in_round'] = 0
            player['has_acted_in_round'] = False
            card1 = deck.deal_card()
            card2 = deck.deal_card()
            if card1 and card2:
                player['hand'].append(card1.to_output_format())
                player['hand'].append(card2.to_output_format())
            else:
                logger.error(f"[deal_hole_cards] Not enough cards to deal hole cards for player {player['name']}.")
                return False, "Not enough cards.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['players'] = players_data
        game_state['board_cards'] = []

        logger.info(f"[deal_hole_cards] Hole cards dealt for room {room_id}.")
        return True, "Hole cards dealt.", game_state

    async def deal_dealer_cards(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals two cards to the dealer for the specified room_id."""
        deck = Deck(game_state.get('deck', []))

        if 'dealer_hand' not in game_state or not isinstance(game_state['dealer_hand'], list):
            game_state['dealer_hand'] = []
        else:
            game_state['dealer_hand'].clear()

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
            return False, f"Cannot deal flop. Current round is {game_state['current_round']}.", game_state

        deck = Deck(game_state.get('deck', []))
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        for _ in range(3):
            card = deck.deal_card()
            if card:
                board_cards_output.append(card.to_output_format())
            else:
                return False, "Not enough cards for flop.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "flop"
        
        return True, "Flop dealt.", game_state

    async def deal_turn(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals the fourth community card (turn) for the specified room_id."""
        if game_state['current_round'] != 'flop':
            return False, f"Cannot deal turn. Current round is {game_state['current_round']}.", game_state

        deck = Deck(game_state.get('deck', []))
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        turn_card = deck.deal_card()
        if turn_card:
            board_cards_output.append(turn_card.to_output_format())
        else:
            return False, "Not enough cards for turn.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "turn"

        return True, "Turn dealt.", game_state

    async def deal_river(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Deals the fifth and final community card (river) for the specified room_id."""
        if game_state['current_round'] != 'turn':
            return False, f"Cannot deal river. Current round is {game_state['current_round']}.", game_state

        deck = Deck(game_state.get('deck', []))
        board_cards_output = game_state.get('board_cards', [])

        deck.deal_card() # Burn a card

        river_card = deck.deal_card()
        if river_card:
            board_cards_output.append(river_card.to_output_format())
        else:
            return False, "Not enough cards for river.", game_state

        game_state['deck'] = deck.to_output_format()
        game_state['board_cards'] = board_cards_output
        game_state['current_round'] = "river"

        return True, "River dealt.", game_state

    async def evaluate_hands(self, room_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """
        Evaluates all players' hands against the community cards, determines the winner(s),
        and adds this information to the game state.
        """
        if game_state['current_round'] != 'river':
            return False, f"Cannot evaluate hands. Current round is {game_state['current_round']}.", game_state

        players_data = game_state.get('players', [])
        board_cards_obj = [Card.from_output_format(c_str) for c_str in game_state.get('board_cards', [])]
        active_players = [p for p in players_data if not p.get('folded', False)]

        if len(board_cards_obj) != 5:
            return False, "Board not complete.", game_state

        player_evaluations = []
        best_score = (-1,)
        winning_players = []

        for player_data in active_players:
            player_hand_obj = [Card.from_output_format(c_str) for c_str in player_data.get('hand', [])]
            combined_cards = player_hand_obj + board_cards_obj
            hand_type, hand_score_vector = evaluate_poker_hand(combined_cards)

            player_evaluations.append({
                "discord_id": player_data['discord_id'],
                "name": player_data['name'],
                "hand_type": hand_type,
                "hand_score_vector": hand_score_vector,
                "hole_cards": [c.to_output_format() for c in player_hand_obj],
                "is_winner": False
            })
            
            if hand_score_vector > best_score:
                best_score = hand_score_vector
                winning_players = [player_data['discord_id']]
            elif hand_score_vector == best_score:
                winning_players.append(player_data['discord_id'])

        player_evaluations.sort(key=lambda x: x['hand_score_vector'], reverse=True)

        for eval_data in player_evaluations:
            if eval_data['discord_id'] in winning_players:
                eval_data['is_winner'] = True
        
        winning_hand_name = player_evaluations[0]['hand_type'] if player_evaluations else "N/A"

        game_state['current_round'] = "showdown"
        game_state['last_evaluation'] = {
            "evaluations": player_evaluations,
            "winning_info": {"hand_type": winning_hand_name, "score_vector": best_score, "winners": winning_players}
        }
        game_state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME

        winnings = game_state.get('current_betting_round_pot', 0)
        num_winners = len(winning_players)
        winnings_per_player = winnings // num_winners if num_winners > 0 else 0

        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                for winner_id in winning_players:
                    await cursor.execute(
                        "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE discord_id = %s AND guild_id = %s",
                        (winnings_per_player, winner_id, game_state['guild_id'])
                    )
            await conn.commit()
        except Exception as e:
            logger.error(f"[evaluate_hands] Failed to update kekchipz for winners: {e}", exc_info=True)
            if conn: await conn.rollback()
        finally:
            if conn: conn.close()

        return True, "Hands evaluated.", game_state

    async def broadcast_game_state(self, room_id: str, game_state: dict, echo_message: dict = None):
        """
        Broadcasts the current game state to all connected WebSocket clients in the room.
        """
        if room_id not in self.bot.ws_rooms:
            return

        payload = {"game_state": game_state}
        if echo_message:
            payload["echo_message"] = echo_message
        
        message_json = json.dumps(payload)
        
        for websocket in list(self.bot.ws_rooms[room_id]):
            try:
                await websocket.send_str(message_json)
            except Exception as e:
                logger.error(f"[broadcast_game_state] Error sending WebSocket message: {e}", exc_info=True)

    def _get_sorted_players(self, game_state: dict) -> list:
        """Returns a list of players sorted by their seat_id."""
        players = game_state.get('players', [])
        active_players = [p for p in players if p.get('seat_id') and not p.get('folded', False)]
        return sorted(active_players, key=lambda p: int(p['seat_id'].replace('seat_', '')))

    def _get_next_active_player_index(self, game_state: dict, current_index: int) -> int:
        """Finds the index of the next active player, skipping folded players."""
        sorted_players = self._get_sorted_players(game_state)
        if not sorted_players: return -1

        num_players = len(sorted_players)
        start_search_index = (current_index + 1) % num_players if current_index != -1 else 0

        for i in range(num_players):
            idx = (start_search_index + i) % num_players
            if not sorted_players[idx].get('folded', False):
                return idx
        return -1

    async def _start_player_turn(self, room_id: str, game_state: dict) -> dict:
        """Sets the timer for the current player's turn."""
        sorted_players = self._get_sorted_players(game_state)
        current_player_index = game_state['current_player_turn_index']

        if not sorted_players:
            game_state['timer_end_time'] = None
            game_state['current_player_turn_index'] = -1
            return game_state

        if len(sorted_players) == 1 and current_player_index == -1:
            game_state['current_player_turn_index'] = 0
            current_player_index = 0
        elif current_player_index == -1 or current_player_index >= len(sorted_players):
            game_state['timer_end_time'] = None
            game_state['current_player_turn_index'] = -1
            return game_state

        game_state['timer_end_time'] = int(time.time()) + self.PLAYER_TURN_TIME
        return game_state


    async def _apply_blinds(self, game_state: dict):
        """Applies small and big blinds to players."""
        sorted_players = self._get_sorted_players(game_state)
        num_players = len(sorted_players)
        if num_players == 0: return

        dealer_pos = game_state['dealer_button_position']
        small_blind_pos_idx = (dealer_pos + 1) % num_players
        big_blind_pos_idx = (dealer_pos + 2) % num_players

        small_blind_amount = game_state.get('small_blind_amount', 5)
        big_blind_amount = game_state.get('big_blind_amount', 10)

        small_blind_player = sorted_players[small_blind_pos_idx] if num_players > small_blind_pos_idx else None
        big_blind_player = sorted_players[big_blind_pos_idx] if num_players > big_blind_pos_idx else None

        if small_blind_player:
            amount = min(small_blind_amount, small_blind_player['total_chips'])
            small_blind_player['total_chips'] -= amount
            small_blind_player['current_bet_in_round'] += amount
            game_state['current_betting_round_pot'] += amount
            small_blind_player['has_acted_in_round'] = True

        if big_blind_player:
            amount = min(big_blind_amount, big_blind_player['total_chips'])
            big_blind_player['total_chips'] -= amount
            big_blind_player['current_bet_in_round'] += amount
            game_state['current_betting_round_pot'] += amount
            big_blind_player['has_acted_in_round'] = True

        game_state['current_round_min_bet'] = big_blind_amount if big_blind_player else 0

        for i, p in enumerate(game_state['players']):
            if small_blind_player and p['discord_id'] == small_blind_player['discord_id']:
                game_state['players'][i] = small_blind_player
            elif big_blind_player and p['discord_id'] == big_blind_player['discord_id']:
                game_state['players'][i] = big_blind_player


    async def _start_betting_round(self, room_id: str, game_state: dict) -> dict:
        """Initializes variables for a new betting round."""
        for player in game_state['players']:
            if not player.get('folded', False):
                player['current_bet_in_round'] = 0
                player['has_acted_in_round'] = False
        
        game_state['current_round_min_bet'] = 0
        game_state['last_aggressive_action_player_id'] = None

        sorted_players = self._get_sorted_players(game_state)
        num_players = len(sorted_players)
        if num_players == 0: return game_state

        dealer_pos = game_state['dealer_button_position']
        if game_state['current_round'] == 'pre_flop':
            big_blind_pos_idx = (dealer_pos + 2) % num_players
            first_player_index = (big_blind_pos_idx + 1) % num_players
            await self._apply_blinds(game_state)
            if num_players == 1: first_player_index = 0
        else:
            first_player_index = self._get_next_active_player_index(game_state, dealer_pos)

        if first_player_index != -1:
            game_state['current_player_turn_index'] = first_player_index
            game_state = await self._start_player_turn(room_id, game_state)
        else:
            game_state = await self._advance_game_phase(room_id, game_state)
        return game_state


    async def _end_betting_round(self, room_id: str, game_state: dict) -> dict:
        """Collects bets into the main pot and prepares for the next phase."""
        for player in game_state['players']:
            game_state['current_betting_round_pot'] += player.get('current_bet_in_round', 0)
            player['current_bet_in_round'] = 0
            player['has_acted_in_round'] = False

        game_state['current_round_min_bet'] = 0
        game_state['last_aggressive_action_player_id'] = None
        return game_state


    def _check_round_completion(self, game_state: dict) -> bool:
        """Checks if the current betting round is complete."""
        sorted_players = self._get_sorted_players(game_state)
        active_players = [p for p in sorted_players if not p.get('folded', False)]

        if len(active_players) <= 1: return True

        highest_bet = max(p.get('current_bet_in_round', 0) for p in active_players)

        all_settled = all(
            p.get('has_acted_in_round', False) and
            (p.get('current_bet_in_round', 0) == highest_bet or p.get('total_chips', 0) == 0)
            for p in active_players
        )

        if not all_settled: return False

        current_player_index = game_state['current_player_turn_index']
        current_player_id = sorted_players[current_player_index]['discord_id'] if current_player_index != -1 and current_player_index < len(sorted_players) else None
        last_aggressive_id = game_state['last_aggressive_action_player_id']

        if last_aggressive_id is None: return True
        if current_player_id == last_aggressive_id: return True
        
        last_aggressive_player = next((p for p in game_state['players'] if p['discord_id'] == last_aggressive_id), None)
        if last_aggressive_player and last_aggressive_player.get('folded', False): return True

        return False

    async def _advance_game_phase(self, room_id: str, game_state: dict) -> dict:
        """Moves the game to the next phase (flop, turn, river, showdown)."""
        game_state = await self._end_betting_round(room_id, game_state)

        round_map = {
            'pre_flop': (self.deal_flop, 'flop'),
            'flop': (self.deal_turn, 'turn'),
            'turn': (self.deal_river, 'river'),
            'river': (self.evaluate_hands, 'showdown'),
            'showdown': (self._start_new_round_pre_flop, 'pre_flop')
        }
        
        current_round = game_state['current_round']
        if current_round in round_map:
            action, next_round = round_map[current_round]
            if current_round == 'showdown':
                 success, msg, game_state = await action(room_id, game_state, game_state['guild_id'], game_state['channel_id'])
            else:
                success, msg, game_state = await action(room_id, game_state)

            if not success:
                logger.error(f"[_advance_game_phase] Failed to advance from {current_round}: {msg}")
                return game_state

            if next_round in ['pre_flop', 'flop', 'turn', 'river']:
                game_state = await self._start_betting_round(room_id, game_state)
        
        return game_state


    async def handle_websocket_game_action(self, request_data: dict):
        """Receives and dispatches WebSocket game actions."""
        action = request_data.get('action')
        room_id = request_data.get('room_id')
        guild_id = request_data.get('guild_id')    
        sender_id = request_data.get('sender_id')
        channel_id = request_data.get('channel_id')

        if not all([action, room_id, guild_id, sender_id]):
            logger.error(f"WS message missing critical parameters: {request_data}")
            return

        success = False
        message = "Unknown action."
        echo_message_data = None
        game_state = {}

        try:
            game_state = await self._load_game_state(room_id, guild_id, channel_id)
            
            mutating_actions = ["add_player", "leave_player", "start_new_round_pre_flop", "player_action", "auto_action_timeout"]
            
            handler_map = {
                "get_state": lambda: (True, "Game state retrieved.", game_state),
                "add_player": lambda: self._add_player_to_game(room_id, request_data.get('player_data'), game_state, guild_id, channel_id),
                "leave_player": lambda: self._leave_player(room_id, request_data.get('discord_id'), game_state),
                "start_new_round_pre_flop": lambda: self._start_new_round_pre_flop(room_id, game_state, guild_id, channel_id) if game_state.get('current_round') in ['pre_game', 'showdown'] else (False, "Game in progress.", game_state),
                "player_action": lambda: self._handle_player_action(room_id, request_data.get('player_id'), request_data.get('action_type'), request_data.get('amount', 0), game_state),
                "auto_action_timeout": lambda: self._auto_action_on_timeout(room_id, request_data.get('player_id'), game_state),
                "send_message": lambda: self._handle_in_game_message(room_id, sender_id, request_data.get('message_content'), game_state)
            }

            if action in handler_map:
                if action == "send_message":
                    success, message, response_data = await handler_map[action]()
                    echo_message_data = response_data.get('echo_message')
                    game_state = response_data.get('game_state', game_state)
                else:
                    success, message, game_state = await handler_map[action]()
            else:
                logger.warning(f"Unsupported WS action: {action}")
                return

            if success:
                if action in mutating_actions:
                    await self._save_game_state(room_id, game_state)
                await self.broadcast_game_state(room_id, game_state, echo_message_data)
            else:
                logger.warning(f"Action '{action}' failed for room {room_id}: {message}")

        except Exception as e:
            logger.error(f"Unhandled exception in handle_websocket_game_action for action '{action}': {e}", exc_info=True)
            if logger.handlers: logger.handlers[0].flush()
            raise


    async def _handle_player_action(self, room_id: str, player_id: str, action_type: str, amount: int = 0, game_state: dict = None) -> tuple[bool, str, dict]:
        """Processes a player's action (call, check, bet, raise, fold)."""
        if game_state is None: return False, "Internal error: Game state not provided.", game_state

        sorted_players = self._get_sorted_players(game_state)
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state: return False, "Player not found in game.", game_state

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 else None
        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            return False, "It's not your turn.", game_state
        if player_in_state.get('folded', False): return False, "You have already folded.", game_state

        min_bet_to_call = game_state.get('current_round_min_bet', 0) - player_in_state.get('current_bet_in_round', 0)
        
        success, message = False, ""
        if action_type == 'fold':
            player_in_state['folded'] = True
            success, message = True, f"{player_in_state['name']} folded."
        elif action_type == 'check':
            if min_bet_to_call > 0: return False, "Cannot check, a bet has been made.", game_state
            success, message = True, f"{player_in_state['name']} checked."
        elif action_type == 'call':
            bet_amount = min(min_bet_to_call, player_in_state['total_chips'])
            player_in_state['total_chips'] -= bet_amount
            player_in_state['current_bet_in_round'] += bet_amount
            success, message = True, f"{player_in_state['name']} called ${bet_amount}."
        elif action_type in ['bet', 'raise']:
            if amount <= min_bet_to_call: return False, f"Bet/Raise must be greater than ${min_bet_to_call}.", game_state
            if player_in_state['total_chips'] < amount: return False, "Not enough chips.", game_state
            player_in_state['total_chips'] -= amount
            player_in_state['current_bet_in_round'] += amount
            game_state['current_round_min_bet'] = player_in_state['current_bet_in_round']
            game_state['last_aggressive_action_player_id'] = player_id
            for p in game_state['players']:
                if p['discord_id'] != player_id and not p.get('folded', False): p['has_acted_in_round'] = False
            success, message = True, f"{player_in_state['name']} {action_type}d ${amount}."
        elif action_type == 'all_in':
            amount = player_in_state['total_chips']
            if amount == 0: return False, "You have no chips to go all-in.", game_state
            player_in_state['total_chips'] = 0
            player_in_state['current_bet_in_round'] += amount
            if player_in_state['current_bet_in_round'] > game_state['current_round_min_bet']:
                game_state['current_round_min_bet'] = player_in_state['current_bet_in_round']
                game_state['last_aggressive_action_player_id'] = player_id
                for p in game_state['players']:
                    if p['discord_id'] != player_id and not p.get('folded', False): p['has_acted_in_round'] = False
            success, message = True, f"{player_in_state['name']} went All-In with ${amount}!"
        
        if success:
            player_in_state['has_acted_in_round'] = True
            if self._check_round_completion(game_state):
                game_state = await self._advance_game_phase(room_id, game_state)
            else:
                next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
                if next_player_idx != -1:
                    game_state['current_player_turn_index'] = next_player_idx
                    game_state = await self._start_player_turn(room_id, game_state)
                else:
                    game_state = await self._advance_game_phase(room_id, game_state)
            return True, message, game_state
        return False, "Invalid action.", game_state

    async def _auto_action_on_timeout(self, room_id: str, player_id: str, game_state: dict = None) -> tuple[bool, str, dict]:
        """Performs an automatic action (check/fold) for a player whose turn timed out."""
        if game_state is None: return False, "Internal error: Game state not provided.", game_state

        sorted_players = self._get_sorted_players(game_state)
        player_in_state = next((p for p in game_state['players'] if p['discord_id'] == player_id), None)
        if not player_in_state: return False, "Player not found.", game_state

        current_player_turn_obj = sorted_players[game_state['current_player_turn_index']] if game_state['current_player_turn_index'] != -1 else None
        if not current_player_turn_obj or current_player_turn_obj['discord_id'] != player_id:
            return False, "Timeout for incorrect player.", game_state
        if int(time.time()) < game_state.get('timer_end_time', 0):
            return False, "Turn has not timed out yet.", game_state

        min_bet_to_call = game_state.get('current_round_min_bet', 0) - player_in_state.get('current_bet_in_round', 0)
        
        if min_bet_to_call > 0:
            player_in_state['folded'] = True
            action_message = f"{player_in_state['name']} auto-folded."
        else:
            action_message = f"{player_in_state['name']} auto-checked."
        
        player_in_state['has_acted_in_round'] = True
        
        if self._check_round_completion(game_state):
            game_state = await self._advance_game_phase(room_id, game_state)
        else:
            next_player_idx = self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
            if next_player_idx != -1:
                game_state['current_player_turn_index'] = next_player_idx
                game_state = await self._start_player_turn(room_id, game_state)
            else:
                game_state = await self._advance_game_phase(room_id, game_state)

        return True, action_message, game_state


    async def _add_player_to_game(self, room_id: str, player_data: dict, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """Adds a player to the game, ensuring seat is not occupied."""
        players = game_state.get('players', [])
        player_discord_id = player_data['discord_id']
        seat_id = player_data.get('seat_id')

        if not seat_id: return False, "Seat ID is required.", game_state
        if any(p.get('seat_id') == seat_id for p in players): return False, "Seat is occupied.", game_state
        if any(p['discord_id'] == player_discord_id for p in players): return False, "Player already seated.", game_state

        new_player = {
            'discord_id': player_discord_id,
            'name': player_data.get('name', 'Unknown'),
            'hand': [], 'seat_id': seat_id,
            'avatar_url': player_data.get('avatar_url'),
            'total_chips': 1000, 'current_bet_in_round': 0,
            'has_acted_in_round': False, 'folded': False, 'kekchipz_overall': 0
        }
        players.append(new_player)
        game_state['players'] = players
        
        return True, "Player added.", game_state

    async def _leave_player(self, room_id: str, discord_id: str, game_state: dict) -> tuple[bool, str, dict]:
        """Removes a player from the game."""
        initial_count = len(game_state.get('players', []))
        game_state['players'] = [p for p in game_state.get('players', []) if p['discord_id'] != discord_id]
        if len(game_state['players']) < initial_count:
            return True, "Player left.", game_state
        return False, "Player not found.", game_state

    async def _start_new_game(self, room_id: str, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """Resets the game state for a new game."""
        new_deck = Deck()
        new_deck.build()
        new_deck.shuffle()

        game_state.update({
            'current_round': 'pre_flop', 'deck': new_deck.to_output_format(),
            'board_cards': [], 'dealer_hand': [], 'last_evaluation': None,
            'current_player_turn_index': -1, 'current_betting_round_pot': 0,
            'current_round_min_bet': 0, 'last_aggressive_action_player_id': None,
            'timer_end_time': None
        })

        for player in game_state['players']:
            player.update({
                'hand': [], 'current_bet_in_round': 0,
                'has_acted_in_round': False, 'folded': False,
                'total_chips': player.get('kekchipz_overall', 1000)
            })
        return True, "New game started.", game_state

    async def _start_new_round_pre_flop(self, room_id: str, game_state: dict, guild_id: str = None, channel_id: str = None) -> tuple[bool, str, dict]:
        """Starts a new round (deals cards, sets blinds, starts betting)."""
        success, msg, game_state = await self._start_new_game(room_id, game_state, guild_id, channel_id)
        if not success: return False, msg, game_state

        sorted_players = self._get_sorted_players(game_state)
        if not sorted_players: return False, "No players to start.", game_state
        
        game_state['dealer_button_position'] = (game_state.get('dealer_button_position', -1) + 1) % len(sorted_players)
        
        success, msg, game_state = await self.deal_hole_cards(room_id, game_state)
        if not success: return False, msg, game_state

        success, msg, game_state = await self.deal_dealer_cards(room_id, game_state)
        if not success: return False, msg, game_state

        await self._start_betting_round(room_id, game_state)
        
        return True, "New round started.", game_state

    async def _handle_in_game_message(self, room_id: str, sender_id: str, message_content: str, game_state: dict) -> tuple[bool, str, dict]:
        """Handles an in-game chat message."""
        sender_name = next((p['name'] for p in game_state.get('players', []) if p['discord_id'] == sender_id), "Unknown")
        response_data = {
            "echo_message": {"sender_id": sender_id, "sender_name": sender_name, "content": message_content},
            "game_state": game_state
        }
        return True, "Message processed.", response_data


async def setup(bot):
    try:
        await bot.add_cog(MechanicsMain(bot))
    except Exception as e:
        logging.error(f"Error setting up MechanicsMain cog: {e}", exc_info=True)
        if logging.getLogger().handlers: logging.getLogger().handlers[0].flush()
