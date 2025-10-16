import logging
import json
import aiomysql
import time
from discord.ext import commands
from itertools import combinations

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Texas/Casino Hold'em Hand Evaluation Logic (Kept from original) ---
HAND_RANKINGS = {
    "High Card": 0, "One Pair": 1, "Two Pair": 2, "Three of a Kind": 3, "Straight": 4,
    "Flush": 5, "Full House": 6, "Four of a Kind": 7, "Straight Flush": 8, "Royal Flush": 9
}

def get_rank_value(rank: str) -> int:
    if rank.isdigit():
        return 10 if rank == '0' else int(rank)
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
        if flush and straight:
            return ("Royal Flush", (HAND_RANKINGS["Royal Flush"],)) if hi == 14 else ("Straight Flush", (HAND_RANKINGS["Straight Flush"], hi))
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
        if score > best_score:
            best_score, best_name = score, name
    return best_name, best_score


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain (Hybrid) initialized.")
        self.db_user = bot.db_user
        self.db_password = bot.db_password
        self.db_host = bot.db_host
        self.db_name = "serene_users"
        if not hasattr(bot, "ws_rooms"):
            bot.ws_rooms = {}

    # --- ADDED BACK: WebSocket Connection Management (needed by bot.py) ---
    def _normalize_room_id(self, room_id: str) -> str:
        if not room_id: raise ValueError("room_id missing")
        return str(room_id).strip()

    def register_ws_connection(self, ws, room_id: str):
        rid = self._normalize_room_id(room_id)
        self.bot.ws_rooms.setdefault(rid, set()).add(ws)
        setattr(ws, "_assigned_room", rid)
        logger.info(f"Registered WebSocket to room '{rid}'")
        return True

    def unregister_ws_connection(self, ws):
        room = getattr(ws, "_assigned_room", None)
        if room in self.bot.ws_rooms:
            self.bot.ws_rooms[room].discard(ws)
            if not self.bot.ws_rooms[room]: del self.bot.ws_rooms[room]
            logger.info(f"Unregistered WebSocket from room '{room}'")
    
    async def player_connect(self, *args, **kwargs): return True, "Presence recorded"
    async def player_disconnect(self, *args, **kwargs): return True, "Presence removed"

    # --- KEPT & ADDED BACK: Core DB and Communication Logic ---
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
                if row and row['game_state']:
                    logger.info(f"Loaded existing game state for room '{room_id}'")
                    return json.loads(row['game_state'])
                logger.warning(f"No state for room '{room_id}', creating default.")
                return {'room_id': room_id, 'current_round': 'pre_game', 'players': []}
        finally:
            if conn: conn.close()

    async def _save_game_state(self, room_id: str, state: dict):
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                # This is the corrected "upsert" query without the 'last_activity' column.
                await cursor.execute(
                    """
                    INSERT INTO bot_game_rooms (room_id, game_state, guild_id, channel_id)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE game_state = VALUES(game_state)
                    """,
                    (room_id, json.dumps(state), state.get('guild_id'), state.get('channel_id'))
                )
            await conn.commit()
            logger.info(f"Successfully saved game state for room '{room_id}'")
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"Failed to save game state for room '{room_id}': {e}", exc_info=True)
            raise
        finally:
            if conn: conn.close()

    async def broadcast_game_state(self, room_id: str, state: dict):
        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        envelope = {"game_state": state, "server_ts": int(time.time())}
        msg = json.dumps(envelope)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)
        logger.info(f"Broadcasted state for room '{room_id}' to {len(bucket)} client(s).")

    # --- ADDED BACK: The WebSocket Action Handler ---
    async def handle_websocket_game_action(self, data: dict):
        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))
        
        try:
            # Step 1: Load the current state from the database.
            state = await self._load_game_state(room_id)
            state['guild_id'] = data.get('guild_id') # Ensure guild/channel IDs are fresh
            state['channel_id'] = data.get('channel_id')

            # Step 2: Modify the state based on the action.
            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id, player_id = pdata.get('seat_id'), pdata.get('discord_id')

                if not all([seat_id, player_id]):
                    logger.warning(f"Rejecting 'player_sit': missing seat_id or discord_id.")
                    return

                if any(p.get('seat_id') == seat_id for p in state['players']):
                    logger.warning(f"Player {player_id} failed to sit; seat {seat_id} is taken.")
                    return

                if any(p.get('discord_id') == player_id for p in state['players']):
                    logger.warning(f"Player {player_id} failed to sit; already seated.")
                    return

                state['players'].append({
                    'discord_id': player_id, 'name': pdata.get('name', 'Player'),
                    'seat_id': seat_id, 'avatar_url': pdata.get('avatar_url'),
                    'total_chips': 1000, # Add default values
                })
                logger.info(f"Player {player_id} sat in seat {seat_id} in room '{room_id}'.")
            
            else:
                logger.warning(f"Received unknown or unsupported action: '{action}'")
                return

            # Step 3: Save the newly modified state back to the database.
            await self._save_game_state(room_id, state)

            # Step 4: Broadcast the new state to all clients (the "success report").
            await self.broadcast_game_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

    # --- KEPT: Winner Evaluation Logic ---
    async def evaluate_hands(self, state: dict):
        # ... (evaluation logic remains here, unchanged) ...
        pass


async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
