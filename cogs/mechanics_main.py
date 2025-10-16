import logging
import json
import aiomysql
import time
from discord.ext import commands
from itertools import combinations

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Hand Evaluation Logic (Unchanged) ---
HAND_RANKINGS = {
    "High Card": 0, "One Pair": 1, "Two Pair": 2, "Three of a Kind": 3, "Straight": 4,
    "Flush": 5, "Full House": 6, "Four of a Kind": 7, "Straight Flush": 8, "Royal Flush": 9
}
def get_rank_value(rank: str) -> int:
    if rank.isdigit(): return 10 if rank == '0' else int(rank)
    return {'J': 11, 'Q': 12, 'K': 13, 'A': 14}.get(rank, 0)
def evaluate_poker_hand(cards):
    def rank_value(card): return get_rank_value(card.rank)
    def is_straight(ranks):
        ranks = sorted(set(ranks), reverse=True)
        if {14, 2, 3, 4, 5}.issubset(set(ranks)): return True, 5
        for i in range(len(ranks) - 4):
            window = ranks[i:i+5]
            if all(window[j] - window[j+1] == 1 for j in range(4)): return True, window[0]
        return False, None
    def classify(hand):
        ranks = sorted([rank_value(c) for c in hand], reverse=True)
        suits = [c.suit[0].upper() for c in hand]
        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        cg = sorted(rank_counts.items(), key=lambda x: (-x[1], -x[0]))
        grouped = [r for r, _ in cg]
        flush = len(set(suits)) == 1
        straight, hi = is_straight(ranks)
        if flush and straight: return ("Royal Flush", (HAND_RANKINGS["Royal Flush"],)) if hi == 14 else ("Straight Flush", (HAND_RANKINGS["Straight Flush"], hi))
        if cg[0][1] == 4: return "Four of a Kind", (HAND_RANKINGS["Four of a Kind"], cg[0][0], grouped[1])
        if cg[0][1] == 3 and cg[1][1] >= 2: return "Full House", (HAND_RANKINGS["Full House"], cg[0][0], cg[1][0])
        if flush: return "Flush", (HAND_RANKINGS["Flush"], *ranks)
        if straight: return "Straight", (HAND_RANKINGS["Straight"], hi)
        if cg[0][1] == 3: return "Three of a Kind", (HAND_RANKINGS["Three of a Kind"], cg[0][0], *grouped[1:3])
        if cg[0][1] == 2 and cg[1][1] == 2: return "Two Pair", (HAND_RANKINGS["Two Pair"], cg[0][0], cg[1][0], grouped[2])
        if cg[0][1] == 2: return "One Pair", (HAND_RANKINGS["One Pair"], cg[0][0], *grouped[1:4])
        return "High Card", (HAND_RANKINGS["High Card"], *ranks)
    best_name, best_score = "", (-1,)
    for combo in combinations(cards, 5):
        name, score = classify(combo)
        if score > best_score: best_score, best_name = score, name
    return best_name, best_score


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain (Update-Only) initialized.")
        self.db_user = bot.db_user
        self.db_password = bot.db_password
        self.db_host = bot.db_host
        self.db_name = "serene_users"
        if not hasattr(bot, "ws_rooms"): bot.ws_rooms = {}

    def _normalize_room_id(self, room_id: str) -> str:
        if not room_id: raise ValueError("room_id missing")
        return str(room_id).strip()

    def register_ws_connection(self, ws, room_id: str):
        rid = self._normalize_room_id(room_id)
        self.bot.ws_rooms.setdefault(rid, set()).add(ws)
        setattr(ws, "_assigned_room", rid)
        return True

    def unregister_ws_connection(self, ws):
        room = getattr(ws, "_assigned_room", None)
        if room in self.bot.ws_rooms:
            self.bot.ws_rooms[room].discard(ws)
            if not self.bot.ws_rooms[room]: del self.bot.ws_rooms[room]
    
    async def player_connect(self, *args, **kwargs): return True, ""
    async def player_disconnect(self, *args, **kwargs): return True, ""

    async def _get_db_connection(self):
        return await aiomysql.connect(
            host=self.db_host, user=self.db_user, password=self.db_password,
            db=self.db_name, charset='utf8mb4', autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    # --- CORRECTED: This function is now READ-ONLY ---
    async def _load_game_state(self, room_id: str) -> dict:
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT game_state FROM bot_game_rooms WHERE room_id = %s", (room_id,))
                row = await cursor.fetchone()

                if row and row.get('game_state'):
                    logger.info(f"Loaded existing game state for room '{room_id}'")
                    return json.loads(row['game_state'])
                
                # If row is not found, or game_state is NULL, return None.
                # The action handler is now responsible for initialization.
                logger.warning(f"No valid game state found in DB for room '{room_id}'. The row may be missing or the game_state column is NULL.")
                return None
        finally:
            if conn: conn.close()

    # --- CORRECTED: This function is now UPDATE-ONLY ---
    async def _save_game_state(self, room_id: str, state: dict):
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                rows_affected = await cursor.execute(
                    "UPDATE bot_game_rooms SET game_state = %s WHERE room_id = %s",
                    (json.dumps(state), room_id)
                )
                if rows_affected == 0:
                    logger.error(f"CRITICAL: Failed to save state. Room with room_id '{room_id}' was not found in the database for update.")
            await conn.commit()
            logger.info(f"Successfully saved (updated) game state for room '{room_id}'")
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"DB save error for room '{room_id}': {e}", exc_info=True)
            raise

    async def broadcast_game_state(self, room_id: str, state: dict):
        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        envelope = {"game_state": state, "server_ts": int(time.time())}
        msg = json.dumps(envelope)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)

    # --- Game Start Logic (needed for the timer) ---
    async def _start_new_round_pre_flop(self, state: dict):
        logger.info(f"Starting 'pre-flop' round for room '{state.get('room_id')}'")
        
        deck = Deck(); deck.build(); deck.shuffle()
        state.update({
            'current_round': 'pre_flop',
            'deck': deck.to_output_format(),
            'board_cards': [],
            'dealer_hand': [],
            'pre_flop_timer_start_time': None,
        })
        for p in state['players']: p['hand'] = []
        
        for p in state['players']:
            if not p.get('is_spectating'):
                p['hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['dealer_hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['deck'] = deck.to_output_format()
        
        return state

    # --- CORRECTED: Main Action Handler with Fixed Logic Flow ---
    async def handle_websocket_game_action(self, data: dict):
        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))
        
        try:
            state = await self._load_game_state(room_id)

            if state is None:
                logger.info(f"Loaded empty state for '{room_id}'. Initializing 'pre-game' state in memory.")
                state = {
                    'room_id': room_id,
                    'current_round': 'pre_game',
                    'players': []
                }
            
            state['guild_id'] = data.get('guild_id')
            state['channel_id'] = data.get('channel_id')

            timer_start = state.get('pre_flop_timer_start_time')
            if state.get('current_round') == 'pre-game' and timer_start:
                if time.time() >= timer_start + 60:
                    logger.info(f"60s timer expired for room '{room_id}'. Transitioning to pre-flop.")
                    state = await self._start_new_round_pre_flop(state)

            # --- START of THE FIX ---
            # This logic now correctly handles 'player_sit' and rejects unknown actions.
            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id, player_id = pdata.get('seat_id'), pdata.get('discord_id')

                if not all([seat_id, player_id]):
                    return # Exit if data is incomplete

                if any(p.get('discord_id') == player_id for p in state['players']):
                    return # Exit if player is already seated
                
                state['players'].append({
                    'discord_id': player_id, 'name': pdata.get('name', 'Player'),
                    'seat_id': seat_id, 'avatar_url': pdata.get('avatar_url'), 'total_chips': 1000
                })
                logger.info(f"Player {player_id} sat in seat {seat_id} in room '{room_id}'.")

                if len(state['players']) == 1 and state['current_round'] == 'pre-game':
                    state['pre_flop_timer_start_time'] = time.time()
                    logger.info(f"First player sat down. 60-second pre-flop timer started for room '{room_id}'.")
            
            else:
                logger.warning(f"Received unknown or unsupported action: '{action}'")
                return # Exit for any action that is NOT 'player_sit'
            # --- END of THE FIX ---

            # These lines will now be correctly executed after a 'player_sit' action.
            await self._save_game_state(room_id, state)
            await self.broadcast_game_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

    async def evaluate_hands(self, state: dict):
        pass # Evaluation logic remains here for future use

async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
