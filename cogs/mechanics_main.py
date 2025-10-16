import logging
import json
import aiomysql
import time
from discord.ext import commands
from itertools import combinations

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Texas/Casino Hold'em Hand Evaluation Logic (Unchanged) ---
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

def get_rank_value(rank: str) -> int:
    """
    Map rank string to numeric value.
    This implementation expects '0' to represent Ten (10).
    Valid ranks: '2'..'9', '0' (Ten), 'J','Q','K','A'
    """
    if rank.isdigit():
        if rank == '0':
            return 10
        return int(rank)
    return {'J': 11, 'Q': 12, 'K': 13, 'A': 14}.get(rank, 0)

def evaluate_poker_hand(cards):
    def rank_value(card): return get_rank_value(card.rank)

    def is_straight(ranks):
        ranks = sorted(set(ranks), reverse=True)
        if {14, 2, 3, 4, 5}.issubset(set(ranks)):
            return True, 5
        for i in range(len(ranks) - 4):
            window = ranks[i:i+5]
            if all(window[j] - window[j+1] == 1 for j in range(4)):
                return True, window[0]
        return False, None

    def classify(hand):
        ranks = sorted([rank_value(c) for c in hand], reverse=True)
        suits = [c.suit[0].upper() for c in hand]
        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        cg = sorted(rank_counts.items(), key=lambda x: (-x[1], -x[0]))
        grouped = [r for r, _ in cg]

        flush = len(set(suits)) == 1
        straight, hi = is_straight(ranks)

        if flush and straight:
            if hi == 14:
                return "Royal Flush", (HAND_RANKINGS["Royal Flush"],)
            return "Straight Flush", (HAND_RANKINGS["Straight Flush"], hi)
        if cg[0][1] == 4:
            return "Four of a Kind", (HAND_RANKINGS["Four of a Kind"], cg[0][0], grouped[1])
        if cg[0][1] == 3 and cg[1][1] >= 2:
            return "Full House", (HAND_RANKINGS["Full House"], cg[0][0], cg[1][0])
        if flush:
            return "Flush", (HAND_RANKINGS["Flush"], *ranks)
        if straight:
            return "Straight", (HAND_RANKINGS["Straight"], hi)
        if cg[0][1] == 3:
            return "Three of a Kind", (HAND_RANKINGS["Three of a Kind"], cg[0][0], *grouped[1:3])
        if cg[0][1] == 2 and cg[1][1] == 2:
            return "Two Pair", (HAND_RANKINGS["Two Pair"], cg[0][0], cg[1][0], grouped[2])
        if cg[0][1] == 2:
            return "One Pair", (HAND_RANKINGS["One Pair"], cg[0][0], *grouped[1:4])
        return "High Card", (HAND_RANKINGS["High Card"], *ranks)

    best_name, best_score = "", (-1,)
    for combo in combinations(cards, 5):
        name, score = classify(combo)
        if score > best_score:
            best_score, best_name = score, name
    return best_name, best_score


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    """
    A stripped-down version of the game mechanics, focused only on
    loading game state and evaluating winners. All interactive elements
    (player actions, dealing, betting, WebSockets) have been removed.
    """
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain (Evaluation-Only) initialized.")
        self.db_user = self.bot.db_user
        self.db_password = self.bot.db_password
        self.db_host = self.bot.db_host
        self.db_name = "serene_users"

    async def cog_load(self):
        logger.info("MechanicsMain (Evaluation-Only) cog loaded.")

    async def cog_unload(self):
        logger.info("MechanicsMain (Evaluation-Only) cog unloaded.")

    # ---- KEPT: Core DB and Loading Logic ----
    async def _get_db_connection(self):
        if not all([self.db_user, self.db_password, self.db_host, self.db_name]):
            logger.error("DB credentials missing in MechanicsMain.")
            raise ConnectionError("Database credentials not configured.")
        return await aiomysql.connect(
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            db=self.db_name,
            charset='utf8mb4',
            autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_game_state(self, room_id: str, guild_id: str = None, channel_id: str = None) -> dict:
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE TRIM(room_id) = %s",
                    (str(room_id).strip(),)
                )
                row = await cursor.fetchone()
                if row and row['game_state']:
                    return json.loads(row['game_state'])
                else:
                    # If no state is found, return None or an empty dict
                    # instead of creating a new game.
                    return None
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"_load_game_state error for room {room_id}: {e}", exc_info=True)
            raise
        finally:
            if conn: conn.close()

    # ---- KEPT: Winner Evaluation Logic ----
    async def evaluate_hands(self, state: dict):
        """
        Takes a game state dictionary, evaluates player and dealer hands,
        and returns a dictionary with the evaluation results.
        This version does NOT handle payouts or modify player chip counts.
        """
        if not state or not isinstance(state, dict):
            return None, "Invalid state provided."

        players = state.get('players', [])
        board_cards = state.get('board_cards', [])
        dealer_hand = state.get('dealer_hand', [])

        # Basic validation for evaluation
        if len(board_cards) != 5:
            return None, "Cannot evaluate: Board is not complete (requires 5 cards)."
        if not players:
            return None, "Cannot evaluate: No players in the state."

        board = [Card.from_output_format(c) for c in board_cards]
        dealer = [Card.from_output_format(c) for c in dealer_hand]
        active_players = [p for p in players if not p.get('folded', False) and not p.get('is_spectating')]
        
        d_name, d_score = evaluate_poker_hand(dealer + board)
        dealer_eval = {
            "name": "Dealer",
            "hand_type": d_name,
            "hand_score_vector": d_score,
            "hole_cards": [c.to_output_format() for c in dealer]
        }

        evals = []
        best_player_score = d_score
        winners = []

        for p in active_players:
            hand = [Card.from_output_format(c) for c in p.get('hand', [])]
            if len(hand) != 2:
                continue # Skip players with incomplete hands

            name, score = evaluate_poker_hand(hand + board)
            evals.append({
                "discord_id": p['discord_id'],
                "name": p['name'],
                "hand_type": name,
                "hand_score_vector": score,
                "hole_cards": [c.to_output_format() for c in hand],
                "is_winner": False
            })
            if score > best_player_score:
                best_player_score = score
                winners = [p['discord_id']]
            elif score == best_player_score and best_player_score > d_score:
                winners.append(p['discord_id'])

        evals.sort(key=lambda x: x['hand_score_vector'], reverse=True)
        for e in evals:
            if e['discord_id'] in winners:
                e['is_winner'] = True
        
        winning_hand_name = evals[0]['hand_type'] if winners and evals else d_name

        # The final evaluation result to be returned
        final_evaluation = {
            "dealer_evaluation": dealer_eval,
            "player_evaluations": evals,
            "winning_info": {
                "hand_type": winning_hand_name,
                "score_vector": best_player_score,
                "winner_ids": winners
            }
        }
        
        return final_evaluation, "Hands evaluated successfully."

    # --- ALL INTERACTIVE METHODS HAVE BEEN REMOVED ---
    # - handle_websocket_game_action
    # - _add_player_to_game, _leave_player
    # - _start_new_round_pre_flop, _start_new_game
    # - All dealing functions (deal_hole_cards, deal_flop, etc.)
    # - All betting/turn functions (_start_betting_round, _handle_player_action, etc.)
    # - _save_game_state, broadcast_game_state

# Required by bot.py to load the cog
async def setup(bot):
    try:
        await bot.add_cog(MechanicsMain(bot))
    except Exception as e:
        logging.error(f"MechanicsMain setup error: {e}", exc_info=True)
