# cogs/mechanics_main2.py
import os
import json
import time
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from discord.ext import commands, tasks
import aiomysql

# --- Use the project's card/deck models exactly as provided ---
# Note: ranks use "0" for 10; Card.to_output_format() returns e.g. "Ah", "0s".
from utils.game_models import Card, Deck

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# ---------------- Presence / DC grace config ----------------
DISCONNECT_GRACE_SECS = 10  # after this, a DC'd seated player is removed if not reconnected

# ---------------- Minimums by game_mode (parity with TH) ----------------
MODE_MIN_BET = {
    "1": 5,
    "2": 10,
    "3": 25,
    "4": 100,
}

# ===================== WS DEBUG LOGGING =====================
def _init_logger() -> logging.Logger:
    """
    blackjack_ws logger with env-driven configuration.
    Env:
      BJ_WS_LOG_LEVEL    -> logging level (DEBUG/INFO/WARNING/ERROR)
      BJ_WS_LOG_JSON     -> "1" to emit JSON lines, else human-readable
      BJ_WS_LOG_PAYLOADS -> "1" to include compact payload bodies (inbound)
    """
    name = "blackjack_ws"
    logger = logging.getLogger(name)

    # Only configure handlers once
    if getattr(logger, "_bj_configured", False):
        return logger

    level_name = (os.getenv("BJ_WS_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    json_mode = os.getenv("BJ_WS_LOG_JSON") == "1"

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            base = {
                "ts": int(time.time()),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            # Merge extra dict if present
            if hasattr(record, "extra"):
                try:
                    base.update(record.extra)  # type: ignore[attr-defined]
                except Exception:
                    pass
            return json.dumps(base, separators=(",", ":"), ensure_ascii=False)

    class _TextFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            prefix = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {record.levelname:<7} {record.name}: "
            msg = record.getMessage()
            if hasattr(record, "extra"):
                try:
                    x = record.extra  # type: ignore[attr-defined]
                    if x:
                        msg += " | " + ", ".join(f"{k}={x[k]}" for k in sorted(x))
                except Exception:
                    pass
            return prefix + msg

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(_JsonFormatter() if json_mode else _TextFormatter())
    logger.addHandler(handler)
    logger._bj_configured = True  # type: ignore[attr-defined]

    return logger

LOG = _init_logger()
LOG_INCLUDE_PAYLOADS = os.getenv("BJ_WS_LOG_PAYLOADS") == "1"

def _log(event: str, level: int = logging.INFO, **fields):
    """
    Emit a normalized log line. Keeps messages short; puts details in the 'extra' bag.
    """
    try:
        msg = event
        rec = logging.LogRecord(LOG.name, level, __file__, 0, msg, args=None, exc_info=None)
        setattr(rec, "extra", fields)
        LOG.handle(rec)
    except Exception:
        # never let logging crash the game loop
        pass

def _payload_size_snippet(obj) -> Tuple[int, Optional[str]]:
    try:
        s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        return len(s), (s if LOG_INCLUDE_PAYLOADS else None)
    except Exception:
        return 0, None
# ===========================================================


# ====== DB helpers (mirror your style in bot.py) ======
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

# ====== Blackjack helpers ======
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
    Blackjack mechanics with **seating identical to Texas Hold 'Em**.

    Room state layout (JSON in bot_game_rooms.game_state):

    {
      "room_type": "blackjack",
      "deck": ["Ah","Kd",...],     # remaining undealt
      "dealer": {"hand": ["??", "7d"], "hole_revealed": false},
      "players": [
        {"discord_id":"123","name":"Alice","seat_id":"seat_3","avatar_url":"...",
         "hand":["As","0d"],"bet":25,"stood":false,"busted":false,"doubled":false,
         "surrendered":false,"acted":false,"in_hand":true,"is_spectating":false,
         "connected":true,"total_contributed":0}
      ],
      "status": "pre-game|in_round|showdown|round_over",
      "turn_index": 0,
      "min_bet": 5,
      "guild_id": "...", "channel_id": "...",
      "pre_flop_timer_start_time": 0,
      "initial_countdown_triggered": false,
      "round_timer_start": null, "round_timer_secs": null,
      "action_deadline_epoch": null,
      "pending_disconnects": {"123": 1711111111},
      "__rev": 7
    }
    """
    # ------ Phase timers (parity with TH semantics) ------
    PRE_GAME_WAIT_SECS = 60         # delay after first sitter
    POST_ROUND_WAIT_SECS = 15       # visible results window
    ACTION_SECS = 60                # per-player action (optional hint)

    def __init__(self, bot):
        self.bot = bot

        # independent WS registry (room_id -> set(web.WebSocketResponse))
        self._ws_rooms: Dict[str, set] = {}
        # action locks per room
        self._locks: Dict[str, asyncio.Lock] = {}
        # rooms we poll for timers
        self.rooms_with_active_timers = set()

        # kick off timer loop
        self.check_game_timers.start()
        _log("mechanics_init", room_id="*", note="timer_loop_started")

    def cog_unload(self):
        self.check_game_timers.cancel()
        _log("mechanics_unload", room_id="*")

    # ------------- Websocket presence API expected by bot.py -------------

    def register_ws_connection(self, ws, room_id: str) -> bool:
        """
        Called by bot.py when a /game_bj websocket connects.
        bot.py should also tag ws._player_id = <sender_id> on handshake (same as TH).
        """
        rid = _normalize_room_id(room_id)
        if not rid:
            return False
        if rid not in self._ws_rooms:
            self._ws_rooms[rid] = set()
        self._ws_rooms[rid].add(ws)
        setattr(ws, "_bj_room_id", rid)
        _log("ws_register", room_id=rid, player_id=getattr(ws, "_player_id", None), count=len(self._ws_rooms[rid]))
        return True

    def unregister_ws_connection(self, ws):
        rid = getattr(ws, "_bj_room_id", None)
        pid = getattr(ws, "_player_id", None)
        if not rid:
            return
        try:
            bucket = self._ws_rooms.get(rid)
            if bucket and ws in bucket:
                bucket.discard(ws)
            if bucket and not bucket:
                self._ws_rooms.pop(rid, None)
            _log("ws_unregister", room_id=rid, player_id=pid, remaining=len(self._ws_rooms.get(rid, set())))
        except Exception as e:
            _log("ws_unregister_error", logging.ERROR, room_id=rid, player_id=pid, error=str(e))

    # ---- presence tagging helpers (parity with TH) ----
    def _is_ws_connected(self, room_id: str, player_id: str) -> bool:
        """
        True if a live WS in the room is tagged with _player_id == player_id.
        """
        try:
            for ws in list(self._ws_rooms.get(str(room_id), set())):
                if getattr(ws, "closed", False):
                    continue
                if str(getattr(ws, "_player_id", "")) == str(player_id):
                    return True
        except Exception:
            pass
        return False

    async def player_connect(self, room_id: str, discord_id: str) -> Tuple[bool, Optional[str]]:
        """
        Mark a player connected and clear any disconnect stamp (parity with TH).
        """
        rid = _normalize_room_id(room_id)
        try:
            state = await self._load_game_state(rid) or self._new_state()
            self._ensure_defaults(state)
            p = self._find_player(state, str(discord_id))
            if not p:
                _log("presence_connect_noop", room_id=rid, player_id=discord_id)
                return True, None
            changed = False
            if p.get("connected") is not True:
                p["connected"] = True
                changed = True
            if p.pop("_dc_since", None) is not None:
                changed = True
            pend = state.setdefault("pending_disconnects", {})
            if pend.pop(str(discord_id), None) is not None:
                changed = True
            if changed:
                self._mark_dirty(state)
                await self._save_game_state(rid, state)
                _log("presence_connect", room_id=rid, player_id=discord_id)
        except Exception as e:
            _log("presence_connect_error", logging.ERROR, room_id=rid, player_id=discord_id, error=str(e))
            return False, str(e)
        return True, None

    async def player_disconnect(self, room_id: str, discord_id: str):
        """
        Mark a player disconnected and stamp when that happened (parity with TH).
        """
        rid = _normalize_room_id(room_id)
        try:
            state = await self._load_game_state(rid) or self._new_state()
            self._ensure_defaults(state)
            p = self._find_player(state, str(discord_id))
            if not p:
                _log("presence_disconnect_noop", room_id=rid, player_id=discord_id)
                return
            changed = False
            if p.get("connected") is not False:
                p["connected"] = False
                changed = True
            if not p.get("_dc_since"):
                p["_dc_since"] = _now()
                changed = True
            pend = state.setdefault("pending_disconnects", {})
            deadline = _now() + DISCONNECT_GRACE_SECS
            if pend.get(str(discord_id)) != deadline:
                pend[str(discord_id)] = deadline
                changed = True
            if changed:
                self._mark_dirty(state)
                await self._save_game_state(rid, state)
                _log("presence_disconnect", room_id=rid, player_id=discord_id, grace_secs=DISCONNECT_GRACE_SECS)
        except Exception as e:
            _log("presence_disconnect_error", logging.ERROR, room_id=rid, player_id=discord_id, error=str(e))

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

    async def _load_room_config(self, room_id: str) -> dict:
        """
        Fetch game_mode for the room and derive min_bet (+ guild/channel), parity with TH.
        """
        if not all([DB_USER, DB_PASSWORD, DB_HOST]):
            return {"game_mode": "1", "min_bet": MODE_MIN_BET["1"], "guild_id": None, "channel_id": None}
        conn = None
        try:
            conn = await _db_connect_dict()
            async with conn.cursor() as cur:
                await cur.execute("SELECT game_mode, guild_id, channel_id FROM bot_game_rooms WHERE room_id=%s LIMIT 1", (room_id,))
                row = await cur.fetchone()
                game_mode = str((row or {}).get("game_mode") or "1")
                min_bet = MODE_MIN_BET.get(game_mode, MODE_MIN_BET["1"])
                return {
                    "game_mode": game_mode,
                    "min_bet": int(min_bet),
                    "guild_id": (row or {}).get("guild_id"),
                    "channel_id": (row or {}).get("channel_id"),
                }
        except Exception:
            return {"game_mode": "1", "min_bet": MODE_MIN_BET["1"], "guild_id": None, "channel_id": None}

    # ---------------------- Broadcasting ----------------------

    async def _broadcast(self, room_id: str, payload: dict):
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
                except Exception as e:
                    dead.append(ws)
                    _log("ws_send_error", logging.ERROR, room_id=room_id, error=str(e))
            for ws in dead:
                try:
                    bucket.discard(ws)
                except Exception:
                    pass
            _log(
                "broadcast",
                room_id=room_id,
                kind=str(payload.get("type") or payload.get("action") or "unknown"),
                recipients=len(bucket),
                bytes=len(msg),
            )
        except Exception as e:
            _log("broadcast_error", logging.ERROR, room_id=room_id, error=str(e))

    async def _broadcast_state(self, room_id: str, state: dict):
        await self._broadcast(
            room_id,
            {"type": "state", "game_state": state, "room_id": room_id, "server_ts": _now()}
        )

    def _build_ui_hint_for_current_actor(self, state: dict) -> dict:
        """
        Hints for client action bar (mirrors TH idea, adapted for BJ).
        """
        players = state.get("players") or []
        idx = int(state.get("turn_index") or 0)
        actor = None
        if players and 0 <= idx < len(players):
            actor = players[idx]
        can_hit = can_stand = can_double = can_surrender = False
        if state.get("status") == "in_round" and actor:
            hand = actor.get("hand") or []
            total, _ = _hand_value(hand)
            busted = total > 21
            acted = bool(actor.get("acted"))
            stood = bool(actor.get("stood"))
            can_hit = not (busted or stood)
            can_stand = not (busted or stood)
            # Typical BJ: double only on first action with 2 cards
            can_double = (len(hand) == 2) and not acted and not stood and not busted
            can_surrender = (len(hand) == 2) and not acted and not stood and not busted
        return {
            "actor": str(actor.get("discord_id")) if actor else None,
            "can_hit": bool(can_hit),
            "can_stand": bool(can_stand),
            "can_double": bool(can_double),
            "can_split": False,         # implement split later if needed
            "can_surrender": bool(can_surrender),
            "can_insure": False,        # implement insurance if needed
            "min_bet": int(state.get("min_bet") or MODE_MIN_BET["1"]),
            "max_bet": 0,
            "insurance_amount": 0,
            "double_amount": int((actor or {}).get("bet") or (state.get("min_bet") or 0)),
        }

    async def _broadcast_tick(self, room_id: str, state: dict):
        pkt = {
            "type": "tick",
            "room_id": room_id,
            "server_ts": _now(),
            "current_round": state.get("status"),
            "action_deadline_epoch": state.get("action_deadline_epoch"),
            "round_timer_start": state.get("round_timer_start"),
            "round_timer_secs": state.get("round_timer_secs"),
            "current_actor": self._actor_id(state),
            "ui_for_current_actor": self._build_ui_hint_for_current_actor(state),
            "__rev": int(state.get("__rev") or 0),
        }
        await self._broadcast(room_id, pkt)

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
            "status": "pre-game",           # parity: start in pre-game
            "turn_index": 0,
            "min_bet": MODE_MIN_BET["1"],
            "guild_id": None, "channel_id": None,
            "pre_flop_timer_start_time": None,
            "initial_countdown_triggered": False,
            "round_timer_start": None, "round_timer_secs": None,
            "action_deadline_epoch": None,
            "pending_disconnects": {},
            "__rev": 0
        }

    def _deal_from(self, state: dict) -> Optional[str]:
        # pop from the end (top) — consistent with Deck.deal_card().to_output_format()
        deck = state.get("deck") or []
        if not deck:
            return None
        return deck.pop()

    def _player_by_id(self, state: dict, player_id: str) -> Optional[dict]:
        for p in (state.get("players") or []):
            if str(p.get("discord_id")) == str(player_id) or str(p.get("id")) == str(player_id):
                return p
        return None

    def _find_player(self, state: dict, discord_id: str) -> Optional[dict]:
        return self._player_by_id(state, discord_id)

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

    def _actor_id(self, state: dict) -> Optional[str]:
        players = state.get("players") or []
        idx = int(state.get("turn_index") or 0)
        if players and 0 <= idx < len(players):
            return str(players[idx].get("discord_id"))
        return None

    # --------- Revision / defaults / empty-seat ------
    def _mark_dirty(self, state: dict):
        state["__rev"] = int(state.get("__rev") or 0) + 1

    def _ensure_defaults(self, state: dict) -> dict:
        state.setdefault("room_type", "blackjack")
        state.setdefault("deck", [])
        state.setdefault("dealer", {"hand": [], "hole_revealed": False})
        state.setdefault("players", [])
        state.setdefault("status", "pre-game")
        state.setdefault("turn_index", 0)
        state.setdefault("min_bet", MODE_MIN_BET["1"])
        state.setdefault("guild_id", None)
        state.setdefault("channel_id", None)
        state.setdefault("pre_flop_timer_start_time", None)   # keep same key as TH for countdown
        state.setdefault("initial_countdown_triggered", False)
        state.setdefault("round_timer_start", None)
        state.setdefault("round_timer_secs", None)
        state.setdefault("action_deadline_epoch", None)
        state.setdefault("pending_disconnects", {})
        state.setdefault("__rev", 0)
        return state

    def _force_pre_game_if_empty_seats(self, state: dict) -> bool:
        """
        If no players are seated, eventually force a clean 'pre-game' state.
        Debounced (2s) like TH.
        """
        seated = [p for p in state.get("players", []) if p.get("seat_id")]
        now = _now()
        if seated:
            state.pop("_empty_since", None)
            return False

        t0 = state.get("_empty_since")
        if not t0:
            state["_empty_since"] = now
            return False
        if (now - int(t0)) < 2:
            return False

        # reset to lobby/pre-game
        state["status"] = "pre-game"
        state["dealer"] = {"hand": [], "hole_revealed": False}
        state["deck"] = state.get("deck") or []
        state["turn_index"] = 0
        # timers
        state["round_timer_start"] = None
        state["round_timer_secs"] = None
        state["action_deadline_epoch"] = None
        state["pre_flop_timer_start_time"] = None
        state["initial_countdown_triggered"] = False
        # clear debounce
        state.pop("_empty_since", None)
        self._mark_dirty(state)
        _log("force_pre_game", room_id=state.get("room_id"))
        return True

    # --------- DC reap (parity with TH) ---------
    def _remove_player_by_id(self, state: dict, player_id: str) -> str:
        """
        Remove player; returns "advance_phase" iff we should advance a betting/action phase.
        For BJ, if actor folds/removed and others remain, advance pointer; otherwise, finish.
        """
        pid = str(player_id)
        p = self._find_player(state, pid)
        if not p:
            return "none"

        in_round = state.get("status") == "in_round"
        was_current = (self._actor_id(state) == pid)

        # In BJ, if removed mid-round, mark them acted/stood to skip
        p["stood"] = True
        p["acted"] = True

        # Remove from table entirely (parity with TH)
        state["players"] = [q for q in state.get("players", []) if str(q.get("discord_id")) != pid]
        self._mark_dirty(state)
        _log("player_removed", room_id=state.get("room_id"), player_id=pid, in_round=in_round, was_current=was_current)

        # Force pre-game if no one is seated
        self._force_pre_game_if_empty_seats(state)

        # Advance pointer if needed
        if in_round and was_current and state.get("status") == "in_round":
            if self._everyone_acted_or_busted(state):
                return "advance_phase"  # here, "advance" means finish dealer & score
            else:
                # adjust turn_index within new array size
                state["turn_index"] = min(state.get("turn_index", 0), max(0, len(state.get("players", [])) - 1))
                self._advance_turn(state)

        return "none"

    def _reap_players_with_dead_ws(self, state: dict, room_id: str) -> Tuple[bool, bool]:
        now = _now()
        changed = False
        need_advance = False

        for p in list(state.get("players", [])):
            pid = str(p.get("discord_id") or "")
            if not pid:
                continue

            t0 = p.get("_dc_since")

            # start grace if we see no live WS and no stamp
            if not t0 and not self._is_ws_connected(room_id, pid):
                p["_dc_since"] = now
                self._mark_dirty(state)
                changed = True
                t0 = now
                _log("reap_mark_dc", room_id=room_id, player_id=pid)

            if not t0:
                continue

            # reconnected?
            if self._is_ws_connected(room_id, pid):
                try:
                    p.pop("_dc_since", None)
                except Exception:
                    pass
                (state.setdefault("pending_disconnects", {})).pop(pid, None)
                self._mark_dirty(state)
                changed = True
                _log("reap_reconnect", room_id=room_id, player_id=pid)
                continue

            # if grace not elapsed, skip
            if (now - int(t0)) < DISCONNECT_GRACE_SECS:
                continue

            # otherwise, remove
            action = self._remove_player_by_id(state, pid)
            (state.setdefault("pending_disconnects", {})).pop(pid, None)
            changed = True
            if action == "advance_phase":
                need_advance = True
            _log("reap_remove", room_id=room_id, player_id=pid, advance=need_advance)

        return changed, need_advance

    def _reap_pending_disconnects(self, state: dict, room_id: str) -> Tuple[bool, bool]:
        pend = dict(state.get("pending_disconnects") or {})
        if not pend:
            return False, False

        now = _now()
        changed = False
        need_advance = False

        for pid, deadline in list(pend.items()):
            if now < int(deadline):
                continue
            if self._is_ws_connected(room_id, pid):
                state["pending_disconnects"].pop(pid, None)
                self._mark_dirty(state)
                changed = True
                _log("reap_pend_cleared_live", room_id=room_id, player_id=pid)
                continue

            action = self._remove_player_by_id(state, pid)
            state["pending_disconnects"].pop(pid, None)
            self._mark_dirty(state)
            changed = True
            if action == "advance_phase":
                need_advance = True
            _log("reap_pend_removed", room_id=room_id, player_id=pid, advance=need_advance)

        return changed, need_advance

    # ---------------------- Round flow ----------------------

    async def _ensure_room(self, room_id: str) -> dict:
        state = await self._load_game_state(room_id)
        if not state:
            state = self._new_state()
            await self._save_game_state(room_id, state)
            _log("room_bootstrap", room_id=room_id)
        return state

    async def _start_round_if_possible(self, room_id: str, state: dict):
        """
        Start a new deal if status is 'pre-game' or 'round_over' and at least one eligible, non-spectator player is seated.
        """
        if state.get("status") not in ("pre-game", "round_over"):
            return
        players = [p for p in (state.get("players") or []) if not p.get("is_spectating")]
        if not players:
            return

        # Fresh deck each round if low
        if len(state.get("deck") or []) < 15:
            d = Deck()
            d.shuffle()
            state["deck"] = d.to_output_format()

        # reset table markers
        state["dealer"] = {"hand": [], "hole_revealed": False}
        for p in state.get("players") or []:
            p.update({
                "hand": [], "stood": False, "busted": False,
                "doubled": False, "surrendered": False, "acted": False
            })
            # put only seated, non-spectators into the round
            if p.get("seat_id") and not p.get("is_spectating"):
                p["in_hand"] = True
            else:
                p["in_hand"] = False

        # initial deal: player, dealer (up), player, dealer (hole)
        for p in players:
            c = self._deal_from(state);  p["hand"].append(c)
        up = self._deal_from(state);     state["dealer"]["hand"].append(up)
        for p in players:
            c = self._deal_from(state);  p["hand"].append(c)
        hole = self._deal_from(state);   state["dealer"]["hand"].append(hole)
        state["dealer"]["hole_revealed"] = False

        # mark first actionable player (lowest seat number first)
        order = sorted([q for q in players], key=lambda q: self._seat_num(q))
        if order:
            first_id = str(order[0].get("discord_id"))
            idx = next((i for i, r in enumerate(state["players"]) if str(r.get("discord_id")) == first_id), 0)
            state["turn_index"] = idx
        else:
            state["turn_index"] = 0

        # auto-mark actors with natural blackjack
        for p in players:
            if _is_blackjack(p["hand"]):
                p["acted"] = True

        state["status"] = "in_round"
        # optional: set an action deadline like TH (client uses it for countdown)
        state["action_deadline_epoch"] = _now() + self.ACTION_SECS
        self._mark_dirty(state)
        _log("round_start", room_id=room_id, players=len(players))

    def _finish_dealer_and_score(self, state: dict):
        dealer = state.get("dealer") or {}
        dealer_hand = dealer.get("hand") or []
        dealer["hole_revealed"] = True

        # Dealer draws to 17; stand on soft 17 (adjust if needed)
        while True:
            total, soft = _hand_value(dealer_hand)
            if total < 17:
                c = self._deal_from(state)
                if not c:
                    break
                dealer_hand.append(c)
                continue
            if total == 17 and soft:
                break
            if total >= 17:
                break

        dealer_total, _ = _hand_value(dealer_hand)
        for p in (state.get("players") or []):
            if not p.get("in_hand"):
                p["result"] = None
                continue
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

        # start visible timer before moving to post round
        state["status"] = "showdown"
        state["round_timer_start"] = _now()
        state["round_timer_secs"] = self.POST_ROUND_WAIT_SECS
        self._mark_dirty(state)
        _log("round_showdown", room_id=state.get("room_id"), dealer_total=dealer_total)

    # ---------------------- Timer loop ----------------------
    def _add_room_active(self, room_id: str):
        self.rooms_with_active_timers.add(_normalize_room_id(room_id))

    @tasks.loop(seconds=1.0)
    async def check_game_timers(self):
        if not self.rooms_with_active_timers:
            return
        for room_id in list(self.rooms_with_active_timers):
            rid = _normalize_room_id(room_id)
            try:
                state = await self._load_game_state(rid)
                if not state:
                    continue
                self._ensure_defaults(state)

                # ensure min_bet present from DB once (parity with TH)
                if not state.get("min_bet"):
                    cfg = await self._load_room_config(rid)
                    state["min_bet"] = int(cfg["min_bet"])
                    state.setdefault("guild_id", cfg.get("guild_id"))
                    state.setdefault("channel_id", cfg.get("channel_id"))
                    self._mark_dirty(state)

                before_rev = int(state.get("__rev") or 0)

                # force pre-game if empty seating (debounced)
                if self._force_pre_game_if_empty_seats(state):
                    await self._save_game_state(rid, state)
                    await self._broadcast_state(rid, state)
                    self._add_room_active(rid)
                    continue

                # reap DC'd players
                changed_dc, need_advance_dc = self._reap_players_with_dead_ws(state, rid)
                changed_pd, need_advance_pd = self._reap_pending_disconnects(state, rid)
                need_advance = need_advance_dc or need_advance_pd

                # pre-game countdown: after first sitter
                if state.get("status") == "pre-game":
                    t0 = state.get("pre_flop_timer_start_time")
                    if t0 and _now() >= int(t0) + self.PRE_GAME_WAIT_SECS:
                        await self._start_round_if_possible(rid, state)

                # in-round action timeout -> advance turn or finish round
                elif state.get("status") == "in_round":
                    deadline = state.get("action_deadline_epoch")
                    if isinstance(deadline, int) and _now() >= deadline:
                        # auto-stand the actor (fold-equivalent in BJ)
                        actor_id = self._actor_id(state)
                        p = self._find_player(state, actor_id) if actor_id else None
                        if p and not (p.get("stood") or p.get("busted") or p.get("surrendered")):
                            p["stood"] = True
                            p["acted"] = True
                            self._mark_dirty(state)
                            _log("auto_stand_timeout", room_id=rid, player_id=actor_id)
                        if self._everyone_acted_or_busted(state):
                            self._finish_dealer_and_score(state)
                        else:
                            self._advance_turn(state)
                            state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                            self._mark_dirty(state)

                # showdown -> after visible window, move to round_over
                elif state.get("status") == "showdown":
                    ts = state.get("round_timer_start")
                    dur = state.get("round_timer_secs")
                    if ts and dur and _now() >= int(ts) + int(dur):
                        state["status"] = "round_over"
                        # reset timers
                        state["round_timer_start"] = None
                        state["round_timer_secs"] = None
                        state["action_deadline_epoch"] = None
                        self._mark_dirty(state)
                        _log("status_round_over", room_id=rid)

                # if a reaper said advance phase mid-round, finish dealer now
                if need_advance and state.get("status") == "in_round":
                    self._finish_dealer_and_score(state)

                after_rev = int(state.get("__rev") or 0)
                changed = (after_rev != before_rev)

                if changed:
                    await self._save_game_state(rid, state)
                    await self._broadcast_state(rid, state)
                else:
                    # push tick if timers are active
                    if self._has_timers(state):
                        await self._broadcast_tick(rid, state)

                self._add_room_active(rid)

            except Exception as e:
                _log("timer_loop_error", logging.ERROR, room_id=rid, error=str(e))
                # stop polling this room if it keeps erroring
                try:
                    self.rooms_with_active_timers.discard(rid)
                except Exception:
                    pass

    @check_game_timers.before_loop
    async def before_check_game_timers(self):
        await self.bot.wait_until_ready()

    def _has_timers(self, state: dict) -> bool:
        if state.get("status") == "in_round" and isinstance(state.get("action_deadline_epoch"), int):
            return True
        if state.get("status") == "showdown" and state.get("round_timer_start") and state.get("round_timer_secs"):
            return True
        if state.get("status") == "pre-game" and state.get("pre_flop_timer_start_time"):
            return True
        return False

    # ---------------------- Action entrypoints (WS) ----------------------

    def _seat_num(self, p: dict) -> int:
        try:
            sid = str(p.get("seat_id") or "")
            return int(sid.split("_")[-1])
        except Exception:
            return 9999

    async def handle_websocket_game_action(self, data: dict):
        """
        Dispatch actions coming from the WS (and **seating parity with TH**):
          player_sit, player_leave, create_room, join, set_bet, deal, hit, stand, double, surrender, reset_round
        Expected fields include: room_id, sender_id, maybe display_name, bet, player_data{seat_id,...}.
        """
        room_id = _normalize_room_id(data.get("room_id"))
        sender_id = str(data.get("sender_id"))
        action = str(data.get("action") or "").lower()
        display_name = str(data.get("display_name") or data.get("joiner_display_name") or f"Player {sender_id}")

        if not room_id or not sender_id or not action:
            return

        # Log inbound (compact)
        size, snippet = _payload_size_snippet(data)
        _log(
            "ws_inbound",
            room_id=room_id,
            player_id=sender_id,
            action=action,
            bytes=size,
            payload=(snippet if LOG_INCLUDE_PAYLOADS else None),
        )

        async with self._lock_for(room_id):
            state = await self._ensure_room(room_id)
            self._ensure_defaults(state)

            # ensure min_bet present (from DB) once
            if not state.get("min_bet"):
                cfg = await self._load_room_config(room_id)
                state["min_bet"] = int(cfg["min_bet"])
                state.setdefault("guild_id", cfg.get("guild_id"))
                state.setdefault("channel_id", cfg.get("channel_id"))
                self._mark_dirty(state)

            # If pre-game countdown already elapsed, start immediately (parity with TH)
            t0 = state.get("pre_flop_timer_start_time")
            if state.get("status") == "pre-game" and t0 and _now() >= int(t0) + self.PRE_GAME_WAIT_SECS:
                await self._start_round_if_possible(room_id, state)
                self._mark_dirty(state)

            players = state.setdefault("players", [])

            # ---- Seating parity actions ----
            if action == "player_sit":
                pdata = data.get("player_data", {}) or {}
                seat_id = pdata.get("seat_id")
                player_id = str(pdata.get("discord_id") or data.get("sender_id"))
                if seat_id and player_id:
                    # already at table? noop
                    if any(str(p.get("discord_id")) == player_id for p in players):
                        pass
                    # seat taken? noop
                    elif any(str(p.get("seat_id")) == str(seat_id) for p in players):
                        pass
                    else:
                        is_mid_hand = state.get("status") not in ("pre-game", "round_over")
                        players.append({
                            "discord_id": player_id,
                            "name": pdata.get("name") or display_name or "Player",
                            "seat_id": seat_id,
                            "avatar_url": pdata.get("avatar_url"),
                            # PARITY WITH TH SEATING: set bet=0 on sit; total_chips + total_contributed fields
                            "total_chips": 1000,
                            "hand": [],
                            "bet": 0,
                            "stood": False, "busted": False,
                            "doubled": False, "surrendered": False,
                            "acted": False,
                            "is_spectating": bool(is_mid_hand),
                            "in_hand": not bool(is_mid_hand),
                            "total_contributed": 0,
                            "connected": True,
                        })
                        try:
                            players[-1].pop("_dc_since", None)
                        except Exception:
                            pass
                        self._mark_dirty(state)

                        # First eligible sitter: start pre-game countdown (identical trigger to TH)
                        if len([p for p in players if not p.get("is_spectating")]) == 1 \
                            and state.get("status") == "pre-game" and not state.get("initial_countdown_triggered"):
                            state["pre_flop_timer_start_time"] = _now()
                            state["initial_countdown_triggered"] = True
                            self._mark_dirty(state)
                            self._add_room_active(room_id)
                else:
                    # missing fields -> ignore (same as TH's noop behavior)
                    pass

                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("sit_attempt", room_id=room_id, player_id=player_id or sender_id, seat_id=seat_id)
                return

            if action == "player_leave":
                outcome = self._remove_player_by_id(state, sender_id)
                if outcome == "advance_phase" and state.get("status") == "in_round":
                    self._finish_dealer_and_score(state)
                self._add_room_active(room_id)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("leave", room_id=room_id, player_id=sender_id, outcome=outcome)
                return

            # ---- Game setup / presence-level actions ----
            if action == "create_room":
                state = self._new_state()
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("create_room", room_id=room_id)
                return

            if action == "join":
                # ensure a player shell exists but do not auto-seat (seating is via player_sit)
                if not self._find_player(state, sender_id):
                    players.append({
                        "discord_id": sender_id, "name": display_name,
                        "hand": [], "bet": 0,
                        "stood": False, "busted": False,
                        "doubled": False, "surrendered": False, "acted": False,
                        "is_spectating": True, "in_hand": False,
                        "connected": True,
                        "total_contributed": 0,
                        "total_chips": 1000,
                    })
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("join", room_id=room_id, player_id=sender_id)
                return

            if action == "set_bet":
                amt = data.get("bet")
                try:
                    bet = max(state.get("min_bet", MODE_MIN_BET["1"]), int(amt))
                except Exception:
                    bet = state.get("min_bet", MODE_MIN_BET["1"])
                p = self._find_player(state, sender_id)
                if p:
                    p["bet"] = bet
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("set_bet", room_id=room_id, player_id=sender_id, bet=bet)
                return

            if action == "deal":
                if state.get("status") in ("pre-game", "round_over"):
                    await self._start_round_if_possible(room_id, state)
                    await self._save_game_state(room_id, state)
                    await self._broadcast_state(room_id, state)
                    _log("deal", room_id=room_id, player_id=sender_id)
                return

            # ---- Per-move actions ----
            if action == "hit":
                if state.get("status") != "in_round":
                    return
                p = self._find_player(state, sender_id)
                if not p or p.get("stood") or p.get("busted") or p.get("surrendered"):
                    return
                c = self._deal_from(state)
                if c:
                    p["hand"].append(c)
                total, _ = _hand_value(p["hand"])
                if total > 21:
                    p["busted"] = True
                    p["acted"] = True
                    if self._everyone_acted_or_busted(state):
                        self._finish_dealer_and_score(state)
                    else:
                        self._advance_turn(state)
                        state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                        self._mark_dirty(state)
                else:
                    # reset actor timer on successful action
                    state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("move_hit", room_id=room_id, player_id=sender_id, total=total)
                return

            if action == "stand":
                if state.get("status") != "in_round":
                    return
                p = self._find_player(state, sender_id)
                if not p or p.get("busted") or p.get("surrendered") or p.get("stood"):
                    return
                p["stood"] = True
                p["acted"] = True
                if self._everyone_acted_or_busted(state):
                    self._finish_dealer_and_score(state)
                else:
                    self._advance_turn(state)
                    state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("move_stand", room_id=room_id, player_id=sender_id)
                return

            if action == "double":
                if state.get("status") != "in_round":
                    return
                p = self._find_player(state, sender_id)
                if not p or p.get("acted") or len(p.get("hand") or []) != 2:
                    return
                p["bet"] = int(p.get("bet", state.get("min_bet", MODE_MIN_BET["1"]))) * 2
                p["doubled"] = True
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
                    state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("move_double", room_id=room_id, player_id=sender_id, total=total, bet=p.get("bet"))
                return

            if action == "surrender":
                if state.get("status") != "in_round":
                    return
                p = self._find_player(state, sender_id)
                if not p or p.get("acted") or len(p.get("hand") or []) != 2:
                    return
                p["surrendered"] = True
                p["acted"] = True
                p["stood"] = True
                if self._everyone_acted_or_busted(state):
                    self._finish_dealer_and_score(state)
                else:
                    self._advance_turn(state)
                    state["action_deadline_epoch"] = _now() + self.ACTION_SECS
                    self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("move_surrender", room_id=room_id, player_id=sender_id)
                return

            if action == "reset_round":
                # keep players seated; return to pre-game lobby state
                state["status"] = "pre-game"
                state["dealer"] = {"hand": [], "hole_revealed": False}
                state["turn_index"] = 0
                state["round_timer_start"] = None
                state["round_timer_secs"] = None
                state["action_deadline_epoch"] = None
                self._mark_dirty(state)
                await self._save_game_state(room_id, state)
                await self._broadcast_state(room_id, state)
                _log("reset_round", room_id=room_id, player_id=sender_id)
                return

            # Unknown action -> ignore
            _log("ws_inbound_unknown", room_id=room_id, player_id=sender_id, action=action)
            return


async def setup(bot):
    await bot.add_cog(MechanicsMain2(bot))
