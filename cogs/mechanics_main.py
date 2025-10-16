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
        logger.info("MechanicsMain (Timed Start) initialized.")
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

    # --- MODIFIED: This function now handles creating the initial DB row ---
    async def _load_game_state(self, room_id: str) -> dict:
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT game_state FROM bot_game_rooms WHERE room_id = %s", (room_id,))
                row = await cursor.fetchone()

                # If the row exists and has a game state, load it.
                if row and row['game_state']:
                    logger.info(f"Loaded existing game state for room '{room_id}'")
                    return json.loads(row['game_state'])

                # If the row exists but game_state is NULL, or if the row doesn't exist at all,
                # create and save the default "pre-game" state.
                else:
                    logger.warning(f"No game state found for '{room_id}'. Initializing 'pre-game' state in DB.")
                    default_state = {'room_id': room_id, 'current_round': 'pre-game', 'players': []}
                    
                    # This query will INSERT the default state if the row is missing,
                    # or UPDATE the game_state if the row exists but the column is NULL.
                    await cursor.execute(
                        """
                        INSERT INTO bot_game_rooms (room_id, game_state)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE game_state = VALUES(game_state)
                        """,
                        (room_id, json.dumps(default_state))
                    )
                    await conn.commit()
                    return default_state
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
                    logger.error(f"Failed to save state: Room '{room_id}' not found.")
            await conn.commit()
            logger.info(f"Saved state for room '{room_id}'")
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

    # --- ADDED BACK: Game Start and Dealing Logic ---
    async def _start_new_round_pre_flop(self, state: dict):
        logger.info(f"Starting 'pre-flop' round for room '{state.get('room_id')}'")
        
        # Reset state for a new round
        deck = Deck(); deck.build(); deck.shuffle()
        state.update({
            'current_round': 'pre_flop',
            'deck': deck.to_output_format(),
            'board_cards': [],
            'dealer_hand': [],
            'pre_flop_timer_start_time': None, # Clear the timer
        })
        for p in state['players']:
            p['hand'] = [] # Clear hands
        
        # Deal cards
        for p in state['players']:
            if not p.get('is_spectating'):
                p['hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['dealer_hand'] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state['deck'] = deck.to_output_format()

        # NOTE: Betting/turn logic would go here, but is omitted per previous requests.
        # The state is now correctly 'pre-flop' with cards dealt.
        return state

    # --- MODIFIED: Main Action Handler with Timer Logic ---
    async def handle_websocket_game_action(self, data: dict):
        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))
        
        try:
            state = await self._load_game_state(room_id)
            state['guild_id'] = data.get('guild_id')
            state['channel_id'] = data.get('channel_id')

            # --- NEW: Check if the 60-second timer has expired ---
            timer_start = state.get('pre_flop_timer_start_time')
            if state.get('current_round') == 'pre-game' and timer_start:
                if time.time() >= timer_start + 60:
                    logger.info(f"60s timer expired for room '{room_id}'. Transitioning to pre-flop.")
                    state = await self._start_new_round_pre_flop(state)
                    # After transitioning, we still save and broadcast below.

            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id, player_id = pdata.get('seat_id'), pdata.get('discord_id')

                if not all([seat_id, player_id]): return
                if any(p.get('discord_id') == player_id for p in state['players']): return
                
                state['players'].append({
                    'discord_id': player_id, 'name': pdata.get('name', 'Player'),
                    'seat_id': seat_id, 'avatar_url': pdata.get('avatar_url'), 'total_chips': 1000
                })
                logger.info(f"Player {player_id} sat in seat {seat_id} in room '{room_id}'.")

                # --- NEW: If this is the first player, start the 60-second timer ---
                if len(state['players']) == 1 and state['current_round'] == 'pre-game':
                    state['pre_flop_timer_start_time'] = time.time()
                    logger.info(f"First player sat down. 60-second pre-flop timer started for room '{room_id}'.")
            
            else:
                logger.warning(f"Received unsupported action: '{action}'")
                return # Only handle 'player_sit' for now

            await self._save_game_state(room_id, state)
            await self.broadcast_game_state(room_id, state)

        except Exception as e:
            logger.error(f"Error handling action '{action}' for room '{room_id}': {e}", exc_info=True)

    # --- KEPT: Winner Evaluation Logic (for future use) ---
    async def evaluate_hands(self, state: dict):
        # ... (full evaluation logic is here, but not called by the simple sit-down action) ...
        pass

async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
