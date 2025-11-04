import logging
import json
import aiomysql
import time
import aiohttp
import os
from typing import List, Tuple, Optional

from discord.ext import commands, tasks

from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# ---------------- Presence / DC grace config ----------------
DISCONNECT_GRACE_SECS = 10  # after this, a DC'd seated player is removed if not reconnected

# ---------------- Game / Round Names (Blackjack) ----------------
PHASE_PRE_GAME    = "pre-game"
PHASE_BETTING     = "betting"
PHASE_DEALING     = "dealing"
PHASE_PLAYER_TURN = "player_turn"
PHASE_DEALER_TURN = "dealer_turn"
PHASE_SHOWDOWN    = "showdown"
PHASE_POST_ROUND  = "post_round"  # kept for compatibility but no longer used in the normal flow

# Phase timers
PRE_GAME_WAIT_SECS    = 60
POST_ROUND_WAIT_SECS  = 15   # winners are shown for 15 seconds
ACTION_SECS           = 60   # per-actor move timer (betting turns & player actions)
DEALER_REVEAL_WAIT    = 2    # 2 ticks before the dealer starts auto-hitting

# ---------------- Minimums by game_mode ----------------
MODE_MIN_BET = {
    "1": 5,
    "2": 10,
    "3": 25,
    "4": 100,
}

# Optional max bet caps by mode (tune as desired)
MODE_MAX_BET = {
    "1": 500,
    "2": 1000,
    "3": 2500,
    "4": 10000,
}

# ---------------- Blackjack helpers ----------------
def rank_value_for_bj(rank: str) -> int:
    """
    Our deck uses '0' for Ten; ranks may be 'A','K','Q','J','0','9',...,'2'
    """
    r = (rank or "").upper()
    if r == "A": return 11
    if r in ("K", "Q", "J"): return 10
    if r == "0": return 10
    # digits '2'..'9'
    try:
        return int(r)
    except Exception:
        return 0

def bj_total(cards: List[dict]) -> Tuple[int, bool, bool, bool]:
    """
    Return (total, is_blackjack, is_busted, is_soft)
    cards are dicts from Deck.to_output_format() like {"code":"AS","rank":"A","suit":"S"}
    Blackjack definition: exactly 2 cards totalling 21 = blackjack.
    """
    vals = []
    aces = 0
    for c in (cards or []):
        rank = (c.get("rank") or "").upper()
        if rank in ("A",):
            aces += 1
            vals.append(11)
        elif rank in ("K","Q","J","0"):
            vals.append(10)
        else:
            try: vals.append(int(rank))
            except: vals.append(rank_value_for_bj(rank))

    total = sum(vals)
    soft = False
    # Downgrade aces from 11 -> 1 while busting
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
        soft = True  # we used soft adjustment at least once
    is_bj = (len(cards) == 2 and total == 21)
    is_busted = (total > 21)
    return total, is_bj, is_busted, (soft or aces > 0)

def safe_int(x, default=0):
    try: return int(x)
    except: return default

# ---------------- Card-code hygiene (NO BACKEND HIDING) ----------------
# The client controls visibility. We *always* transmit real codes.
VALID_SUITS = {"S","H","D","C"}
def _sanitize_card_dict(cd: Optional[dict]) -> Optional[dict]:
    """
    Ensure a card dict has a real code (never '??', never blank), and normalize to
    the output format expected by the client.
    """
    if not cd:
        return None
    code = (cd.get("code") or "").strip()
    rank = (cd.get("rank") or "").strip().upper()
    suit = (cd.get("suit") or "").strip().upper()

    # If a Card object slipped through, force to output format:
    if hasattr(cd, "to_output_format"):
        try:
            cd = cd.to_output_format()
            code = (cd.get("code") or "").strip()
            rank = (cd.get("rank") or "").strip().upper()
            suit = (cd.get("suit") or "").strip().upper()
        except Exception:
            return None

    # Reject any placeholder/hidden marker:
    if code in ("??", "BACK", "HIDE", "X", "xx", "XX"):
        return None

    # Normalize:
    if not rank and code:
        # derive rank/suit from code like 'AS' or '0D'
        rank = code[:-1].upper()
        suit = code[-1:].upper()
    if rank == "10":  # frontend prefers '0' for tens
        rank = "0"
    if suit not in VALID_SUITS or not rank:
        return None

    out_code = f"{rank}{suit}"
    return {"code": out_code, "rank": rank, "suit": suit}

def _sanitize_cards_list(cards: Optional[List[dict]]) -> List[dict]:
    out = []
    for cd in (cards or []):
        norm = _sanitize_card_dict(cd)
        if norm:
            out.append(norm)
    return out

# ----------------------------------------------------------------------
# The Cog
# ----------------------------------------------------------------------
class MechanicsMain2(commands.Cog, name="MechanicsMain2"):
    """
    Blackjack mechanics to be used behind the blackjack_ws websocket.
    IMPORTANT: The server NEVER hides cards. It always sends real codes/dicts.
               The client (gameth.php) decides what to render face-up/back.
    """
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain2 (Blackjack) initialized.")

        # DB config
        self.db_user = bot.db_user
        self.db_password = bot.db_password
        self.db_host = bot.db_host
        self.db_name = "serene_users"

        # WS bucket (populated by bot.blackjack_ws_handler via register_ws_connection)
        if not hasattr(bot, "ws_rooms"): bot.ws_rooms = {}

        # Rooms we poll for timers
        self.rooms_with_active_timers = set()
        self.check_game_timers.start()

    def cog_unload(self):
        self.check_game_timers.cancel()

    # ---------------- Utility helpers ----------------
    def _normalize_room_id(self, room_id: str) -> str:
        if not room_id: raise ValueError("room_id missing")
        return str(room_id).strip()

    async def _get_db_connection(self):
        return await aiomysql.connect(
            host=self.db_host, user=self.db_user, password=self.db_password,
            db=self.db_name, charset='utf8mb4', autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_room_config(self, room_id: str) -> dict:
        try:
            conn = await aiomysql.connect(
                host=self.db_host, user=self.db_user, password=self.db_password,
                db=self.db_name, charset='utf8mb4', autocommit=True,
                cursorclass=aiomysql.cursors.DictCursor
            )
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT game_mode, guild_id, channel_id FROM bot_game_rooms WHERE room_id = %s LIMIT 1", (room_id,))
                row = await cursor.fetchone()
                game_mode = str(row.get("game_mode") or "1") if row else "1"
                min_bet = MODE_MIN_BET.get(game_mode, MODE_MIN_BET["1"])
                max_bet = MODE_MAX_BET.get(game_mode, MODE_MAX_BET["1"])
                return {
                    "game_mode": game_mode,
                    "min_bet": int(min_bet),
                    "max_bet": int(max_bet),
                    "guild_id": row.get("guild_id") if row else None,
                    "channel_id": row.get("channel_id") if row else None
                }
        except Exception as e:
            logger.warning(f"Failed to load game_mode for room {room_id}: {e}")
            return {
                "game_mode": "1",
                "min_bet": MODE_MIN_BET["1"],
                "max_bet": MODE_MAX_BET["1"],
                "guild_id": None,
                "channel_id": None
            }

    async def _load_game_state(self, room_id: str) -> Optional[dict]:
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT game_state FROM bot_game_rooms WHERE room_id = %s", (room_id,))
                row = await cursor.fetchone()
                if row and row.get('game_state'):
                    try:
                        return json.loads(row['game_state'])
                    except Exception:
                        logger.warning(f"Bad JSON game_state for room {room_id}, resetting.")
                        return None
                return None
        finally:
            if conn: conn.close()

    async def _save_game_state(self, room_id: str, state: dict):
        # As a last line of defense, sanitize every save to guarantee real codes:
        try:
            self._sanitize_state_cards(state)
        except Exception as e:
            logger.error(f"Sanitize before save failed: {e}", exc_info=True)

        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                rows_affected = await cursor.execute(
                    "UPDATE bot_game_rooms SET game_state = %s WHERE room_id = %s",
                    (json.dumps(state), room_id)
                )
                if rows_affected == 0:
                    logger.error(f"CRITICAL: Failed to save state. Room '{room_id}' not found for update.")
            await conn.commit()
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"DB save error for room '{room_id}': {e}", exc_info=True)
            raise
        finally:
            if conn: conn.close()

    def _sanitize_state_cards(self, state: dict):
        """Make sure no placeholder/back markers exist anywhere in state."""
        # dealer
        state["dealer_hand"] = _sanitize_cards_list(state.get("dealer_hand") or [])
        # players
        for p in state.get("players", []):
            hands = p.get("hands") or []
            for h in hands:
                h["cards"] = _sanitize_cards_list(h.get("cards") or [])

    async def _save_if_current(self, room_id: str, state: dict, expected_rev: int) -> bool:
        try:
            current = await self._load_game_state(room_id)
        except Exception as e:
            logger.error(f"[{room_id}] optimistic check load failed: {e}")
            return False

        db_rev = int((current or {}).get("__rev") or 0)
        if db_rev != int(expected_rev):
            logger.info(f"[{room_id}] Skip stale save (db={db_rev}, expected={expected_rev})")
            return False

        await self._save_game_state(room_id, state)
        return True

    def register_ws_connection(self, ws, room_id: str):
        rid = self._normalize_room_id(room_id)
        self.bot.ws_rooms.setdefault(rid, set()).add(ws)
        setattr(ws, "_assigned_room", rid)
        return True

    def unregister_ws_connection(self, ws):
        room = getattr(ws, "_assigned_room", None)
        if room in self.bot.ws_rooms:
            self.bot.ws_rooms[room].discard(ws)
            if not self.bot.ws_rooms[room]:
                del self.bot.ws_rooms[room]

    # ---- Presence tagging helpers (bot.blackjack_ws_handler should set ws._player_id) ----
    def _is_ws_connected(self, room_id: str, player_id: str) -> bool:
        try:
            for ws in list(self.bot.ws_rooms.get(str(room_id), set())):
                if getattr(ws, "closed", False):
                    continue
                if str(getattr(ws, "_player_id", "")) == str(player_id):
                    return True
        except Exception:
            pass
        return False

    # ---------------- State defaults & patches ----------------
    def _mark_dirty(self, state: dict):
        state["__rev"] = int(state.get("__rev") or 0) + 1

    def _ensure_defaults(self, state: dict) -> dict:
        state.setdefault("room_id", None)
        state.setdefault("current_round", PHASE_PRE_GAME)
        state.setdefault("players", [])  # each player may have "hands": [ {cards,total,is_busted,is_standing,bet,double,surrender} ... ]
        state.setdefault("dealer_hand", [])
        state.setdefault("dealer_total", None)
        state.setdefault("deck", [])
        state.setdefault("round_timer_start", None)
        state.setdefault("round_timer_secs", None)
        state.setdefault("action_timer_start", None)
        state.setdefault("action_timer_secs", None)
        state.setdefault("action_deadline_epoch", None)
        state.setdefault("initial_countdown_triggered", False)
        state.setdefault("__rev", 0)
        state.setdefault("last_evaluation", None)
        state.setdefault("min_bet", MODE_MIN_BET["1"])
        state.setdefault("max_bet", MODE_MAX_BET["1"])
        state.setdefault("current_actor", None)  # discord_id
        state.setdefault("pending_disconnects", {})  # {discord_id: epoch_deadline}
        # added for new mechanics
        state.setdefault("dealer_reveal_triggered", False)
        state.setdefault("_betting_skip_round", {})  # {discord_id: True} mark per-round betting timeouts
        return state

    def _ensure_room_limits(self, state: dict, cfg: dict):
        if not state.get("min_bet"):
            state["min_bet"] = int(cfg.get("min_bet") or MODE_MIN_BET["1"])
            self._mark_dirty(state)
        if not state.get("max_bet"):
            state["max_bet"] = int(cfg.get("max_bet") or MODE_MAX_BET["1"])
            self._mark_dirty(state)
        if not state.get("guild_id"):   state["guild_id"]   = cfg.get("guild_id")
        if not state.get("channel_id"): state["channel_id"] = cfg.get("channel_id")

    # ---------------- Broadcasts ----------------
    async def _broadcast_state(self, room_id: str, state: dict):
        # sanitize before sending, as well:
        try:
            self._sanitize_state_cards(state)
        except Exception as e:
            logger.error(f"Sanitize before broadcast failed: {e}", exc_info=True)

        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        envelope = {
            "type": "state",
            "game_state": state,
            "room_id": room_id,
            "server_ts": int(time.time()),
        }
        msg = json.dumps(envelope)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)

    def _build_ui_hint_for_actor(self, state: dict) -> dict:
        actor = state.get("current_actor")
        hint = {
            "actor": str(actor) if actor else None,
            "can_hit": False,
            "can_stand": False,
            "can_double": False,
            "can_split": False,
            "can_surrender": False,
            "can_insure": False,
            "insurance_amount": 0,
            "double_amount": 0,
            "min_bet": int(state.get("min_bet") or 0),
            "max_bet": int(state.get("max_bet") or 0),
        }
        if not actor: return hint
        p = self._find_player(state, actor)
        if not p: return hint

        # during betting: no action bar
        if state.get("current_round") != PHASE_PLAYER_TURN:
            return hint

        # find the active hand (first non-busted/non-standing)
        hand = self._active_hand(p)
        if not hand:
            return hint

        cards = hand.get("cards", [])
        total, is_bj, is_busted, _ = bj_total(cards)
        # Base permissions
        hint["can_hit"] = (not is_busted and not hand.get("is_standing", False) and total < 21)
        hint["can_stand"] = (not is_busted and not hand.get("is_standing", False))
        # Double: exactly 2 cards and still acting on first decision on this hand
        hint["can_double"] = (len(cards) == 2 and not hand.get("has_acted", False))
        hint["double_amount"] = int(hand.get("bet") or p.get("bet") or 0)

        # Split: 2 cards same rank and not already split; implement basic gate (client shows disabled otherwise)
        if len(cards) == 2:
            r0 = (cards[0].get("rank") or "").upper()
            r1 = (cards[1].get("rank") or "").upper()
            hint["can_split"] = (r0 == r1 and not p.get("has_split", False))

        # Surrender: allow early surrender only on first decision with 2 cards
        hint["can_surrender"] = (len(cards) == 2 and not hand.get("has_acted", False))

        # Insurance: dealer upcard Ace and 2 card hand and hasn’t insured yet
        dealer_cards = state.get("dealer_hand") or []
        if len(dealer_cards) >= 1 and (dealer_cards[0].get("rank") or "").upper() == "A" and len(cards) == 2:
            hint["can_insure"] = not hand.get("insured", False)
            # conventionally up to half of bet
            base_bet = int(hand.get("bet") or p.get("bet") or 0)
            hint["insurance_amount"] = max(0, base_bet // 2)

        return hint

    async def _broadcast_tick(self, room_id: str, state: dict):
        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        payload = {
            "type": "tick",
            "room_id": room_id,
            "server_ts": int(time.time()),
            "current_round": state.get("current_round"),
            "action_deadline_epoch": state.get("action_deadline_epoch"),
            "round_timer_start": state.get("round_timer_start"),
            "round_timer_secs": state.get("round_timer_secs"),
            "current_actor": state.get("current_actor"),
            "__rev": state.get("__rev", 0),
            "ui_for_current_actor": self._build_ui_hint_for_actor(state),
        }
        msg = json.dumps(payload)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)

    def _add_room_active(self, room_id: str):
        self.rooms_with_active_timers.add(self._normalize_room_id(room_id))

    # ---------------- Player utilities ----------------
    def _find_player(self, state: dict, discord_id: str):
        for p in state.get("players", []):
            if str(p.get("discord_id")) == str(discord_id):
                return p
        return None

    def _eligible_seated(self, p: dict) -> bool:
        return bool(p and p.get("seat_id"))

    def _active_hand(self, p: dict) -> Optional[dict]:
        hands = p.get("hands") or []
        for h in hands:
            if not h.get("is_busted") and not h.get("is_standing") and not h.get("surrendered"):
                return h
        return None

    # ----- Canonical seat order & eligibility (mirrors hold'em) -----
    def _seat_num(self, seat_id: Optional[str]) -> int:
        try:
            return int(str(seat_id or "seat_9999").split("_")[-1])
        except Exception:
            return 9999

    def _seat_order_ids(self, state: dict) -> List[str]:
        """Sorted list of discord_ids by seat order (ascending), seated only."""
        pairs = []
        for p in state.get("players", []):
            if not p.get("seat_id"):
                continue
            pairs.append((self._seat_num(p.get("seat_id")), str(p.get("discord_id"))))
        pairs.sort(key=lambda t: t[0])
        return [pid for _, pid in pairs]

    def _is_within_dc_grace(self, state: dict, room_id: str, pid: str) -> bool:
        """
        Hold'em-parity presence:
          - connected == True/None  -> eligible
          - connected == False      -> eligible only if WS is live OR within DC grace window
        """
        p = self._find_player(state, pid)
        if not p or not p.get("seat_id"):
            return False

        conn_flag = p.get("connected", None)
        if conn_flag is True or conn_flag is None:
            return True  # unknown/true presence should not block seats/turns

        # explicitly disconnected: allow during grace or if socket is live
        if self._is_ws_connected(room_id, pid):
            return True

        deadline = (state.get("pending_disconnects") or {}).get(str(pid))
        if deadline and int(time.time()) < int(deadline):
            return True

        dc_since = p.get("_dc_since")
        if dc_since and (int(time.time()) - int(dc_since)) < DISCONNECT_GRACE_SECS:
            return True

        return False

    def _eligible_for_betting(self, state: dict, room_id: str, pid: str) -> bool:
        """Seated, not skipped this betting round, and hasn't placed a bet yet."""
        p = self._find_player(state, pid)
        if not p or not p.get("seat_id"):
            return False
        if not self._is_within_dc_grace(state, room_id, pid):
            return False
        skips = state.get("_betting_skip_round") or {}
        if skips.get(str(pid)):
            return False
        return safe_int(p.get("bet")) <= 0

    def _eligible_for_action(self, state: dict, room_id: str, pid: str) -> bool:
        """Seated, within DC grace, has an actionable hand (not busted/standing/surrendered)."""
        p = self._find_player(state, pid)
        if not p or not p.get("seat_id"):
            return False
        if not self._is_within_dc_grace(state, room_id, pid):
            return False
        h = self._active_hand(p)
        return bool(h)

    def _first_in_round(self, ids: List[str], predicate) -> Optional[str]:
        for pid in ids:
            if predicate(pid):
                return pid
        return None

    def _next_in_round(self, ids: List[str], current: Optional[str], predicate) -> Optional[str]:
        """One full wrap max, exactly like hold'em."""
        if not ids:
            return None
        if current in ids:
            start = ids.index(current)
        else:
            start = -1
        n = len(ids)
        for step in range(1, n+1):
            cand = ids[(start + step) % n]
            if predicate(cand):
                return cand
        return None

    def _all_bets_placed_or_skipped(self, state: dict, room_id: str) -> bool:
        ids = self._seat_order_ids(state)
        if not ids:
            return False
        skips = state.get("_betting_skip_round") or {}
        for pid in ids:
            p = self._find_player(state, pid)
            if not p:
                continue
            # DC past grace ⇒ implicitly skipped
            if not self._is_within_dc_grace(state, room_id, pid):
                continue
            if safe_int(p.get("bet")) > 0:
                continue
            if skips.get(pid):
                continue
            return False
        return True

    def _first_betting_actor(self, state: dict, room_id: str) -> Optional[str]:
        ids = self._seat_order_ids(state)
        return self._first_in_round(ids, lambda pid: self._eligible_for_betting(state, room_id, pid))

    def _advance_betting_actor(self, state: dict, room_id: str):
        ids = self._seat_order_ids(state)
        nxt = self._next_in_round(ids, state.get("current_actor"),
                                  lambda pid: self._eligible_for_betting(state, room_id, pid))
        state["current_actor"] = nxt
        if nxt:
            self._start_action_timer(state)  # 60s betting turn
        self._mark_dirty(state)

    def _first_actor(self, state: dict, room_id: str) -> Optional[str]:
        ids = self._seat_order_ids(state)
        return self._first_in_round(ids, lambda pid: self._eligible_for_action(state, room_id, pid))

    def _advance_actor(self, state: dict, room_id: str):
        ids = self._seat_order_ids(state)
        nxt = self._next_in_round(ids, state.get("current_actor"),
                                  lambda pid: self._eligible_for_action(state, room_id, pid))
        state["current_actor"] = nxt
        if nxt:
            self._start_action_timer(state)
        self._mark_dirty(state)

    # ---------------- Timers ----------------
    def _start_phase_timer(self, state: dict, seconds: int):
        state["round_timer_start"] = int(time.time()) if seconds else None
        state["round_timer_secs"] = int(seconds) if seconds else None

    def _timer_expired(self, state: dict) -> bool:
        ts = state.get("round_timer_start")
        dur = state.get("round_timer_secs")
        if not ts or not dur:
            return False
        return int(time.time()) >= (int(ts) + int(dur))

    def _start_action_timer(self, state: dict, seconds: int = ACTION_SECS):
        state["action_timer_start"] = int(time.time())
        state["action_timer_secs"] = int(seconds)
        state["action_deadline_epoch"] = state["action_timer_start"] + state["action_timer_secs"]

    def _action_timer_expired(self, state: dict) -> bool:
        ts = state.get("action_timer_start")
        dur = state.get("action_timer_secs")
        if not ts or not dur:
            return False
        return int(time.time()) >= (int(ts) + int(dur))

    # ---------------- Dealer & dealing ----------------
    def _fresh_deck(self) -> Deck:
        d = Deck()
        d.shuffle()
        return d

    def _deal_card(self, state: dict) -> dict:
        """
        ALWAYS returns a real card dict (never placeholders).
        """
        deck = Deck(cards_data=state["deck"]) if state.get("deck") else self._fresh_deck()
        c = deck.deal_card()
        state["deck"] = deck.to_output_format()
        return c.to_output_format() if c else None

    def _start_new_round(self, state: dict):
        # reset dealer & players for a new hand (but keep seats)
        state["dealer_hand"] = []
        state["dealer_total"] = None
        state["deck"] = []
        state["last_evaluation"] = None
        # reset each player’s running state
        for p in state.get("players", []):
            p["hands"] = []
            p["has_split"] = False
            # keep p["bet"] (already placed) until settlement
        self._mark_dirty(state)

    def _initial_deal(self, state: dict):
        # assume all bets > 0 have been placed or players skipped
        deck = self._fresh_deck()
        skips = state.get("_betting_skip_round") or {}
        # deal 2 to each seated who actually bet and were not skipped
        for _ in range(2):
            for p in state.get("players", []):
                if not self._eligible_seated(p):
                    continue
                pid = str(p.get("discord_id"))
                if safe_int(p.get("bet")) <= 0 or skips.get(pid):
                    continue
                if not p.get("hands"):
                    p["hands"] = [{
                        "cards": [],
                        "total": 0,
                        "is_busted": False,
                        "is_standing": False,
                        "has_acted": False,
                        "bet": int(p.get("bet") or 0),
                        "double": False,
                        "surrendered": False,
                        "insured": False,
                    }]
                # draw
                c = deck.deal_card()
                if c:
                    p["hands"][0]["cards"].append(c.to_output_format())
        # dealer 2
        dh = []
        for _ in range(2):
            c = deck.deal_card()
            if c: dh.append(c.to_output_format())
        state["dealer_hand"] = dh
        state["deck"] = deck.to_output_format()

        # compute initial totals
        for p in state.get("players", []):
            if not p.get("hands"): continue
            cards = p["hands"][0]["cards"]
            # sanitize just in case:
            p["hands"][0]["cards"] = _sanitize_cards_list(cards)
            tot, is_bj, is_busted, _ = bj_total(p["hands"][0]["cards"])
            p["hands"][0]["total"] = tot
            p["hands"][0]["is_busted"] = is_busted

        state["dealer_hand"] = _sanitize_cards_list(state["dealer_hand"])
        dt, _, dbust, _ = bj_total(state["dealer_hand"])
        state["dealer_total"] = dt
        self._mark_dirty(state)

    # ---------------- Phase transitions ----------------
    async def _to_betting(self, state: dict):
        state["current_round"] = PHASE_BETTING
        # wipe any previous per-round flags
        state["dealer_reveal_triggered"] = False
        state["_betting_skip_round"] = {}
        for p in state.get("players", []):
            p["bet"] = 0  # reset when entering betting
            p["hands"] = []
        # establish first betting actor & timer
        state["current_actor"] = self._first_betting_actor(state, state.get("room_id") or "")
        if state["current_actor"]:
            self._start_action_timer(state)  # 60s per bettor
        else:
            self._start_phase_timer(state, 0)
        self._mark_dirty(state)

    async def _to_dealing(self, state: dict):
        state["current_round"] = PHASE_DEALING
        self._start_phase_timer(state, 0)
        self._start_new_round(state)
        self._initial_deal(state)
        self._mark_dirty(state)

    async def _to_player_turn(self, state: dict):
        state["current_round"] = PHASE_PLAYER_TURN
        state["current_actor"] = self._first_actor(state, state.get("room_id") or "")
        if state["current_actor"]:
            self._start_action_timer(state)
        self._mark_dirty(state)

    async def _to_dealer_turn(self, state: dict):
        state["current_round"] = PHASE_DEALER_TURN
        state["current_actor"] = None
        state["dealer_reveal_triggered"] = True  # frontend should flip reveal on this
        # wait 2 ticks before dealer auto-hits
        self._start_phase_timer(state, DEALER_REVEAL_WAIT)
        self._mark_dirty(state)

    async def _to_showdown(self, state: dict):
        state["current_round"] = PHASE_SHOWDOWN
        self._start_phase_timer(state, POST_ROUND_WAIT_SECS)  # 15s winners screen
        # compute payouts
        await self._compute_and_credit_payouts(state)
        self._mark_dirty(state)

    async def _to_post_round(self, state: dict):
        state["current_round"] = PHASE_POST_ROUND
        # cleanup: zero bets after credit is done
        for p in state.get("players", []):
            p["bet"] = 0
        self._mark_dirty(state)

    # ---------------- Payouts ----------------
    async def _compute_and_credit_payouts(self, state: dict):
        """
        Build last_evaluation and credit winners (idempotent).
        Rules:
          - Blackjack (2-card 21) pays 3:2 unless dealer also blackjack (push).
          - Bust loses.
          - Surrender loses half.
          - Double: bet doubled; draw one card then stand (handled earlier); settle at 1:1.
          - Insurance (not fully enforced here; placeholder for future).
        """
        state["dealer_hand"] = _sanitize_cards_list(state.get("dealer_hand") or [])
        dealer_cards = state["dealer_hand"]
        d_total, d_bj, d_bust, _ = bj_total(dealer_cards)
        d_is_blackjack = (len(dealer_cards) == 2 and d_total == 21)

        payouts = {}  # discord_id -> amount delta (+ means credit)
        winner_lines = []
        eval_rows = []

        for p in state.get("players", []):
            pid = str(p.get("discord_id"))
            name = p.get("name") or "Player"
            hands = p.get("hands") or []
            base_bet = safe_int(p.get("bet"), 0)

            # if no hand (didn't bet), skip row
            if not hands:
                eval_rows.append({"name": name, "hand_type": "", "is_winner": False, "discord_id": pid, "amount_won": 0})
                continue

            # For now single hand; if you later support splits, iterate all hands and accumulate
            h = hands[0]
            h["cards"] = _sanitize_cards_list(h.get("cards") or [])
            bet = safe_int(h.get("bet"), base_bet)
            doubled = bool(h.get("double"))
            surrender = bool(h.get("surrendered"))
            cards = h.get("cards") or []
            t, is_bj, bust, _ = bj_total(cards)

            # Adjust wager for double
            effective_bet = bet * (2 if doubled else 1)

            delta = 0
            if surrender:
                # lose half (round down)
                delta = - (effective_bet // 2)
            elif bust:
                delta = - effective_bet
            else:
                if d_bust:
                    # dealer busts: player wins 1:1 unless player has blackjack (still 3:2)
                    if is_bj and not d_is_blackjack:
                        delta = int(1.5 * bet)  # blackjack uses original bet, not doubled
                    else:
                        delta = effective_bet
                else:
                    if is_bj and not d_is_blackjack:
                        delta = int(1.5 * bet)
                    elif d_is_blackjack and is_bj:
                        delta = 0  # push on mutual blackjack
                    else:
                        if t > d_total:
                            delta = effective_bet
                        elif t < d_total:
                            delta = - effective_bet
                        else:
                            delta = 0  # push

            payouts[pid] = payouts.get(pid, 0) + delta
            eval_rows.append({
                "name": name,
                "hand_type": "Blackjack" if is_bj else f"{t}",
                "is_winner": (delta > 0),
                "discord_id": pid,
                "amount_won": delta
            })

        # winner_lines for the UI
        for pid, amt in payouts.items():
            nm = None
            for row in eval_rows:
                if row["discord_id"] == pid:
                    nm = row["name"]; break
            if nm is None: nm = "Player"
            if amt != 0:
                sign = "+" if amt > 0 else "-"
                winner_lines.append(f"{nm}: {sign}${abs(amt):,}")

        state["last_evaluation"] = {
            "evaluations": eval_rows,
            "dealer_evaluation": {"hand_type": "Blackjack" if d_is_blackjack else f"{d_total}"},
            "winner_lines": winner_lines
        }
        state["pending_payouts"] = {"payouts": {k: int(v) for k, v in payouts.items()}, "credited": False}

        try:
            await self._execute_payouts(state)
        except Exception as e:
            logger.error(f"Payout failure at showdown: {e}", exc_info=True)

    async def _execute_payouts(self, state: dict):
        info = state.get("pending_payouts") or {}
        if info.get("credited"):
            return

        payouts = (info.get("payouts") or {})
        # mark credited early to be safe (idempotent)
        info["credited"] = True
        state["pending_payouts"] = info
        self._mark_dirty(state)

        if not payouts:
            return

        guild_id = state.get("guild_id")
        for pid, amount in payouts.items():
            if amount <= 0:
                # losses are absorbed by the house, no debit needed
                continue
            try:
                await self._credit_kekchipz(guild_id, pid, int(amount))
            except Exception as e:
                logger.error(f"Failed to credit {pid} amount={amount}: {e}")

    async def _credit_kekchipz(self, guild_id: Optional[str], discord_id: str, amount: int):
        if amount <= 0:
            return
        secret = os.environ.get("BOT_ENTRY", "")
        form = aiohttp.FormData()
        form.add_field("action", "credit")
        form.add_field("guild_id", guild_id or "")
        form.add_field("discord_id", str(discord_id))
        form.add_field("amount", str(amount))
        headers = {"X-Serene-Auth": secret} if secret else None
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://serenekeks.com/withdraw_kekchipz.php",
                data=form,
                headers=headers,
                timeout=10,
            )
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                raise RuntimeError(f"Credit failed: non-JSON response: {text[:256]}")
            if not data.get("ok"):
                raise RuntimeError(f"Credit failed: {data}")

    # ---------------- Disconnect / empties ----------------
    def _force_pre_game_if_empty_seats(self, state: dict) -> bool:
        seated = [p for p in state.get("players", []) if p.get("seat_id")]
        now = int(time.time())
        if seated:
            state.pop("_empty_since", None)
            return False
        t0 = state.get("_empty_since")
        if not t0:
            state["_empty_since"] = now
            return False
        if (now - int(t0)) < 2:
            return False

        # clear to pre-game
        state["current_round"] = PHASE_PRE_GAME
        state["dealer_hand"] = []; state["dealer_total"] = None
        state["deck"] = []
        state["round_timer_start"] = None; state["round_timer_secs"] = None
        state["action_timer_start"] = None; state["action_timer_secs"] = None; state["action_deadline_epoch"] = None
        state["initial_countdown_triggered"] = False
        state["current_actor"] = None
        state["last_evaluation"] = None
        # keep players list but drop hands/bets
        for p in state.get("players", []):
            p["hands"] = []; p["bet"] = 0
        state.pop("_empty_since", None)
        self._mark_dirty(state)
        return True

    def _remove_player_by_id(self, state: dict, player_id: str):
        pid = str(player_id)
        state["players"] = [q for q in state.get("players", []) if str(q.get("discord_id")) != pid]
        if str(state.get("current_actor") or "") == pid:
            state["current_actor"] = None
        self._mark_dirty(state)

    def _reap_players_with_dead_ws(self, state: dict, room_id: str) -> bool:
        now = int(time.time())
        changed = False
        for p in list(state.get("players", [])):
            pid = str(p.get("discord_id") or "")
            if not pid: continue
            t0 = p.get("_dc_since")
            # start stamp if no live socket
            if not t0 and not self._is_ws_connected(room_id, pid):
                p["_dc_since"] = now
                self._mark_dirty(state)
                changed = True
                t0 = now
            if not t0: continue
            # if reconnected, clear
            if self._is_ws_connected(room_id, pid):
                p.pop("_dc_since", None)
                (state.setdefault("pending_disconnects", {})).pop(pid, None)
                self._mark_dirty(state)
                changed = True
                continue
            if (now - int(t0)) >= DISCONNECT_GRACE_SECS:
                self._remove_player_by_id(state, pid)
                (state.setdefault("pending_disconnects", {})).pop(pid, None)
                changed = True
        return changed

    def _reap_pending_disconnects(self, state: dict, room_id: str) -> bool:
        pend = dict(state.get("pending_disconnects") or {})
        if not pend: return False
        now = int(time.time())
        changed = False
        for pid, deadline in list(pend.items()):
            if now < int(deadline):
                continue
            if self._is_ws_connected(room_id, pid):
                state["pending_disconnects"].pop(pid, None)
                self._mark_dirty(state)
                changed = True
                continue
            self._remove_player_by_id(state, pid)
            state["pending_disconnects"].pop(pid, None)
            self._mark_dirty(state)
            changed = True
        return changed

    # ---------------- Timer loop ----------------
    @tasks.loop(seconds=1.0)
    async def check_game_timers(self):
        if not self.rooms_with_active_timers:
            return

        for room_id in list(self.rooms_with_active_timers):
            rid = self._normalize_room_id(room_id)
            try:
                state = await self._load_game_state(rid)
                if not state:
                    continue
                self._ensure_defaults(state)
                # stamp the room id so helpers that read it don't see None
                if not state.get("room_id"):
                    state["room_id"] = rid

                # room limits from DB once
                cfg = await self._load_room_config(rid)
                self._ensure_room_limits(state, cfg)

                before_rev = int(state.get("__rev") or 0)

                if self._force_pre_game_if_empty_seats(state):
                    if await self._save_if_current(rid, state, before_rev):
                        await self._broadcast_state(rid, state)
                    self._add_room_active(rid)
                    continue

                # DC reaps
                if self._reap_players_with_dead_ws(state, rid):
                    pass
                if self._reap_pending_disconnects(state, rid):
                    pass

                phase = state.get("current_round", PHASE_PRE_GAME)

                # --- Unify actor validity with hold'em ---
                ids = self._seat_order_ids(state)
                if phase == PHASE_BETTING:
                    if state.get("current_actor") and not self._eligible_for_betting(state, rid, str(state["current_actor"])):
                        state["current_actor"] = self._next_in_round(ids, state.get("current_actor"),
                                                                    lambda pid: self._eligible_for_betting(state, rid, pid))
                        if state.get("current_actor"):
                            self._start_action_timer(state)
                        self._mark_dirty(state)
                    if not state.get("current_actor") and not self._all_bets_placed_or_skipped(state, rid):
                        first = self._first_betting_actor(state, rid)
                        if first:
                            state["current_actor"] = first
                            self._start_action_timer(state)
                            self._mark_dirty(state)

                elif phase == PHASE_PLAYER_TURN:
                    if state.get("current_actor") and not self._eligible_for_action(state, rid, str(state["current_actor"])):
                        state["current_actor"] = self._next_in_round(ids, state.get("current_actor"),
                                                                    lambda pid: self._eligible_for_action(state, rid, pid))
                        if state.get("current_actor"):
                            self._start_action_timer(state)
                        self._mark_dirty(state)

                if phase == PHASE_PRE_GAME:
                    t0 = state.get("pre_game_timer_start")
                    if t0 and int(time.time()) >= int(t0) + PRE_GAME_WAIT_SECS:
                        await self._to_betting(state)

                elif phase == PHASE_BETTING:
                    # per-player 60s betting turns: timeout -> skip for this round
                    if state.get("current_actor"):
                        if self._action_timer_expired(state):
                            pid = str(state["current_actor"])
                            skips = state.setdefault("_betting_skip_round", {})
                            skips[pid] = True
                            self._mark_dirty(state)
                            self._advance_betting_actor(state, rid)
                    else:
                        # no actor -> attempt to select first pending bettor
                        nxt = self._first_betting_actor(state, rid)
                        if nxt:
                            state["current_actor"] = nxt
                            self._start_action_timer(state)
                            self._mark_dirty(state)

                    # When all seated either bet or were skipped, deal
                    if self._all_bets_placed_or_skipped(state, rid):
                        await self._to_dealing(state)
                        await self._to_player_turn(state)

                elif phase == PHASE_PLAYER_TURN:
                    if not state.get("current_actor"):
                        # no actor means advance to dealer (reveal + 2s pause then hit)
                        await self._to_dealer_turn(state)
                    else:
                        if self._action_timer_expired(state):
                            # timeout -> auto-stand current actor's active hand
                            actor = state["current_actor"]
                            p = self._find_player(state, actor)
                            if p:
                                h = self._active_hand(p)
                                if h: h["is_standing"] = True
                            self._mark_dirty(state)
                            self._advance_actor(state, rid)
                            if not state.get("current_actor"):
                                await self._to_dealer_turn(state)

                elif phase == PHASE_DEALER_TURN:
                    # Wait for reveal delay, then auto-hit while total <= 16. Stop at >=17 or bust.
                    if self._timer_expired(state):
                        while True:
                            total, is_bj, is_busted, soft = bj_total(state.get("dealer_hand") or [])
                            if total <= 16:
                                c = self._deal_card(state)
                                if c: state["dealer_hand"].append(_sanitize_card_dict(c))
                                continue
                            break
                        state["dealer_hand"] = _sanitize_cards_list(state["dealer_hand"])
                        dt, _, _, _ = bj_total(state["dealer_hand"])
                        state["dealer_total"] = dt
                        self._mark_dirty(state)
                        await self._to_showdown(state)

                elif phase == PHASE_SHOWDOWN:
                    # After 15s of showing winners, go straight to a new betting round
                    if self._timer_expired(state):
                        for p in state.get("players", []):
                            p["bet"] = 0
                        await self._to_betting(state)

                elif phase == PHASE_POST_ROUND:
                    # For compatibility, immediately start betting again
                    await self._to_betting(state)

                # Save/broadcast
                after_rev = int(state.get("__rev") or 0)
                changed = (after_rev != before_rev)
                if changed:
                    if await self._save_if_current(rid, state, before_rev):
                        await self._broadcast_state(rid, state)
                else:
                    # heartbeat tick for countdowns/action bar
                    await self._broadcast_tick(rid, state)

                self._add_room_active(rid)

            except Exception as e:
                logger.error(f"[TIMER TASK] Error checking room '{rid}': {e}", exc_info=True)
                self.rooms_with_active_timers.discard(rid)

    @check_game_timers.before_loop
    async def before_check_game_timers(self):
        await self.bot.wait_until_ready()

    # ---------------- Connect/Disconnect hooks ----------------
    async def player_connect(self, room_id: str, discord_id: str):
        room_id = self._normalize_room_id(room_id)
        try:
            state = await self._load_game_state(room_id) or {'room_id': room_id}
            self._ensure_defaults(state)
            if not state.get("room_id"):
                state["room_id"] = room_id
            p = self._find_player(state, str(discord_id))
            if not p: return True, ""
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
                await self._save_game_state(room_id, state)
        except Exception as e:
            logger.error(f"player_connect error [{room_id}/{discord_id}]: {e}", exc_info=True)
            return False, str(e)
        return True, ""

    async def player_disconnect(self, room_id: str, discord_id: str):
        room_id = self._normalize_room_id(room_id)
        try:
            state = await self._load_game_state(room_id) or {'room_id': room_id}
            self._ensure_defaults(state)
            if not state.get("room_id"):
                state["room_id"] = room_id
            p = self._find_player(state, str(discord_id))
            if not p: return True, ""
            changed = False
            if p.get("connected") is not False:
                p["connected"] = False; changed = True
            if not p.get("_dc_since"):
                p["_dc_since"] = int(time.time()); changed = True
            pend = state.setdefault("pending_disconnects", {})
            deadline = int(time.time()) + DISCONNECT_GRACE_SECS
            if pend.get(str(discord_id)) != deadline:
                pend[str(discord_id)] = deadline; changed = True
            if changed:
                self._mark_dirty(state)
                await self._save_game_state(room_id, state)
        except Exception as e:
            logger.error(f"player_disconnect error [{room_id}/{discord_id}]: {e}", exc_info=True)
            return False, str(e)
        return True, ""

    # ---------------- Websocket action handler ----------------
    async def handle_websocket_game_action(self, data: dict):
        """
        This is called by the blackjack_ws handler in bot.py.
        Accepts:
          - 'player_sit', 'player_leave'  (universal)
          - 'advance_phase'               (universal/admin)
          - 'player_action' with moves: 'bet','hit','stand','double','split','surrender','insurance'
        """
        # --- NEW: normalize payload to dict in case upstream passed a JSON string ---
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                logger.error("handle_websocket_game_action: received non-dict and non-JSON payload; ignoring")
                return
        if not isinstance(data, dict):
            logger.error("handle_websocket_game_action: payload is not a dict; ignoring")
            return

        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))

        try:
            state = await self._load_game_state(room_id)
            if state is None:
                state = {'room_id': room_id, 'current_round': PHASE_PRE_GAME, 'players': []}

            self._ensure_defaults(state)
            # stamp room
            state["room_id"] = room_id

            # room limits presence
            cfg = await self._load_room_config(room_id)
            self._ensure_room_limits(state, cfg)

            # optimistic rev (only for timer saves; actions will save unconditionally)
            before_rev = int(state.get("__rev") or 0)

            # minimal enrichment
            state['guild_id'] = state.get('guild_id') or data.get('guild_id')
            state['channel_id'] = state.get('channel_id') or data.get('channel_id')

            # empty table debounce
            self._force_pre_game_if_empty_seats(state)

            # If anyone interacts and pre-game countdown already elapsed, move to betting
            t0 = state.get('pre_game_timer_start')
            if state.get('current_round') == PHASE_PRE_GAME and t0 and time.time() >= t0 + PRE_GAME_WAIT_SECS:
                await self._to_betting(state)
                self._add_room_active(room_id)

            if action == 'player_sit':
                pdata = data.get('player_data', {})
                if not isinstance(pdata, dict):
                    pdata = {}
                seat_id = pdata.get('seat_id')
                player_id = str(pdata.get('discord_id') or data.get('sender_id'))
                if seat_id and player_id:
                    # prevent dup seat/dup player
                    if any(str(p.get('discord_id')) == player_id for p in state['players']):
                        pass
                    elif any(str(p.get('seat_id')) == str(seat_id) for p in state['players']):
                        pass
                    else:
                        state['players'].append({
                            'discord_id': player_id,
                            'name': pdata.get('name', 'Player'),
                            'seat_id': seat_id,
                            'avatar_url': pdata.get('avatar_url'),
                            'bet': 0,
                            'hands': [],
                            'connected': True,
                        })
                        # start pre-game countdown (first sitter)
                        if state['current_round'] == PHASE_PRE_GAME and not state.get('initial_countdown_triggered'):
                            state['pre_game_timer_start'] = time.time()
                            state['initial_countdown_triggered'] = True
                            self._add_room_active(room_id)
                        self._mark_dirty(state)

            elif action == 'player_leave':
                player_id = str(data.get('sender_id') or data.get('discord_id'))
                self._remove_player_by_id(state, player_id)
                self._add_room_active(room_id)

            elif action == 'advance_phase':
                phase = state.get('current_round')
                if phase == PHASE_PRE_GAME:
                    await self._to_betting(state)
                elif phase == PHASE_BETTING:
                    if self._all_bets_placed_or_skipped(state, room_id):
                        await self._to_dealing(state); await self._to_player_turn(state)
                elif phase == PHASE_DEALING:
                    await self._to_player_turn(state)
                elif phase == PHASE_PLAYER_TURN:
                    await self._to_dealer_turn(state)
                elif phase == PHASE_DEALER_TURN:
                    # emulate reveal delay elapsed then proceed to showdown
                    while True:
                        total, is_bj, is_busted, soft = bj_total(state.get("dealer_hand") or [])
                        if total <= 16:
                            c = self._deal_card(state)
                            if c: state["dealer_hand"].append(_sanitize_card_dict(c))
                            continue
                        break
                    state["dealer_hand"] = _sanitize_cards_list(state["dealer_hand"])
                    dt, _, _, _ = bj_total(state["dealer_hand"])
                    state["dealer_total"] = dt
                    self._mark_dirty(state)
                    await self._to_showdown(state)
                elif phase == PHASE_SHOWDOWN:
                    # jump to betting
                    for p in state.get("players", []):
                        p["bet"] = 0
                    await self._to_betting(state)
                elif phase == PHASE_POST_ROUND:
                    await self._to_betting(state)
                self._add_room_active(room_id)

            elif action == 'player_action':
                move = (data.get("move") or "").lower()
                actor = str(data.get("sender_id") or data.get("discord_id"))
                amount = safe_int(data.get("amount"), 0)

                changed = False

                # BETTING phase (per-player, actor-gated; hold'em parity)
                if move == "bet" and state.get("current_round") == PHASE_BETTING:
                    if state.get("current_actor") == actor and self._eligible_for_betting(state, room_id, actor):
                        p = self._find_player(state, actor)
                        if p:
                            mn = int(state.get("min_bet") or 0)
                            mx = int(state.get("max_bet") or 0)
                            if amount >= mn and (mx <= 0 or amount <= mx):
                                p["bet"] = amount
                                # clear any skip flag for safety
                                (state.setdefault("_betting_skip_round", {})).pop(actor, None)
                                changed = True
                                self._mark_dirty(state)
                                if self._all_bets_placed_or_skipped(state, room_id):
                                    await self._to_dealing(state)
                                    await self._to_player_turn(state)
                                else:
                                    self._advance_betting_actor(state, room_id)
                                self._add_room_active(room_id)
                    # else: ignore late/ghost bet packets, same as hold'em

                # PLAYER TURN
                elif state.get("current_round") == PHASE_PLAYER_TURN and state.get("current_actor") == actor:
                    p = self._find_player(state, actor)
                    h = self._active_hand(p) if p else None
                    if p and h:
                        cards = h.get("cards") or []
                        total, is_bj, is_busted, _ = bj_total(cards)

                        if move == "hit":
                            c = self._deal_card(state)
                            if c:
                                h["cards"].append(_sanitize_card_dict(c))
                                total, is_bj, is_busted, _ = bj_total(h["cards"])
                                h["total"] = total
                                h["is_busted"] = is_busted
                                h["has_acted"] = True
                            if is_busted or total >= 21:
                                h["is_standing"] = True
                                self._advance_actor(state, room_id)
                            changed = True

                        elif move == "stand":
                            h["is_standing"] = True
                            h["has_acted"] = True
                            self._advance_actor(state, room_id)
                            changed = True

                        elif move == "double":
                            h["double"] = True
                            h["has_acted"] = True
                            if amount > 0:
                                h["bet"] = safe_int(h.get("bet"), safe_int(p.get("bet")))
                            c = self._deal_card(state)
                            if c:
                                h["cards"].append(_sanitize_card_dict(c))
                            total, _, is_busted, _ = bj_total(h["cards"])
                            h["total"] = total
                            h["is_busted"] = is_busted
                            h["is_standing"] = True
                            self._advance_actor(state, room_id)
                            changed = True

                        elif move == "split":
                            pass

                        elif move == "surrender":
                            if len(cards) == 2 and not h.get("has_acted", False):
                                h["surrendered"] = True
                                h["is_standing"] = True
                                h["has_acted"] = True
                                self._advance_actor(state, room_id)
                                changed = True

                        elif move == "insurance":
                            if amount > 0 and not h.get("insured", False):
                                h["insured"] = True
                                h["insurance_amount"] = amount
                                h["has_acted"] = True
                                changed = True

                        if changed and not state.get("current_actor"):
                            await self._to_dealer_turn(state)

                        if changed:
                            self._mark_dirty(state)
                            self._add_room_active(room_id)

                # final save/broadcast if changed — authoritative save from action handler
                if int(state.get("__rev") or 0) != int(before_rev):
                    await self._save_game_state(room_id, state)
                    await self._broadcast_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

# ---------------- Cog setup ----------------
async def setup(bot):
    await bot.add_cog(MechanicsMain2(bot))
