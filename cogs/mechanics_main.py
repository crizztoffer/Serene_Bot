import logging
import json
import aiomysql
import time
from itertools import combinations
import aiohttp
import os

from discord.ext import commands, tasks

from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# ---------------- Presence / DC grace config ----------------
DISCONNECT_GRACE_SECS = 10  # after this, a DC'd seated player is removed if not reconnected

# ---------------- Hand Evaluation (unchanged core) ----------------
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

# ---------------- Round & Timer Configuration ----------------
ROUND_ORDER = [
    "pre-game",
    "pre_flop",     # betting
    "flop",         # reveal 3 -> then pre_turn betting
    "pre_turn",     # betting
    "turn",         # reveal 1 -> then pre_river betting
    "pre_river",    # betting
    "river",        # reveal 1 -> then pre_showdown betting
    "pre_showdown", # betting
    "showdown",     # reveal all / compute results
    "post_showdown" # winners shown, then go to pre_flop (skip pre-game)
]

# Phase timers
PRE_GAME_WAIT_SECS = 60
POST_SHOWDOWN_WAIT_SECS = 15

# Per-player action timer (betting rounds)
ACTION_SECS = 60

BETTING_ROUNDS = {"pre_flop", "pre_turn", "pre_river", "pre_showdown"}

# ---------------- Minimums by game_mode ----------------
MODE_MIN_BET = {
    "1": 5,
    "2": 10,
    "3": 25,
    "4": 100,
}

class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain initialized (with per-player action timers + DC grace/reap).")

        # DB config
        self.db_user = bot.db_user
        self.db_password = bot.db_password
        self.db_host = bot.db_host
        self.db_name = "serene_users"

        # WS bucket (populated by bot.game_was_handler via register_ws_connection)
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

    async def _load_room_config(self, room_id: str) -> dict:
        """
        Fetch game_mode for the room and derive min_bet.
        """
        conn = None
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
                guild_id = row.get("guild_id") if row else None
                channel_id = row.get("channel_id") if row else None
                return {"game_mode": game_mode, "min_bet": int(min_bet), "guild_id": guild_id, "channel_id": channel_id}
        except Exception as e:
            logger.warning(f"Failed to load game_mode for room {room_id}: {e}")
            return {"game_mode": "1", "min_bet": MODE_MIN_BET["1"], "guild_id": None, "channel_id": None}
        finally:
            if conn: conn.close()

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

    # ---- NEW: presence tagging helpers ----
    def _is_ws_connected(self, room_id: str, player_id: str) -> bool:
        """
        Returns True if we see a live WS in the room tagged with this player_id.
        Requires bot.py to set ws._player_id = <sender_id> on /game_was handshake.
        """
        try:
            for ws in list(self.bot.ws_rooms.get(str(room_id), set())):
                if getattr(ws, "closed", False):
                    continue
                if str(getattr(ws, "_player_id", "")) == str(player_id):
                    return True
        except Exception:
            pass
        return False

    # ---------------- Connection hooks (now implemented) ----------------
    async def player_connect(self, room_id: str, discord_id: str):
        """
        Mark a player as connected and clear any disconnect stamp.
        """
        room_id = self._normalize_room_id(room_id)
        try:
            state = await self._load_game_state(room_id) or {'room_id': room_id, 'current_round': 'pre-game', 'players': []}
            self._ensure_defaults(state)
            p = self._find_player(state, str(discord_id))
            if not p:
                return True, ""  # not seated yet; nothing to do
            changed = False
            if p.get("connected") is not True:
                p["connected"] = True
                changed = True
            # Clear dc stamp on reconnect
            if p.pop("_dc_since", None) is not None:
                changed = True
            # Also clear any pending entry so the fallback reaper can't hit them
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
        """
        Mark a player as disconnected and stamp when that happened.
        Also seed pending_disconnects for a belt-and-suspenders fallback.
        """
        room_id = self._normalize_room_id(room_id)
        try:
            state = await self._load_game_state(room_id) or {'room_id': room_id, 'current_round': 'pre-game', 'players': []}
            self._ensure_defaults(state)
            p = self._find_player(state, str(discord_id))
            if not p:
                return True, ""
            changed = False
            if p.get("connected") is not False:
                p["connected"] = False
                changed = True
            if not p.get("_dc_since"):
                p["_dc_since"] = int(time.time())
                changed = True
            # optional pending map as secondary guard
            pend = state.setdefault("pending_disconnects", {})
            deadline = int(time.time()) + DISCONNECT_GRACE_SECS
            if pend.get(str(discord_id)) != deadline:
                pend[str(discord_id)] = deadline
                changed = True
            if changed:
                self._mark_dirty(state)
                await self._save_game_state(room_id, state)
        except Exception as e:
            logger.error(f"player_disconnect error [{room_id}/{discord_id}]: {e}", exc_info=True)
            return False, str(e)
        return True, ""

    async def _get_db_connection(self):
        return await aiomysql.connect(
            host=self.db_host, user=self.db_user, password=self.db_password,
            db=self.db_name, charset='utf8mb4', autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_game_state(self, room_id: str) -> dict | None:
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
            logger.info(f"Saved state for room '{room_id}'")
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"DB save error for room '{room_id}': {e}", exc_info=True)
            raise
        finally:
            if conn: conn.close()

    # -------- NEW: optimistic save guard (prevents stale overwrites) --------
    async def _save_if_current(self, room_id: str, state: dict, expected_rev: int) -> bool:
        """
        Save only if the DB's current __rev matches the expected_rev that we loaded.
        Prevents a timer tick snapshot from overwriting a concurrent player_sit (and vice versa).
        Returns True iff save occurred.
        """
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

    # ---------------- Partitioned broadcast helpers ----------------
    def _mark_dirty(self, state: dict):
        state["__rev"] = int(state.get("__rev") or 0) + 1

    def _has_timers(self, state: dict):
        in_bet = state.get("current_round") in {"pre_flop", "pre_turn", "pre_river", "pre_showdown"}
        has_action_deadline = isinstance(state.get("action_deadline_epoch"), int)
        pre = state.get("current_round") == "pre-game" and state.get("pre_flop_timer_start_time")
        postable = state.get("round_timer_start") and state.get("round_timer_secs")
        return bool((in_bet and has_action_deadline) or pre or postable)

    async def _broadcast_state(self, room_id: str, state: dict):
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

    def _build_ui_hint_for_current_bettor(self, state: dict) -> dict:
        actor = state.get("current_bettor")
        min_bet = int(state.get("min_bet") or 0)
        current_bet = int(state.get("current_bet") or 0)

        can_check = False
        call_amount = 0
        if actor:
            p = self._find_player(state, actor)
            if p and self._eligible_for_action(p):
                call_amount = max(0, current_bet - int(p.get("bet") or 0))
                can_check = (call_amount == 0)

        return {
            "actor": str(actor) if actor else None,
            "can_check": bool(can_check),
            "call_amount": int(call_amount),
            "min_bet": int(min_bet),
            "min_raise": int(min_bet),
            "current_bet": int(current_bet),
        }

    async def _broadcast_tick(self, room_id: str, state: dict):
        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        payload = {
            "type": "tick",
            "room_id": room_id,
            "server_ts": int(time.time()),
            "current_round": state.get("current_round"),
            "action_deadline_epoch": state.get("action_deadline_epoch"),
            "pre_flop_timer_start_time": state.get("pre_flop_timer_start_time"),
            "round_timer_start": state.get("round_timer_start"),
            "round_timer_secs": state.get("round_timer_secs"),
            "current_bettor": state.get("current_bettor"),
            "pot": state.get("pot"),
            "__rev": state.get("__rev", 0),
            "ui_for_current_bettor": self._build_ui_hint_for_current_bettor(state),
        }
        msg = json.dumps(payload)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)

    def _add_room_active(self, room_id: str):
        rid = self._normalize_room_id(room_id)
        self.rooms_with_active_timers.add(rid)

    # ---------------- State defaults & evaluators ----------------
    def _ensure_defaults(self, state: dict) -> dict:
        state.setdefault("room_id", None)
        state.setdefault("current_round", "pre-game")
        state.setdefault("players", [])
        state.setdefault("pot", 0)
        state.setdefault("board_cards", [])
        state.setdefault("dealer_hand", [])
        state.setdefault("deck", [])
        state.setdefault("round_timer_start", None)
        state.setdefault("round_timer_secs", None)
        state.setdefault("action_deadline_epoch", None)
        state.setdefault("initial_countdown_triggered", False)
        state.setdefault("__rev", 0)
        state.setdefault("last_evaluation", None)
        state.setdefault("current_bet", 0)
        state.setdefault("min_bet", MODE_MIN_BET["1"])
        state.setdefault("pending_payouts", None)
        # NEW: pending DC map for fallback reap path
        state.setdefault("pending_disconnects", {})  # {discord_id: epoch_deadline}
        return state

    def _ensure_betting_defaults(self, state: dict) -> dict:
        state.setdefault("action_order", [])
        state.setdefault("action_index", 0)
        state.setdefault("current_bettor", None)
        state.setdefault("action_timer_start", None)
        state.setdefault("action_timer_secs", None)
        state.setdefault("action_deadline_epoch", None)
        return state

    def _reset_betting_round_numbers(self, state: dict):
        state["current_bet"] = 0
        for p in state.get("players", []):
            p["bet"] = 0
        self._mark_dirty(state)

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

    # ---------------- Player filters & action order helpers ----------------
    def _find_player(self, state: dict, discord_id: str):
        for p in state["players"]:
            if str(p.get("discord_id")) == str(discord_id):
                return p
        return None

    def _seated_players_in_hand(self, state: dict):
        return [p for p in state["players"] if not p.get("is_spectating") and p.get("in_hand")]

    def _active_players(self, state: dict):
        return [p for p in self._seated_players_in_hand(state) if not p.get("is_folded")]

    def _active_player_count(self, state: dict) -> int:
        return len([p for p in state["players"] if self._eligible_for_action(p)])

    def _eligible_for_action(self, p) -> bool:
        return (
            p and p.get("in_hand") and not p.get("is_spectating")
            and not p.get("is_folded")
            and p.get("seat_id")
        )

    def _seat_num(self, p) -> int:
        try:
            sid = str(p.get("seat_id") or "")
            return int(sid.split("_")[-1])
        except Exception:
            return 9999

    def _build_action_order(self, state: dict):
        players = [p for p in state["players"] if self._eligible_for_action(p)]
        players.sort(key=self._seat_num)
        state["action_order"] = [str(p["discord_id"]) for p in players]
        state["action_index"] = 0
        state["current_bettor"] = state["action_order"][0] if state["action_order"] else None
        if state["current_bettor"]:
            self._start_action_timer(state)
        else:
            state["action_timer_start"] = None
            state["action_timer_secs"] = None
            state["action_deadline_epoch"] = None

    def _advance_bettor_pointer(self, state: dict):
        order = list(state.get("action_order") or [])
        alive_ids = {str(p.get("discord_id")) for p in state["players"] if self._eligible_for_action(p)}
        order = [pid for pid in order if pid in alive_ids]
        state["action_order"] = order
        n = len(order)
        if n == 0:
            state["current_bettor"] = None
            state["action_index"] = 0
            state["action_timer_start"] = None
            state["action_timer_secs"] = None
            state["action_deadline_epoch"] = None
            self._mark_dirty(state)
            return
        i = (state.get("action_index", 0) + 1)
        if i >= n:
            state["current_bettor"] = None
            state["action_index"] = n
            state["action_timer_start"] = None
            state["action_timer_secs"] = None
            state["action_deadline_epoch"] = None
            self._mark_dirty(state)
            return
        state["action_index"] = i
        state["current_bettor"] = order[i]
        self._start_action_timer(state)
        self._mark_dirty(state)

    # ---------------- Helpers for requirements ----------------
    def _force_pre_game_if_empty_seats(self, state: dict) -> bool:
        """
        If no players are seated, eventually force a clean 'pre-game' state.
        Debounced slightly so a single empty snapshot doesn't clobber a concurrent 'sit'.
        """
        seated = [p for p in state.get("players", []) if p.get("seat_id")]

        now = int(time.time())
        if seated:
            # Clear debounce marker when someone is seated
            state.pop("_empty_since", None)
            return False

        # Debounce: require 2s of continuous emptiness before forcing pre-game
        t0 = state.get("_empty_since")
        if not t0:
            state["_empty_since"] = now
            return False
        if (now - int(t0)) < 2:
            return False

        # Actually force pre-game
        state["current_round"] = "pre-game"
        state["board_cards"] = []
        state["dealer_hand"] = []
        state["deck"] = []
        state["pot"] = 0

        # reset timers & betting pointers
        state["round_timer_start"] = None
        state["round_timer_secs"] = None
        state["action_timer_start"] = None
        state["action_timer_secs"] = None
        state["action_deadline_epoch"] = None
        state["pre_flop_timer_start_time"] = None
        state["initial_countdown_triggered"] = False

        state["action_order"] = []
        state["action_index"] = 0
        state["current_bettor"] = None

        state["last_evaluation"] = None

        # clear debounce marker
        state.pop("_empty_since", None)

        self._reset_betting_round_numbers(state)
        self._mark_dirty(state)
        return True

    class _EvalCard:
        __slots__ = ("rank", "suit")
        def __init__(self, rank, suit):
            self.rank = rank
            self.suit = suit

    def _mk_eval_card(self, c):
        if not c:
            return None
        if isinstance(c, dict):
            code = c.get("code")
            rank = c.get("rank") or c.get("r")
            suit = c.get("suit") or c.get("s")
            if not rank or not suit:
                if isinstance(code, str) and len(code) >= 2:
                    t = code.strip().upper()
                    s = t[-1]; r = t[:-1]
                    if r in ("10", "T"): r = "0"
                    return self._EvalCard(r, s)
                return None
            rr = str(rank).upper()
            if rr in ("10", "T"): rr = "0"
            ss = str(suit).upper()[0]
            return self._EvalCard(rr, ss)
        if isinstance(c, str):
            t = c.strip().upper()
            if len(t) < 2: return None
            s = t[-1]; r = t[:-1]
            if r in ("10", "T"): r = "0"
            return self._EvalCard(r, s)
        return None

    # ---------------- Dealing & phase transitions ----------------
    def _deal_from_deck(self, state: dict, n: int):
        deck = Deck(cards_data=state["deck"]) if state.get("deck") else Deck()
        if not state.get("deck"):
            deck.shuffle()
        out = []
        for _ in range(n):
            c = deck.deal_card()
            if c: out.append(c.to_output_format())
        state["deck"] = deck.to_output_format()
        return out

    def _new_hand_reset_player_flags(self, state: dict):
        for p in state["players"]:
            if p.get("is_spectating"):
                p["is_spectating"] = False
            p["hand"] = []
            p["bet"] = 0
            p["is_folded"] = False
            p["in_hand"] = bool(p.get("seat_id"))
            # NEW: track full-hand contribution for side pots
            p["total_contributed"] = 0

    async def _to_pre_flop(self, state: dict):
        logger.info(f"Transition -> pre_flop for room '{state.get('room_id')}'")
        state["last_evaluation"] = None

        deck = Deck(); deck.shuffle()
        state["deck"] = deck.to_output_format()
        state["board_cards"] = []
        state["dealer_hand"] = []
        state["pot"] = 0
        self._reset_betting_round_numbers(state)
        self._new_hand_reset_player_flags(state)

        # Deal 2 to each eligible player and 2 to dealer
        for p in self._seated_players_in_hand(state):
            if not p.get("is_spectating"):
                p["hand"] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state["dealer_hand"] = [deck.deal_card().to_output_format(), deck.deal_card().to_output_format()]
        state["deck"] = deck.to_output_format()

        state["current_round"] = "pre_flop"
        self._ensure_betting_defaults(state)
        self._build_action_order(state)
        self._mark_dirty(state)

    async def _to_flop(self, state: dict):
        new_cards = self._deal_from_deck(state, 3)
        logger.info(f"[{state.get('room_id')}] FLOP -> {new_cards}")
        state["board_cards"].extend(new_cards)
        state["current_round"] = "pre_turn"
        self._reset_betting_round_numbers(state)
        self._ensure_betting_defaults(state)
        self._build_action_order(state)
        self._mark_dirty(state)

    async def _to_turn(self, state: dict):
        new_cards = self._deal_from_deck(state, 1)
        logger.info(f"[{state.get('room_id')}] TURN -> {new_cards}")
        state["board_cards"].extend(new_cards)
        state["current_round"] = "pre_river"
        self._reset_betting_round_numbers(state)
        self._ensure_betting_defaults(state)
        self._build_action_order(state)
        self._mark_dirty(state)

    async def _to_river(self, state: dict):
        new_cards = self._deal_from_deck(state, 1)
        logger.info(f"[{state.get('room_id')}] RIVER -> {new_cards}")
        state["board_cards"].extend(new_cards)
        state["current_round"] = "pre_showdown"
        self._reset_betting_round_numbers(state)
        self._ensure_betting_defaults(state)
        self._build_action_order(state)
        self._mark_dirty(state)

    async def _to_pre_showdown(self, state: dict):
        state["current_round"] = "pre_showdown"
        self._reset_betting_round_numbers(state)
        self._ensure_betting_defaults(state)
        self._build_action_order(state)
        self._mark_dirty(state)

    # -------- Side-pot builder --------
    def _build_side_pots(self, state: dict) -> list[dict]:
        """
        Returns a list of pots:
          [{"amount": int, "eligible": set[str]} ...]
        Using each player's total_contributed across the hand.
        Folded players are never eligible but still count toward pot sizing.
        """
        # Map id -> contribution
        contrib = {}
        elig   = set()
        for p in state.get("players", []):
            pid = str(p.get("discord_id"))
            c   = int(p.get("total_contributed") or 0)
            if c <= 0:
                continue
            contrib[pid] = c
            if p.get("in_hand") and not p.get("is_folded") and not p.get("is_spectating"):
                elig.add(pid)

        if not contrib:
            return []

        # Layer the contributions: sort distinct levels ascending
        levels = sorted(set(contrib.values()))
        pots = []
        prev = 0
        remaining = set(contrib.keys())
        for lvl in levels:
            delta = max(0, lvl - prev)
            if delta == 0:
                prev = lvl
                continue
            # Who still has >= lvl?
            layer_players = [pid for pid in remaining if contrib[pid] >= lvl]
            # For pot sizing we need the count of players who *reached* at least this level
            count_at_least = len([pid for pid, amt in contrib.items() if amt >= lvl])
            if count_at_least > 0 and delta > 0:
                pot_amount = delta * count_at_least
                pot_eligible = {pid for pid in contrib.keys() if contrib[pid] >= lvl and pid in elig}
                if pot_amount > 0:
                    pots.append({"amount": pot_amount, "eligible": pot_eligible})
            prev = lvl

        # Sanity: sum of pots should equal total pot we tracked
        # If there's drift (rounding, resets etc.), clamp last pot to match state["pot"]
        total_from_layers = sum(p["amount"] for p in pots)
        total_pot = int(state.get("pot") or 0)
        if total_from_layers != total_pot and pots:
            diff = total_pot - total_from_layers
            pots[-1]["amount"] = max(0, pots[-1]["amount"] + diff)

        return pots

    async def _to_showdown(self, state: dict):
        state["current_round"] = "showdown"

        # --- Build evaluator inputs (tolerant to strings/dicts)
        board_eval = [self._mk_eval_card(c) for c in (state.get("board_cards") or [])]
        board_eval = [c for c in board_eval if c]

        dealer_eval_cards = [self._mk_eval_card(c) for c in (state.get("dealer_hand") or [])]
        dealer_eval_cards = [c for c in dealer_eval_cards if c]

        # Safety: if missing critical cards, emit empty winners payload
        if len(board_eval) < 3 or len(dealer_eval_cards) < 2:
            state["last_evaluation"] = {"evaluations": [], "dealer_evaluation": None, "winner_lines": []}
            self._start_phase_timer(state, POST_SHOWDOWN_WAIT_SECS)
            self._mark_dirty(state)
            return

        # Dealer best
        dealer_name, dealer_score = evaluate_poker_hand(dealer_eval_cards + board_eval)

        # Evaluate players (keep score tuples for comparisons)
        eval_rows = []
        score_by_id = {}   # pid -> (hand_name, score_tuple)
        name_by_id  = {}
        active_any = False

        for p in state.get("players", []):
            pid = str(p.get("discord_id"))
            name = p.get("name") or "Player"
            name_by_id[pid] = name

            hand = p.get("hand") or []
            if len(hand) < 2:
                eval_rows.append({
                    "name": name,
                    "hand_type": "",
                    "is_winner": False,
                    "discord_id": pid,
                    "amount_won": 0
                })
                continue

            active_any = True
            p_eval = [self._mk_eval_card(c) for c in hand]
            p_eval = [c for c in p_eval if c]

            if len(p_eval) >= 2:
                p_name, p_score = evaluate_poker_hand(p_eval + board_eval)
            else:
                p_name, p_score = ("", (-1,))

            score_by_id[pid] = (p_name, p_score)

        # -------- Side pots & payouts --------
        pots = self._build_side_pots(state)
        payouts: dict[str, int] = {}

        for pot in pots:
            amount = int(pot["amount"])
            eligible = list(pot["eligible"])
            if amount <= 0 or not eligible:
                continue

            # Among eligible, compare vs dealer. Ties with dealer lose to dealer.
            # If NO player beats dealer for this pot, the pot is taken by dealer (i.e., removed from table economy).
            best_score = None
            winners = []
            for pid in eligible:
                if pid not in score_by_id:
                    continue
                _, s = score_by_id[pid]
                if s > dealer_score:
                    if (best_score is None) or (s > best_score):
                        best_score = s
                        winners = [pid]
                    elif s == best_score:
                        winners.append(pid)

            if not winners:
                # Dealer takes this side pot: nothing credited to players.
                continue

            # Split this pot among the winners
            share = amount // len(winners)
            remainder = amount - share * len(winners)
            for idx, pid in enumerate(winners):
                add = share + (1 if idx == 0 and remainder > 0 else 0)
                payouts[pid] = payouts.get(pid, 0) + add

        # Build last_evaluation + per-row amounts + pretty winner_lines
        winner_lines = []
        for pid, (hand_name, _) in score_by_id.items():
            won = int(payouts.get(pid, 0))
            is_winner = won > 0
            eval_rows.append({
                "name": name_by_id.get(pid, "Player"),
                "hand_type": hand_name,
                "is_winner": is_winner,
                "discord_id": pid,
                "amount_won": won
            })
        # Compact, human-friendly "Name: $amount" lines
        for pid, amt in payouts.items():
            winner_lines.append(f"{name_by_id.get(pid, 'Player')}: ${amt:,}")

        state["last_evaluation"] = {
            "evaluations": eval_rows,
            "dealer_evaluation": {"hand_type": dealer_name} if active_any else None,
            "winner_lines": winner_lines
        }

        # Persist pending payouts for credit
        pot_total = int(state.get("pot") or 0)
        state["pending_payouts"] = {
            "total_pot": pot_total,
            "payouts": {pid: int(amt) for pid, amt in payouts.items()},   # {discord_id: amount}
        }

        # ---- CREDIT WINNERS IMMEDIATELY (so UI refresh during showdown sees it)
        try:
            await self._execute_payouts(state)
        except Exception as e:
            logger.error(f"Payout failure at showdown: {e}", exc_info=True)

        # Start visible timer before moving to post_showdown
        self._start_phase_timer(state, POST_SHOWDOWN_WAIT_SECS)
        self._mark_dirty(state)

    async def _to_post_showdown(self, state: dict):
        state["current_round"] = "post_showdown"

        # If, for any reason, payouts weren't done at showdown, do them now (idempotent)
        if not (state.get("pending_payouts") or {}).get("credited"):
            try:
                await self._execute_payouts(state)
            except Exception as e:
                logger.error(f"Payout failure: {e}", exc_info=True)

        # Reset pot after payouts
        state["pot"] = 0
        state["pending_payouts"] = None

        self._mark_dirty(state)

    async def _finish_betting_round_and_advance(self, state: dict):
        # Only short-circuit if NO active players remain
        if self._active_player_count(state) == 0:
            await self._to_showdown(state)
            return

        phase = state.get("current_round")
        if phase == "pre_flop":
            await self._to_flop(state)
        elif phase == "pre_turn":
            await self._to_turn(state)
        elif phase == "pre_river":
            await self._to_river(state)
        elif phase == "pre_showdown":
            await self._to_showdown(state)

    # ---------------- Pot & betting mechanics ----------------
    def _room_min_bet(self, state: dict) -> int:
        return int(state.get("min_bet") or MODE_MIN_BET["1"])

    def _apply_contribution(self, state: dict, p: dict, contributed: int):
        added = max(0, int(contributed or 0))
        if added <= 0: return
        p["bet"] = int(p.get("bet") or 0) + added
        # NEW: track total across the hand for side pots
        p["total_contributed"] = int(p.get("total_contributed") or 0) + added
        state["pot"] = int(state.get("pot") or 0) + added
        self._mark_dirty(state)

    def _can_check(self, state: dict, p: dict) -> bool:
        return int(p.get("bet") or 0) >= int(state.get("current_bet") or 0)

    def _can_bet(self, state: dict, amount: int) -> bool:
        return int(state.get("current_bet") or 0) == 0 and int(amount) >= self._room_min_bet(state)

    def _can_raise(self, state: dict, p: dict, amount_delta: int) -> bool:
        return int(state.get("current_bet") or 0) > 0 and int(amount_delta) >= self._room_min_bet(state)

    def _end_of_betting_orbit(self, state: dict) -> bool:
        return state.get("current_bettor") is None

    # ---------------- Player removal helpers (used by reap + leave) ----------------
    def _remove_player_by_id(self, state: dict, player_id: str) -> str:
        """
        Remove player from the table, mirroring 'player_leave' semantics.
        Returns:
          "advance_phase" if betting round should be finished, else "none".
        """
        pid = str(player_id)
        p = self._find_player(state, pid)
        if not p:
            return "none"

        in_betting_round = state.get("current_round") in BETTING_ROUNDS
        was_current = (state.get("current_bettor") == pid)

        # If they were in hand mid-round, fold them out
        if p.get('in_hand') and not p.get('is_spectating') and state.get('current_round') not in ('pre-game', 'post_showdown'):
            p['is_folded'] = True
            p['in_hand'] = False
            self._mark_dirty(state)

        # Remove from table
        state['players'] = [q for q in state['players'] if str(q.get('discord_id')) != pid]
        self._mark_dirty(state)

        # Force pre-game if no seats
        self._force_pre_game_if_empty_seats(state)

        # Adjust betting pointer / phase
        if in_betting_round and was_current and state.get("current_round") in BETTING_ROUNDS:
            if self._active_player_count(state) == 0:
                # signal caller to advance phase (async)
                return "advance_phase"
            else:
                self._advance_bettor_pointer(state)

        return "none"

    # ---------------- Disconnect reap helpers ----------------
    def _reap_players_with_dead_ws(self, state: dict, room_id: str) -> tuple[bool, bool]:
        """
        Reap any seated player who lost WS for >= DISCONNECT_GRACE_SECS and hasn't reconnected.
        Returns (changed, need_advance_phase).

        Resiliency: if a player has no _dc_since but also no live WS, auto-stamp _dc_since = now
        so missed disconnect hooks don't leave ghosts.
        """
        now = int(time.time())
        changed = False
        need_advance = False

        for p in list(state.get("players", [])):
            pid = str(p.get("discord_id") or "")
            if not pid:
                continue

            t0 = p.get("_dc_since")

            # NEW: if they have no live WS and no dc stamp, start the grace window now
            if not t0 and not self._is_ws_connected(room_id, pid):
                p["_dc_since"] = now
                self._mark_dirty(state)
                changed = True
                t0 = now

            # If still no stamp, nothing to do for this player
            if not t0:
                continue

            # If they reconnected, clear stamps and any pending entry
            if self._is_ws_connected(room_id, pid):
                try:
                    p.pop("_dc_since", None)
                finally:
                    (state.setdefault("pending_disconnects", {})).pop(pid, None)
                self._mark_dirty(state)
                changed = True
                continue

            # If grace period not yet elapsed, wait
            if (now - int(t0)) < DISCONNECT_GRACE_SECS:
                continue

            # Otherwise, remove player
            action = self._remove_player_by_id(state, pid)
            (state.setdefault("pending_disconnects", {})).pop(pid, None)
            changed = True

            if action == "advance_phase":
                need_advance = True

        return changed, need_advance

    def _reap_pending_disconnects(self, state: dict, room_id: str) -> tuple[bool, bool]:
        """
        Secondary safety net: if state.pending_disconnects[pid] deadline has passed
        and the player still has no live WS, remove them.
        Returns (changed, need_advance_phase).
        """
        pend = dict(state.get("pending_disconnects") or {})
        if not pend:
            return False, False

        now = int(time.time())
        changed = False
        need_advance = False

        for pid, deadline in list(pend.items()):
            if now < int(deadline):
                continue
            # skip if they have reconnected
            if self._is_ws_connected(room_id, pid):
                # they reconnected: drop pending entry
                state["pending_disconnects"].pop(pid, None)
                self._mark_dirty(state)
                changed = True
                continue

            action = self._remove_player_by_id(state, pid)
            state["pending_disconnects"].pop(pid, None)
            self._mark_dirty(state)
            changed = True
            if action == "advance_phase":
                need_advance = True

        return changed, need_advance

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
                self._ensure_betting_defaults(state)

                # Ensure min_bet present (from DB), only once
                if not state.get("min_bet"):
                    cfg = await self._load_room_config(rid)
                    state["min_bet"] = int(cfg["min_bet"])
                    state.setdefault("guild_id", cfg.get("guild_id"))
                    state.setdefault("channel_id", cfg.get("channel_id"))
                    self._mark_dirty(state)

                # Capture the revision we loaded to protect our saves below
                before_rev = int(state.get("__rev") or 0)

                # If no one is seated, force pre_game (debounced) and skip further processing
                if self._force_pre_game_if_empty_seats(state):
                    # Only persist if DB still at the revision we loaded
                    if await self._save_if_current(rid, state, before_rev):
                        await self._broadcast_state(rid, state)
                    self._add_room_active(rid)
                    continue

                # --- NEW: Authoritative DC reap path (based on WS and _dc_since) ---
                changed_dc, need_advance_dc = self._reap_players_with_dead_ws(state, rid)
                if need_advance_dc and state.get("current_round") in BETTING_ROUNDS:
                    await self._finish_betting_round_and_advance(state)

                # --- Secondary: pending_disconnects fallback ---
                changed_pd, need_advance_pd = self._reap_pending_disconnects(state, rid)
                if need_advance_pd and state.get("current_round") in BETTING_ROUNDS:
                    await self._finish_betting_round_and_advance(state)

                phase = state.get("current_round", "pre-game")

                # Pre-game: wait 60s from first seat
                if phase == "pre-game":
                    t0 = state.get("pre_flop_timer_start_time")
                    if t0 and int(time.time()) >= int(t0) + PRE_GAME_WAIT_SECS:
                        await self._to_pre_flop(state)

                # Betting rounds: enforce per-player auto timer
                elif phase in BETTING_ROUNDS:
                    if not state.get("current_bettor"):
                        await self._finish_betting_round_and_advance(state)
                    else:
                        if self._action_timer_expired(state):
                            pid = state["current_bettor"]
                            for p in state["players"]:
                                if str(p.get("discord_id")) == str(pid):
                                    p["is_folded"] = True
                                    p["in_hand"] = False
                                    self._mark_dirty(state)
                                    break
                            if self._active_player_count(state) == 0:
                                await self._finish_betting_round_and_advance(state)
                            else:
                                self._advance_bettor_pointer(state)

                elif phase == "showdown":
                    if self._timer_expired(state):
                        await self._to_post_showdown(state)

                elif phase == "post_showdown":
                    if self._timer_expired(state):
                        await self._to_pre_flop(state)

                # Persist/broadcast only if structurally changed
                after_rev = int(state.get("__rev") or 0)
                changed = (after_rev != before_rev)

                if changed:
                    if await self._save_if_current(rid, state, before_rev):
                        await self._broadcast_state(rid, state)
                else:
                    if self._has_timers(state):
                        await self._broadcast_tick(rid, state)

                self._add_room_active(rid)

            except Exception as e:
                logger.error(f"[TIMER TASK] Error checking room '{rid}': {e}", exc_info=True)
                self.rooms_with_active_timers.discard(rid)

    @check_game_timers.before_loop
    async def before_check_game_timers(self):
        await self.bot.wait_until_ready()

    # ---------------- Websocket action handler ----------------
    async def handle_websocket_game_action(self, data: dict):
        action = data.get('action')
        room_id = self._normalize_room_id(data.get('room_id'))

        try:
            state = await self._load_game_state(room_id)
            if state is None:
                state = {'room_id': room_id, 'current_round': 'pre-game', 'players': []}

            self._ensure_defaults(state)
            self._ensure_betting_defaults(state)

            # Ensure min_bet present (from DB), only once
            if not state.get("min_bet"):
                cfg = await self._load_room_config(room_id)
                state["min_bet"] = int(cfg["min_bet"])
                state.setdefault("guild_id", cfg.get("guild_id"))
                state.setdefault("channel_id", cfg.get("channel_id"))
                self._mark_dirty(state)

            # Immediately enforce pre_game if table is empty (debounced inside)
            self._force_pre_game_if_empty_seats(state)

            # Capture the revision we loaded so our save is optimistic
            before_rev = int(state.get("__rev") or 0)

            state['guild_id'] = state.get('guild_id') or data.get('guild_id')
            state['channel_id'] = state.get('channel_id') or data.get('channel_id')

            # If anyone interacts and pre-game countdown already elapsed, jump to pre-flop
            t0 = state.get('pre_flop_timer_start_time')
            if state.get('current_round') == 'pre-game' and t0 and time.time() >= t0 + PRE_GAME_WAIT_SECS:
                await self._to_pre_flop(state)
                self._add_room_active(room_id)

            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id = pdata.get('seat_id')
                player_id = str(pdata.get('discord_id') or data.get('sender_id'))
                if seat_id and player_id:
                    if any(str(p.get('discord_id')) == player_id for p in state['players']):
                        pass
                    elif any(str(p.get('seat_id')) == str(seat_id) for p in state['players']):
                        pass
                    else:
                        is_mid_hand = state.get('current_round') not in ('pre-game', 'post_showdown')
                        state['players'].append({
                            'discord_id': player_id,
                            'name': pdata.get('name', 'Player'),
                            'seat_id': seat_id,
                            'avatar_url': pdata.get('avatar_url'),
                            'total_chips': 1000,
                            'hand': [],
                            'bet': 0,
                            'is_folded': False,
                            'is_spectating': bool(is_mid_hand),
                            'in_hand': not bool(is_mid_hand),
                            'total_contributed': 0,  # NEW
                            'connected': True,       # assume connected on sit
                        })
                        # remove any stale dc stamps
                        try:
                            state['players'][-1].pop('_dc_since', None)
                        except Exception:
                            pass
                        self._mark_dirty(state)

                        # First eligible sitter: start pre-game countdown
                        if len([p for p in state['players'] if not p.get('is_spectating')]) == 1 \
                            and state['current_round'] == 'pre-game' and not state.get('initial_countdown_triggered'):
                            state['pre_flop_timer_start_time'] = time.time()
                            state['initial_countdown_triggered'] = True
                            self._add_room_active(room_id)
                            self._mark_dirty(state)
                            logger.info(f"First player sat. Room '{room_id}' added to active timer checks.")

            elif action == 'player_leave':
                player_id = str(data.get('sender_id') or data.get('discord_id'))
                # Use the centralized helper so bettors/phase adjust consistently
                outcome = self._remove_player_by_id(state, player_id)
                if outcome == "advance_phase" and state.get("current_round") in BETTING_ROUNDS:
                    await self._finish_betting_round_and_advance(state)

                self._add_room_active(room_id)

            elif action == 'fold':
                player_id = str(data.get('sender_id') or data.get('discord_id'))
                p = self._find_player(state, player_id)
                if p and p.get('in_hand') and not p.get('is_spectating') and state.get('current_round') in BETTING_ROUNDS:
                    if state.get("current_bettor") == player_id:
                        p['is_folded'] = True
                        p['in_hand'] = False
                        self._mark_dirty(state)
                        if self._active_player_count(state) == 0:
                            await self._finish_betting_round_and_advance(state)
                        else:
                            self._advance_bettor_pointer(state)

                self._add_room_active(room_id)

            elif action == 'player_action':
                move = (data.get("move") or "").lower()
                actor = str(data.get("sender_id") or data.get("discord_id"))
                phase = state.get("current_round")

                if phase in BETTING_ROUNDS and state.get("current_bettor") == actor:
                    p = self._find_player(state, actor)
                    if p and self._eligible_for_action(p):
                        min_bet = self._room_min_bet(state)
                        current_bet = int(state.get("current_bet") or 0)
                        p_bet = int(p.get("bet") or 0)
                        amount = int(data.get("amount") or 0)  # NEW chips the actor already withdrew client-side

                        if move == "check":
                            if self._can_check(state, p):
                                pass
                            else:
                                logger.debug("Illegal CHECK attempted; requires call.")
                                return

                        elif move == "call":
                            needed = max(0, current_bet - p_bet)
                            if needed > 0:
                                if amount < needed:
                                    logger.debug("Insufficient CALL contribution; ignoring.")
                                    return
                                self._apply_contribution(state, p, needed)

                        elif move == "bet":
                            if not self._can_bet(state, amount):
                                logger.debug("Illegal BET (either bet exists or below min).")
                                return
                            self._apply_contribution(state, p, amount)
                            state["current_bet"] = int(p.get("bet") or 0)

                        elif move == "raise":
                            delta = amount
                            if not self._can_raise(state, p, delta):
                                logger.debug("Illegal RAISE (below min raise or no bet to raise).")
                                return
                            self._apply_contribution(state, p, delta)
                            state["current_bet"] = int(p.get("bet") or 0)

                        elif move == "fold":
                            p["is_folded"] = True
                            p["in_hand"] = False

                        if self._active_player_count(state) == 0:
                            await self._finish_betting_round_and_advance(state)
                        else:
                            self._advance_bettor_pointer(state)

                        self._mark_dirty(state)

                self._add_room_active(room_id)

            elif action == 'advance_phase':
                phase = state.get('current_round')
                if phase == 'pre_flop':       await self._to_flop(state)
                elif phase == 'pre_turn':     await self._to_turn(state)
                elif phase == 'pre_river':    await self._to_river(state)
                elif phase == 'pre_showdown': await self._to_showdown(state)
                elif phase == 'showdown':     await self._to_post_showdown(state)
                elif phase == 'post_showdown':await self._to_pre_flop(state)
                self._add_room_active(room_id)

            elif action is not None:
                self._add_room_active(room_id)
            else:
                return

            after_rev = int(state.get("__rev") or 0)
            if after_rev != before_rev:
                if await self._save_if_current(room_id, state, before_rev):
                    await self._broadcast_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

    # ---------------- Payouts ----------------
    async def _execute_payouts(self, state: dict):
        """
        Credit winners their shares (idempotent via 'credited' flag).
        """
        info = state.get("pending_payouts") or {}
        if info.get("credited"):
            return

        payouts = (info.get("payouts") or {})
        # If no payouts (dealer wins or zero pot), still mark as credited so we don't try again.
        if not payouts:
            info["credited"] = True
            state["pending_payouts"] = info
            self._mark_dirty(state)
            return

        guild_id = state.get("guild_id")
        for pid, amount in payouts.items():
            try:
                await self._credit_kekchipz(guild_id, pid, int(amount))
            except Exception as e:
                logger.error(f"Failed to credit {pid} amount={amount}: {e}")

        # mark as done (idempotency for the rest of the hand)
        info["credited"] = True
        state["pending_payouts"] = info
        self._mark_dirty(state)

    async def _credit_kekchipz(self, guild_id: str | None, discord_id: str, amount: int):
        """
        POST to your PHP credit endpoint. Use form-encoded payload (matches your withdraw endpoint).
        """
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
            # Expecting JSON like {"ok": true}
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                raise RuntimeError(f"Credit failed: non-JSON response: {text[:256]}")

            if not data.get("ok"):
                raise RuntimeError(f"Credit failed: {data}")

    # ---------------- Hand evaluation (placeholder) ----------------
    async def evaluate_hands(self, state: dict):
        pass

# ---------------- Cog setup ----------------
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
