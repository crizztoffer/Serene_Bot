# cogs/mechanics_main2.py
import os
import json
import time
import asyncio
from typing import Dict, List, Optional, Tuple
from discord.ext import commands
import aiomysql

# --- Use the project's card/deck models exactly as provided ---
# Note: ranks use "0" for 10; Card.to_output_format() returns e.g. "Ah", "0s".
from utils.game_models import Card, Deck

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# DB helpers (mirror your style in bot.py)
async def _db_connect_dict():
    return await aiomysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db="serene_users",
        charset='utf8mb4',
        autocommit=True,
        cursorclass=aiomysql.cursors.DictCursor
    )

def _now() -> int:
    return int(time.time())

def _hand_value(cards: List[str]) -> Tuple[int, bool]:
    """
    Compute blackjack value for a list of two-char card strings, return (total, is_soft).
    Aces start as 11 and we drop them to 1 while busting.
    """
    total = 0
    aces = 0
    for cs in cards:
        # cs like "Ah", "7d", "0s", "Qc"
        rank = cs[:-1]  # all but last char (since "0" is already single-char)
        if rank in ("J", "Q", "K"):
            v = 10
        elif rank == "A":
            v = 11
            aces += 1
        elif rank == "0":
            v = 10
        else:
            try:
                v = int(rank)
            except ValueError:
                v = 0
        total += v
    # Downgrade aces if we bust
    soft = False
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    if aces > 0:  # any remaining ace counted as 11
        soft = True
    return total, soft

def _is_blackjack(cards: List[str]) -> bool:
    if len(cards) != 2:
        return False
    total, _ = _hand_value(cards)
    return total == 21

def _normalize_room_id(room_id: str) -> str:
    return str(room_id or "").strip()

class MechanicsMain2(commands.Cog):
    """
    Blackjack mechanics (independent from Hold 'Em).
    Room state layout (JSON in bot_game_rooms.game_state):

    {
      "room_type": "blackjack",
      "deck": ["Ah","Kd",...],     # remaining undealt
      "dealer": {"hand": ["??", "7d"], "hole_revealed": false},
      "players": [
        {"id": "123", "name": "Alice", "hand": ["As","0d"], "bet": 25, "stood": false,
         "busted": false, "doubled": false, "surrendered": false, "acted": false}
      ],
      "status": "waiting|in_round|showdown|round_over",
      "turn_index": 0,
      "min_bet": 5,
      "_empty_since": 0
    }
    """
    def __init__(self, bot):
        self.bot = bot
        # independent WS registry (room_id -> set(web.WebSocketResponse))
        self._ws_rooms: Dict[str, set] = {}
        # action locks per room
        self._locks: Dict[str, asyncio.Lock] = {}

    # ------------- Websocket presence API expected by bot.py -------------

    def register_ws_connection(self, ws, room_id: str) -> bool:
        rid = _normalize_room_id(room_id)
        if not rid:
            return False
        if rid not in self._ws_rooms:
            self._ws_rooms[rid] = set()
        self._ws_rooms[rid].add(ws)
        setattr(ws, "_bj_room_id", rid)
        return True

    def unregister_ws_connection(self, ws):
        rid = getattr(ws, "_bj_room_id", None)
        if not rid:
            return
        try:
            bucket = self._ws_rooms.get(rid)
            if bucket and ws in bucket:
                bucket.discard(ws)
            if bucket and not bucket:
                self._ws_rooms.pop(rid, None)
        except Exception:
            pass

    async def player_connect(self, room_id: str, discord_id: str) -> Tuple[bool, Optional[str]]:
        # For parity with your poker cog; mark presence if you store it. No-op here.
        return True, None

    async def player_disconnect(self, room_id: str, discord_id: str):
        # Optional bookkeeping (not required for blackjack round flow)
        return

    # ---------------------- DB state helpers ----------------------

    async def _load_game_state(self, room_id: str) -> Optional[dict]:
        if not all([DB_USER, DB_PASSWORD, DB_HOST]):
            return None
        rid = _normalize_room_id(room_id)
        conn = None
        try:
            conn = await _db_connect_dict()
            async with conn.cursor() as cur:
                await cur.execute("SELECT game_state FROM bot_game_rooms WHERE room_id=%s", (rid,))
                row = await cur.fetchone()
                if not row:
                    return None
                raw = row.get("game_state")
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    jd = json.loads(raw) if raw else {}
                except Exception:
                    jd = {}
                return jd or None
        finally:
            if conn:
                conn.close()

    async def _save_game_state(self, room_id: str, state: dict):
        if not all([DB_USER, DB_PASSWORD, DB_HOST]):
            return
        rid = _normalize_room_id(room_id)
        conn = None
        try:
            conn = await _db_connect_dict()
            js = json.dumps(state)
            async with conn.cursor() as cur:
                # insert-or-update the row; set room_type to 'blackjack'
                await cur.execute(
                    """
                    INSERT INTO bot_game_rooms (room_id, room_name, room_type, game_mode, game_state)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE game_state = VALUES(game_state), room_type = VALUES(room_type)
                    """,
                    (rid, f"Blackjack {rid}", "blackjack", 1, js)
                )
        finally:
            if conn:
                conn.close()

    # ---------------------- Broadcasting ----------------------

    async def _broadcast(self, room_id: str, payload: dict):
        # Reuse bot-level broadcaster if you have one; otherwise send here.
        try:
            bucket = self._ws_rooms.get(room_id) or set()
            msg = json.dumps(payload)
            dead = []
            for ws in list(bucket):
                try:
                    if ws.closed:
                        dead.append(ws)
                        continue
                    await ws.send_str(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                try:
                    bucket.discard(ws)
                except Exception:
                    pass
        except Exception:
            pass

    async def _broadcast_state(self, room_id: str, state: dict):
        await self._broadcast(room_id, {"type": "state", "game_state": state, "room_id": room_id, "server_ts": _now()})

    # ---------------------- Game helpers ----------------------

    def _lock_for(self, room_id: str) -> asyncio.Lock:
        rid = _normalize_room_id(room_id)
        if rid not in self._locks:
            self._locks[rid] = asyncio.Lock()
        return self._locks[rid]

    def _new_state(self) -> dict:
        d = Deck()
        d.shuffle()
        return {
            "room_type": "blackjack",
            "deck": d.to_output_format(),  # list[str]
            "dealer": {"hand": [], "hole_revealed": False},
            "players": [],
            "status": "waiting",
            "turn_index": 0,
            "min_bet": 5
        }

    def _deal_from(self, state: dict) -> Optional[str]:
        # pop from the end (top) — consistent with Deck.deal_card().to_output_format()
        deck = state.get("deck") or []
        if not deck:
            return None
        return deck.pop()

    def _player_by_id(self, state: dict, player_id: str) -> Optional[dict]:
        for p in (state.get("players") or []):
            if str(p.get("id")) == str(player_id):
                return p
        return None

    def _everyone_acted_or_busted(self, state: dict) -> bool:
        for p in (state.get("players") or []):
            if not (p.get("busted") or p.get("surrendered") or p.get("stood") or p.get("acted")):
                return False
        return True

    def _advance_turn(self, state: dict):
        players = state.get("players") or []
        n = len(players)
        if n == 0:
            state["turn_index"] = 0
            return
        # move to next player that can act
        for _ in range(n):
            state["turn_index"] = (state.get("turn_index", 0) + 1) % n
            p = players[state["turn_index"]]
            if not (p.get("busted") or p.get("surrendered") or p.get("stood")):
                return

    # ---------------------- Round flow ----------------------

    async def _ensure_room(self, room_id: str) -> dict:
        state = await self._load_game_state(room_id)
        if not state:
            state = self._new_state()
            await self._save_game_state(room_id, state)
        return state

    async def _start_round_if_possible(self, room_id: str, state: dict):
        """
        Start a new deal if status is 'waiting' and at least one player has joined with a bet.
        """
        if state.get("status") != "waiting":
            return
        players = state.get("players") or []
        if not players:
            return

        # Fresh deck each round if low
        if len(state.get("deck") or []) < 15:
            d = Deck()
            d.shuffle()
            state["deck"] = d.to_output_format()

        # reset table markers
        state["dealer"] = {"hand": [], "hole_revealed": False}
        for p in players:
            p.update({
                "hand": [], "stood": False, "busted": False,
                "doubled": False, "surrendered": False, "acted": False
            })

        # initial deal: player, dealer (up), player, dealer (hole)
        for p in players:
            c = self._deal_from(state);  p["hand"].append(c)
        up = self._deal_from(state);     state["dealer"]["hand"].append(up)
        for p in players:
            c = self._deal_from(state);  p["hand"].append(c)
        hole = self._deal_from(state);   state["dealer"]["hand"].append(hole)
        state["dealer"]["hole_revealed"] = False

        # mark first player to act
        state["status"] = "in_round"
        state["turn_index"] = 0
        # pre-mark "acted" for those that have natural blackjack (they won't need actions)
        for p in players:
            if _is_blackjack(p["hand"]):
                p["acted"] = True

    def _finish_dealer_and_score(self, state: dict):
        dealer = state.get("dealer") or {}
        dealer_hand = dealer.get("hand") or []
        dealer["hole_revealed"] = True

        # Dealer draws to 17, hit soft 17? We'll stand on soft 17 (common variant); change if desired.
        while True:
            total, soft = _hand_value(dealer_hand)
            if total < 17:
                # hit
                c = self._deal_from(state)
                if not c:
                    break
                dealer_hand.append(c)
                continue
            # stand on soft 17:
            if total == 17 and soft:
                break
            if total >= 17:
                break

        dealer_total, _ = _hand_value(dealer_hand)
        for p in (state.get("players") or []):
            if p.get("surrendered"):
                p["result"] = "lose_half"
                continue
            if p.get("busted"):
                p["result"] = "lose"
                continue
            pt, _ = _hand_value(p["hand"])
            if _is_blackjack(p["hand"]):
                if _is_blackjack(dealer_hand):
                    p["result"] = "push"
                else:
                    p["result"] = "blackjack"  # 3:2 assumed client-side
                continue
            if dealer_total > 21:
                p["result"] = "win"
            else:
                if pt > dealer_total:
                    p["result"] = "win"
                elif pt < dealer_total:
                    p["result"] = "lose"
                else:
                    p["result"] = "push"

        state["status"] = "round_over"

    # ---------------------- Action entrypoints (WS) ----------------------

    async def handle_websocket_game_action(self, data: dict):
        """
        Dispatch actions coming from the WS:
          create_room, join, deal, hit, stand, double, surrender, reset_round
        Expected fields include: room_id, sender_id, maybe display_name, bet.
        """
        room_id = _normalize_room_id(data.get("room_id"))
        sender_id = str(data.get("sender_id"))
        action = str(data.get("action") or "").lower()
        display_name = str(data.get("display_name") or f"Player {sender_id}")

        if not room_id or not sender_id or not action:
            return

        async with self._lock_for(room_id):
            state = await self._ensure_room(room_id)

            # Ensure room type label
            state.setdefault("room_type", "blackjack")
            state.setdefault("min_bet", 5)
            players = state.setdefault("players", [])

            # utility: find or create player shell (no auto-join on create_room)
            def ensure_player():
                p = self._player_by_id(state, sender_id)
                if not p:
                    p = {
                        "id": sender_id, "name": display_name,
                        "hand": [], "bet": state["min_bet"],
                        "stood": False, "busted": False,
                        "doubled": False, "surrendered": False, "acted": False
                    }
                    players.append(p)
                return p

            # ---- Actions ----

            if action == "create_room":
                # Fresh state, keep same room id
                state = self._new_state()
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "join":
                p = ensure_player()
                # do not start round yet; client will call 'deal' after bets are placed
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "set_bet":
                amt = data.get("bet")
                try:
                    bet = max(state.get("min_bet", 5), int(amt))
                except Exception:
                    bet = state.get("min_bet", 5)
                p = ensure_player()
                p["bet"] = bet
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "deal":
                # start new round if waiting/round_over
                if state.get("status") in ("waiting", "round_over"):
                    await self._start_round_if_possible(room_id, state)
                    await self._save_game_state(room_id, state)
                    await self._broadcast_state(room_id, state)
                return

            if action == "hit":
                if state.get("status") != "in_round":
                    return
                p = self._player_by_id(state, sender_id)
                if not p or p.get("stood") or p.get("busted") or p.get("surrendered"):
                    return
                c = self._deal_from(state)
                if c:
                    p["hand"].append(c)
                total, _ = _hand_value(p["hand"])
                if total > 21:
                    p["busted"] = True
                    p["acted"] = True
                    # advance or finish
                    if self._everyone_acted_or_busted(state):
                        # reveal and score
                        self._finish_dealer_and_score(state)
                    else:
                        self._advance_turn(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "stand":
                if state.get("status") != "in_round":
                    return
                p = self._player_by_id(state, sender_id)
                if not p or p.get("busted") or p.get("surrendered") or p.get("stood"):
                    return
                p["stood"] = True
                p["acted"] = True
                if self._everyone_acted_or_busted(state):
                    self._finish_dealer_and_score(state)
                else:
                    self._advance_turn(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "double":
                if state.get("status") != "in_round":
                    return
                p = self._player_by_id(state, sender_id)
                # allow double only as first action (2 cards, not acted)
                if not p or p.get("acted") or len(p.get("hand") or []) != 2:
                    return
                p["bet"] = int(p.get("bet", state.get("min_bet", 5))) * 2
                p["doubled"] = True
                # exactly one card then auto-stand
                c = self._deal_from(state)
                if c:
                    p["hand"].append(c)
                total, _ = _hand_value(p["hand"])
                if total > 21:
                    p["busted"] = True
                p["stood"] = True
                p["acted"] = True
                if self._everyone_acted_or_busted(state):
                    self._finish_dealer_and_score(state)
                else:
                    self._advance_turn(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "surrender":
                if state.get("status") != "in_round":
                    return
                p = self._player_by_id(state, sender_id)
                # typically only before any other action (2 cards, not acted)
                if not p or p.get("acted") or len(p.get("hand") or []) != 2:
                    return
                p["surrendered"] = True
                p["acted"] = True
                p["stood"] = True
                if self._everyone_acted_or_busted(state):
                    self._finish_dealer_and_score(state)
                else:
                    self._advance_turn(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            if action == "reset_round":
                # keep players at table; set status to waiting so next 'deal' starts fresh
                state["status"] = "waiting"
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                return

            # Unknown action -> ignore (or log)
            return


async def setup(bot):
    await bot.add_cog(MechanicsMain2(bot))
