import asyncio
import json
import logging
import os
import random
import time
from typing import Dict, List, Optional, Tuple

# Use shared card/deck models
from utils.game_models import Deck, Card

import aiomysql
import aiohttp
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

# =============================
# Blackjack constants & helpers
# =============================
BJ_PREBET_WAIT_SECS = 20          # time after at least 1 seated player to allow bets
BJ_DECISION_SECS    = 25          # per-player decision window during their turn
BJ_BET_MIN_BY_MODE  = {
    "1": 5,
    "2": 25,
    "3": 100,
    "4": 250,
}

# Dealer rules: hit soft 17 (common casino rule)
DEALER_HITS_SOFT_17 = True

# Public state phases
PH_WAITING_BETS   = "waiting_bets"
PH_DEALING        = "dealing"
PH_PLAYER_TURNS   = "player_turns"
PH_DEALER_TURN    = "dealer_turn"
PH_PAYOUT         = "payout"
PH_BETWEEN        = "between_hands"

# Allowed moves from client
MOVE_HIT     = "hit"
MOVE_STAND   = "stand"
MOVE_DOUBLE  = "double"
MOVE_SPLIT   = "split"   # implemented carefully (1 split per hand)
MOVE_BET     = "bet"      # place initial bet (client withdraws chips first like poker)
MOVE_SIT     = "player_sit"
MOVE_LEAVE   = "player_leave"
MOVE_GET     = "get_state"

# =============================
# Utilities
# =============================

def _now() -> int:
    return int(time.time())


def _mk_deck_list() -> List[str]:
    """Builds and shuffles a deck using utils.game_models.Deck, returning
    a serialized list of two-character codes (e.g., 'AH', '0D').
    """
    d = Deck()
    d.shuffle(); d.shuffle()
    return d.to_output_format()


def _hand_value(cards: List[str]) -> Tuple[int, bool]:
    """
    Returns (best_total, is_soft) for card codes produced by Card.to_output_format()
    where ranks are '2'..'9','0','J','Q','K','A' and suits end with 'H','D','C','S'.
    """
    total = 0
    aces = 0
    for c in cards:
        if not c:
            continue
        r = c[:-1]  # rank is everything except final suit char
        if r == "A":
            aces += 1
            total += 1
        elif r in ("0", "J", "Q", "K"):
            total += 10
        else:
            try:
                total += int(r)
            except ValueError:
                # Unexpected rank; ignore gracefully
                pass
    # upgrade some aces to 11
    is_soft = False
    while aces > 0 and total + 10 <= 21:
        total += 10
        aces -= 1
        is_soft = True
    return total, is_soft


def _is_blackjack(cards: List[str]) -> bool:
    return len(cards) == 2 and _hand_value(cards)[0] == 21


# =============================
# Cog: MechanicsMain2 (Blackjack)
# =============================
class MechanicsMain2(commands.Cog):
    """A self-contained blackjack round manager that mirrors the public surface of
    MechanicsMain (poker) enough to plug into the same infra: DB, WS bucket,
    and bot websocket dispatcher. The game-state is persisted to
    `bot_game_rooms.game_state` (JSON) just like poker.
    """

    def __init__(self, bot):
        self.bot = bot
        self.db_user = getattr(bot, "db_user", os.getenv("DB_USER"))
        self.db_password = getattr(bot, "db_password", os.getenv("DB_PASSWORD"))
        self.db_host = getattr(bot, "db_host", os.getenv("DB_HOST"))
        self.db_name = "serene_users"

        if not hasattr(bot, "ws_rooms"):
            bot.ws_rooms = {}

        self.rooms_with_active_timers = set()
        self._tick_loop.start()

    def cog_unload(self):
        try:
            self._tick_loop.cancel()
        except Exception:
            pass

    # ------------- DB helpers -------------
    async def _db(self):
        return await aiomysql.connect(
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            db=self.db_name,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor,
        )

    async def _load_room_cfg(self, room_id: str) -> Dict:
        try:
            async with (await self._db()) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT game_mode, guild_id, channel_id FROM bot_game_rooms WHERE room_id=%s LIMIT 1",
                        (str(room_id),),
                    )
                    row = await cur.fetchone()
            gm = str((row or {}).get("game_mode") or "1")
            return {
                "game_mode": gm,
                "min_bet": BJ_BET_MIN_BY_MODE.get(gm, BJ_BET_MIN_BY_MODE["1"]),
                "guild_id": (row or {}).get("guild_id"),
                "channel_id": (row or {}).get("channel_id"),
            }
        except Exception as e:
            logger.warning(f"load_room_cfg failed for {room_id}: {e}")
            return {"game_mode": "1", "min_bet": BJ_BET_MIN_BY_MODE["1"], "guild_id": None, "channel_id": None}

    async def _load_state(self, room_id: str) -> Dict:
        try:
            async with (await self._db()) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT game_state FROM bot_game_rooms WHERE room_id=%s LIMIT 1",
                        (str(room_id),),
                    )
                    row = await cur.fetchone()
            if not row:
                return {}
            raw = row.get("game_state")
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            if not raw:
                return {}
            return json.loads(raw)
        except Exception as e:
            logger.error(f"load_state error {room_id}: {e}")
            return {}

    async def _save_state(self, room_id: str, state: Dict):
        try:
            async with (await self._db()) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE bot_game_rooms SET game_state=%s WHERE room_id=%s",
                        (json.dumps(state), str(room_id)),
                    )
        except Exception as e:
            logger.error(f"save_state error {room_id}: {e}")

    # ------------- WS room bucket -------------
    def register_ws_connection(self, ws, room_id: str):
        rid = str(room_id)
        self.bot.ws_rooms.setdefault(rid, set()).add(ws)
        setattr(ws, "_assigned_room", rid)
        return True

    def unregister_ws_connection(self, ws):
        rid = getattr(ws, "_assigned_room", None)
        try:
            if rid and rid in self.bot.ws_rooms:
                self.bot.ws_rooms[rid].discard(ws)
                if not self.bot.ws_rooms[rid]:
                    del self.bot.ws_rooms[rid]
        except Exception:
            pass

    async def _broadcast(self, room_id: str, payload: Dict):
        payload.setdefault("server_ts", _now())
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.bot.ws_rooms.get(str(room_id), set())):
            try:
                if getattr(ws, "closed", False):
                    dead.append(ws)
                    continue
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.bot.ws_rooms.get(str(room_id), set()).discard(ws)
            except Exception:
                pass

    # ------------- State scaffolding -------------
    def _ensure_defaults(self, state: Dict, room_cfg: Optional[Dict] = None):
        rcfg = room_cfg or {}
        state.setdefault("room_id", state.get("room_id"))
        state.setdefault("game", "blackjack")
        state.setdefault("current_round", PH_WAITING_BETS)
        state.setdefault("min_bet", int(rcfg.get("min_bet") or BJ_BET_MIN_BY_MODE["1"]))
        state.setdefault("guild_id", rcfg.get("guild_id"))
        state.setdefault("channel_id", rcfg.get("channel_id"))
        state.setdefault("players", [])  # each: {discord_id,name,seat_id,connected,total_chips,avatar_url}
        state.setdefault("hands", {})    # pid -> list of hands; hand: {cards, bet, stood, bust, split_allowed}
        state.setdefault("dealer", {"cards": []})
        state.setdefault("deck", [])
        state.setdefault("action_deadline_epoch", 0)
        state.setdefault("actor", None)         # whose turn (discord_id)
        state.setdefault("turn_index", 0)       # index into play order across hands
        state.setdefault("__rev", 0)

    def _rev(self, state: Dict):
        state["__rev"] = int(state.get("__rev") or 0) + 1

    # ------------- Seating & betting -------------
    def _find_player(self, state: Dict, pid: str) -> Optional[Dict]:
        for p in (state.get("players") or []):
            if str(p.get("discord_id")) == str(pid):
                return p
        return None

    def _active_seated(self, state: Dict) -> List[Dict]:
        out = []
        for p in (state.get("players") or []):
            if p.get("seat_id") and not p.get("is_spectating"):
                out.append(p)
        return out

    # Blackjack stores per-hand bets in state['hands'][pid][i]['bet']

    # ------------- Dealing & flow -------------
    def _need_deck(self, state: Dict):
        # state['deck'] stores serialized card codes via Deck.to_output_format()
        if not state.get("deck") or len(state["deck"]) < 15:
            state["deck"] = _mk_deck_list()

    def _deal_card(self, state: Dict) -> str:
        self._need_deck(state)
        try:
            deck_obj = Deck(cards_data=state.get("deck") or [])
            card = deck_obj.deal_card()
            if card is None:
                # rebuild fresh deck
                state["deck"] = _mk_deck_list()
                deck_obj = Deck(cards_data=state["deck"]) 
                card = deck_obj.deal_card()
            # persist mutated deck back to state
            state["deck"] = deck_obj.to_output_format()
            return card.to_output_format()
        except Exception:
            # Fallback: new deck
            state["deck"] = _mk_deck_list()
            deck_obj = Deck(cards_data=state["deck"]) 
            card = deck_obj.deal_card()
            state["deck"] = deck_obj.to_output_format()
            return card.to_output_format()
        except Exception:
            state["deck"] = _mk_deck()
            return state["deck"].pop()

    def _players_in_hand(self, state: Dict) -> List[str]:
        return [str(p.get("discord_id")) for p in self._active_seated(state)]

    def _start_prebet(self, state: Dict):
        state["current_round"] = PH_WAITING_BETS
        state["actor"] = None
        state["turn_index"] = 0
        state["dealer"] = {"cards": []}
        state["hands"] = {}
        state["action_deadline_epoch"] = _now() + BJ_PREBET_WAIT_SECS
        self._rev(state)

    def _can_begin_hand(self, state: Dict) -> bool:
        # Needs at least one player with a positive bet placed
        for pid, hands in (state.get("hands") or {}).items():
            for h in (hands or []):
                if int(h.get("bet") or 0) > 0:
                    return True
        return False

    def _begin_hand(self, state: Dict):
        state["current_round"] = PH_DEALING
        self._need_deck(state)
        state["dealer"] = {"cards": []}
        # initial two cards to each betting hand and dealer (1 up, 1 down)
        for pid, hands in (state.get("hands") or {}).items():
            for h in hands:
                if int(h.get("bet") or 0) > 0:
                    h["cards"] = [self._deal_card(state), self._deal_card(state)]
                    h["stood"], h["bust"], h["split_allowed"] = False, False, True
        state["dealer"]["cards"] = [self._deal_card(state), self._deal_card(state)]
        state["current_round"] = PH_PLAYER_TURNS
        state["turn_index"] = 0
        order = self._linear_play_order(state)
        state["actor"] = order[0] if order else None
        state["action_deadline_epoch"] = _now() + BJ_DECISION_SECS
        self._rev(state)

    def _linear_play_order(self, state: Dict) -> List[Tuple[str, int]]:
        """Flattens players' hands into [(pid, hand_index), ...] left-to-right by seat_id.
        Only hands with a bet > 0 and not bust are included.
        """
        def seat_num(p):
            try:
                return int(str(p.get("seat_id", "seat_0")).split("_")[-1])
            except Exception:
                return 0
        order = []
        for p in sorted(self._active_seated(state), key=seat_num):
            pid = str(p.get("discord_id"))
            for i, h in enumerate(state.get("hands", {}).get(pid, [])):
                if int(h.get("bet") or 0) > 0 and not h.get("bust") and not h.get("stood"):
                    order.append((pid, i))
        return order

    def _advance_turn(self, state: Dict):
        order = self._linear_play_order(state)
        if not order:
            # no playable hands -> dealer turn
            state["current_round"] = PH_DEALER_TURN
            state["actor"] = None
            state["action_deadline_epoch"] = _now() + 2
            self._rev(state)
            return
        idx = min(max(int(state.get("turn_index") or 0), 0), len(order) - 1)
        idx += 1
        if idx >= len(order):
            state["current_round"] = PH_DEALER_TURN
            state["actor"] = None
            state["action_deadline_epoch"] = _now() + 2
        else:
            state["turn_index"] = idx
            state["actor"] = order[idx][0]
            state["action_deadline_epoch"] = _now() + BJ_DECISION_SECS
        self._rev(state)

    def _all_hands_for(self, state: Dict, pid: str) -> List[Dict]:
        return list((state.get("hands", {}).get(str(pid)) or []))

    # ------------- Player actions -------------
    def _ensure_player_hands(self, state: Dict, pid: str):
        hands = state.setdefault("hands", {}).setdefault(str(pid), [])
        if not hands:
            hands.append({"cards": [], "bet": 0, "stood": False, "bust": False, "split_allowed": True})

    def _place_bet(self, state: Dict, pid: str, amount: int):
        self._ensure_player_hands(state, pid)
        h = state["hands"][str(pid)][0]
        h["bet"] = int(h.get("bet") or 0) + max(0, int(amount or 0))
        self._rev(state)

    def _hit(self, state: Dict, pid: str, hand_index: int):
        hands = self._all_hands_for(state, pid)
        if hand_index < 0 or hand_index >= len(hands):
            return
        h = hands[hand_index]
        if h.get("stood") or h.get("bust"):
            return
        h["cards"].append(self._deal_card(state))
        total, _ = _hand_value(h["cards"])
        if total > 21:
            h["bust"] = True
        self._rev(state)

    def _stand(self, state: Dict, pid: str, hand_index: int):
        hands = self._all_hands_for(state, pid)
        if 0 <= hand_index < len(hands):
            hands[hand_index]["stood"] = True
            self._rev(state)

    def _double(self, state: Dict, pid: str, hand_index: int):
        hands = self._all_hands_for(state, pid)
        if 0 <= hand_index < len(hands):
            h = hands[hand_index]
            if len(h["cards"]) == 2:  # simple double rule
                h["bet"] = int(h.get("bet") or 0) * 2
                h["cards"].append(self._deal_card(state))
                total, _ = _hand_value(h["cards"])
                if total > 21:
                    h["bust"] = True
                h["stood"] = True
                self._rev(state)

    def _split(self, state: Dict, pid: str, hand_index: int):
        hands = self._all_hands_for(state, pid)
        if 0 <= hand_index < len(hands):
            h = hands[hand_index]
            if not h.get("split_allowed"):
                return
            cards = h.get("cards") or []
            if len(cards) == 2 and cards[0][0] == cards[1][0]:
                # make two hands, second hand inherits bet
                card_a, card_b = cards
                bet = int(h.get("bet") or 0)
                # replace current hand
                h["cards"] = [card_a, self._deal_card(state)]
                h["split_allowed"] = False
                # add new hand
                hands.insert(hand_index + 1, {"cards": [card_b, self._deal_card(state)], "bet": bet, "stood": False, "bust": False, "split_allowed": False})
                self._rev(state)

    # ------------- Dealer + payout -------------
    def _do_dealer_play(self, state: Dict):
        d = state.get("dealer", {})
        while True:
            total, soft = _hand_value(d.get("cards") or [])
            if total < 17:
                d["cards"].append(self._deal_card(state))
                continue
            if total == 17 and soft and DEALER_HITS_SOFT_17:
                d["cards"].append(self._deal_card(state))
                continue
            break
        self._rev(state)

    def _payouts(self, state: Dict):
        d_total, _ = _hand_value(state.get("dealer", {}).get("cards") or [])
        dealer_bust = d_total > 21
        for pid, hands in (state.get("hands") or {}).items():
            for h in hands:
                bet = int(h.get("bet") or 0)
                if bet <= 0:
                    continue
                total, _ = _hand_value(h.get("cards") or [])
                win = 0
                if _is_blackjack(h.get("cards") or []) and not _is_blackjack(state.get("dealer", {}).get("cards") or []):
                    win = int(bet * 1.5) + bet  # 3:2 payout (bet returned + winnings)
                elif dealer_bust and total <= 21:
                    win = bet * 2
                elif total > 21:
                    win = 0
                elif d_total > 21:
                    win = bet * 2
                elif total > d_total:
                    win = bet * 2
                elif total == d_total:
                    win = bet  # push
                else:
                    win = 0
                h["payout"] = win
        self._rev(state)

    # ------------- Timer loop -------------
    @tasks.loop(seconds=1.0)
    async def _tick_loop(self):
        try:
            # Iterate only rooms that currently have timers running
            for rid in list(self.rooms_with_active_timers):
                state = await self._load_state(rid) or {}
                if not state:
                    self.rooms_with_active_timers.discard(rid)
                    continue
                self._ensure_defaults(state)
                phase = state.get("current_round")
                now = _now()

                # Broadcast lightweight tick for countdowns
                await self._broadcast(rid, {
                    "type": "tick",
                    "room_id": rid,
                    "current_round": phase,
                    "actor": state.get("actor"),
                    "action_deadline_epoch": state.get("action_deadline_epoch"),
                    "server_ts": now,
                })

                if now < int(state.get("action_deadline_epoch") or 0):
                    continue

                changed = False
                if phase == PH_WAITING_BETS:
                    if self._can_begin_hand(state):
                        self._begin_hand(state)
                        changed = True
                    else:
                        # extend window while someone is seated but hasn't bet yet
                        state["action_deadline_epoch"] = now + 5
                        changed = True

                elif phase == PH_PLAYER_TURNS:
                    # on timeout: auto-stand current hand to keep table moving
                    self._advance_turn(state)
                    changed = True

                elif phase == PH_DEALER_TURN:
                    self._do_dealer_play(state)
                    state["current_round"] = PH_PAYOUT
                    state["action_deadline_epoch"] = now + 1
                    changed = True

                elif phase == PH_PAYOUT:
                    self._payouts(state)
                    state["current_round"] = PH_BETWEEN
                    state["action_deadline_epoch"] = now + 5
                    changed = True

                elif phase == PH_BETWEEN:
                    self._start_prebet(state)
                    changed = True

                if changed:
                    await self._save_state(rid, state)
                    await self._broadcast(rid, {"type": "state", "room_id": rid, "game_state": state})
                # keep room live until we reach waiting_bets again
                if state.get("current_round") == PH_WAITING_BETS:
                    # keep ticking while seats exist
                    if not self._active_seated(state):
                        self.rooms_with_active_timers.discard(rid)
        except Exception as e:
            logger.error(f"tick loop error: {e}")

    @_tick_loop.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ------------- Public hooks used by WS handler -------------
    async def player_connect(self, room_id: str, discord_id: str):
        # For blackjack, we just mark them connected in-memory by staying on WS
        return True, ""

    async def player_disconnect(self, room_id: str, discord_id: str):
        return True, ""

    # ============= Main dispatcher for websocket actions =============
    async def handle_websocket_game_action(self, data: Dict):
        room_id = str(data.get("room_id"))
        sender  = str(data.get("sender_id"))
        action  = data.get("action")

        state = await self._load_state(room_id) or {}
        cfg   = await self._load_room_cfg(room_id)
        self._ensure_defaults(state, cfg)
        state["room_id"] = room_id

        # Always ensure there is at least an empty hand for seated players
        if action == MOVE_SIT:
            pdata   = data.get("player_data", {})
            seat_id = pdata.get("seat_id")
            name    = pdata.get("name") or "Player"
            avatar  = pdata.get("avatar_url")
            if seat_id and not self._find_player(state, sender):
                state["players"].append({
                    "discord_id": sender,
                    "name": name,
                    "seat_id": seat_id,
                    "avatar_url": avatar,
                    "is_spectating": False,
                    "connected": True,
                })
                self._ensure_player_hands(state, sender)
                # open a short pre-bet window once the first seat exists
                if state.get("current_round") in (PH_BETWEEN, PH_WAITING_BETS):
                    state["action_deadline_epoch"] = _now() + BJ_PREBET_WAIT_SECS
                    state["current_round"] = PH_WAITING_BETS
                self.rooms_with_active_timers.add(room_id)

        elif action == MOVE_LEAVE:
            # remove player completely
            state["players"] = [p for p in (state.get("players") or []) if str(p.get("discord_id")) != sender]
            if sender in (state.get("hands") or {}):
                del state["hands"][sender]
            if not self._active_seated(state):
                # if table empty, reset to waiting bets
                self._start_prebet(state)

        elif action == MOVE_GET:
            # just send the current state back via broadcast after save below
            pass

        elif action == MOVE_BET:
            amount = int(data.get("amount") or 0)
            min_bet = int(state.get("min_bet") or cfg.get("min_bet") or 5)
            if amount >= min_bet:
                self._place_bet(state, sender, amount)
                # ensure countdown exists
                if state.get("current_round") == PH_WAITING_BETS:
                    state["action_deadline_epoch"] = _now() + max(5, BJ_PREBET_WAIT_SECS // 2)
                self.rooms_with_active_timers.add(room_id)

        elif action == "player_action":
            move = str(data.get("move") or "").lower()
            # Which hand is acting? Default to the first available hand owned by sender
            order = self._linear_play_order(state)
            acting_index = None
            for idx, (pid, hidx) in enumerate(order):
                if pid == sender:
                    acting_index = (pid, hidx)
                    break
            if state.get("current_round") != PH_PLAYER_TURNS or not acting_index:
                # ignore if it's not their turn
                pass
            else:
                pid, hidx = acting_index
                if move == MOVE_HIT:
                    self._hit(state, pid, hidx)
                elif move == MOVE_STAND:
                    self._stand(state, pid, hidx)
                elif move == MOVE_DOUBLE:
                    self._double(state, pid, hidx)
                elif move == MOVE_SPLIT:
                    self._split(state, pid, hidx)
                # advance if current hand is done
                self._advance_turn(state)

        # Begin the hand automatically if we are in waiting_bets and at least one bet exists
        if state.get("current_round") == PH_WAITING_BETS and self._can_begin_hand(state):
            self._begin_hand(state)
            self.rooms_with_active_timers.add(room_id)

        # Persist + broadcast
        await self._save_state(room_id, state)
        await self._broadcast(room_id, {"type": "state", "room_id": room_id, "game_state": state})


async def setup(bot):
    await bot.add_cog(MechanicsMain2(bot))
