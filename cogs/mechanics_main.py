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


# In cogs/mechanics_main.py

class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain (Active Timer) initialized.")
        self.db_user = bot.db_user
        self.db_password = bot.db_password
        self.db_host = bot.db_host
        self.db_name = "serene_users"
        if not hasattr(bot, "ws_rooms"): bot.ws_rooms = {}
        
        # A set to track rooms that need timer checks to avoid scanning the entire DB
        self.rooms_with_active_timers = set()
        
        # Start the background task
        self.check_game_timers.start()

    def cog_unload(self):
        # Gracefully stop the task when the cog is unloaded
        self.check_game_timers.cancel()

    # --- All helper functions (_normalize_room_id, etc.) remain the same ---
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

    async def _load_game_state(self, room_id: str) -> dict:
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT game_state FROM bot_game_rooms WHERE room_id = %s", (room_id,))
                row = await cursor.fetchone()
                if row and row.get('game_state'):
                    return json.loads(row['game_state'])
                return None
        finally:
            if conn: conn.close()

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
                    logger.error(f"CRITICAL: Failed to save state. Room with room_id '{room_id}' was not found for update.")
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

    async def _start_new_round_pre_flop(self, state: dict):
        logger.info(f"Starting 'pre-flop' round for room '{state.get('room_id')}'")
        deck = Deck(); deck.build(); deck.shuffle()
        state.update({
            'current_round': 'pre_flop', 'deck': deck.to_output_format(), 'board_cards': [],
            'dealer_hand': [], 'pre_flop_timer_start_time': None,
        })
        for p in state['players']: p['hand'] = []
        for p in state['players']:
            if not p.get('is_spectating'):
                p['hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['dealer_hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['deck'] = deck.to_output_format()
        return state

    # --- NEW: Active Background Task for Timers ---
    @tasks.loop(seconds=5.0) # Check every 5 seconds for expired timers
    async def check_game_timers(self):
        if not self.rooms_with_active_timers:
            return

        for room_id in list(self.rooms_with_active_timers):
            try:
                state = await self._load_game_state(room_id)
                if not state:
                    self.rooms_with_active_timers.discard(room_id)
                    continue

                timer_start = state.get('pre_flop_timer_start_time')
                if state.get('current_round') == 'pre-game' and timer_start and time.time() >= timer_start + 60:
                    logger.info(f"[TIMER TASK] 60s timer expired for room '{room_id}'. Transitioning.")
                    
                    state = await self._start_new_round_pre_flop(state)
                    
                    await self._save_game_state(room_id, state)
                    await self.broadcast_game_state(room_id, state)

                    self.rooms_with_active_timers.discard(room_id)

            except Exception as e:
                logger.error(f"[TIMER TASK] Error checking room '{room_id}': {e}", exc_info=True)
                self.rooms_with_active_timers.discard(room_id) # Remove on error

    @check_game_timers.before_loop
    async def before_check_game_timers(self):
        await self.bot.wait_until_ready()

    # --- MODIFIED: Action Handler now adds rooms to the timer task ---
    async def handle_websocket_game_action(self, data: dict):
        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))
        
        try:
            state = await self._load_game_state(room_id)
            if state is None:
                state = {'room_id': room_id, 'current_round': 'pre-game', 'players': []}
            
            state['guild_id'] = data.get('guild_id')
            state['channel_id'] = data.get('channel_id')
            
            # --- The passive check is still useful for immediate responsiveness ---
            timer_start = state.get('pre_flop_timer_start_time')
            if state.get('current_round') == 'pre-game' and timer_start and time.time() >= timer_start + 60:
                logger.info(f"[ACTION HANDLER] Timer expired for '{room_id}'. Transitioning.")
                state = await self._start_new_round_pre_flop(state)
                self.rooms_with_active_timers.discard(room_id) # Remove from active checks

            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id, player_id = pdata.get('seat_id'), pdata.get('discord_id')

                if not all([seat_id, player_id]) or any(p.get('discord_id') == player_id for p in state['players']):
                    return
                
                state['players'].append({
                    'discord_id': player_id, 'name': pdata.get('name', 'Player'),
                    'seat_id': seat_id, 'avatar_url': pdata.get('avatar_url'), 'total_chips': 1000
                })

                if len(state['players']) == 1 and state['current_round'] == 'pre-game' and not state.get('initial_countdown_triggered'):
                    state['pre_flop_timer_start_time'] = time.time()
                    state['initial_countdown_triggered'] = True
                    # --- Add this room to the set of rooms the background task needs to check ---
                    self.rooms_with_active_timers.add(room_id)
                    logger.info(f"First player sat. Room '{room_id}' added to active timer checks.")
            
            elif action is not None: pass
            else: return

            await self._save_game_state(room_id, state)
            await self.broadcast_game_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

    async def evaluate_hands(self, state: dict): pass

async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
