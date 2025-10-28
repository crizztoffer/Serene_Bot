import logging
import json
import aiomysql
import time
from itertools import combinations

from discord.ext import commands, tasks

# Import Card and Deck from your utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# ---------------- Hand Evaluation (unchanged from your version) ----------------
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
    "pre_flop",  # betting
    "flop",      # reveal 3 -> then pre_turn betting
    "pre_turn",  # betting
    "turn",      # reveal 1 -> then pre_river betting
    "pre_river", # betting
    "river",     # reveal 1 -> then pre_showdown betting
    "pre_showdown", # betting
    "showdown",  # reveal all / compute results
    "post_showdown" # winners shown, then go to pre_flop (skip pre-game)
]

# Phase timers
PRE_GAME_WAIT_SECS = 60
POST_SHOWDOWN_WAIT_SECS = 15

# Per-player action timer (betting rounds)
ACTION_SECS = 60

BETTING_ROUNDS = {"pre_flop", "pre_turn", "pre_river", "pre_showdown"}

class MechanicsMain(commands.Cog, name="MechanicsMain"):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain initialized (with per-player action timers).")

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

    async def player_connect(self, *args, **kwargs): return True, ""
    async def player_disconnect(self, *args, **kwargs): return True, ""

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

    async def broadcast_game_state(self, room_id: str, state: dict):
        bucket = self.bot.ws_rooms.get(room_id, set())
        if not bucket: return
        envelope = {"game_state": state, "server_ts": int(time.time())}
        msg = json.dumps(envelope)
        for ws in list(bucket):
            try: await ws.send_str(msg)
            except: self.unregister_ws_connection(ws)

    # ---------------- State defaults & evaluators ----------------
    def _ensure_defaults(self, state: dict) -> dict:
        state.setdefault("room_id", None)
        state.setdefault("current_round", "pre-game")
        state.setdefault("players", [])
        state.setdefault("pot", 0)
        state.setdefault("board_cards", [])
        state.setdefault("dealer_hand", [])
        state.setdefault("deck", [])
        state.setdefault("round_timer_start", None)   # generic phase timer
        state.setdefault("round_timer_secs", None)
        state.setdefault("action_deadline_epoch", None)
        state.setdefault("initial_countdown_triggered", False)
        return state

    def _ensure_betting_defaults(self, state: dict) -> dict:
        state.setdefault("action_order", [])           # list[str] of discord_ids
        state.setdefault("action_index", 0)            # current index into action_order
        state.setdefault("current_bettor", None)       # discord_id
        state.setdefault("action_timer_start", None)   # epoch seconds
        state.setdefault("action_timer_secs", None)    # seconds
        state.setdefault("action_deadline_epoch", None)# epoch seconds (for frontend)
        return state

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
            return
        i = (state.get("action_index", 0) + 1)
        if i >= n:
            # End of orbit -> round complete
            state["current_bettor"] = None
            state["action_index"] = n
            state["action_timer_start"] = None
            state["action_timer_secs"] = None
            state["action_deadline_epoch"] = None
            return
        state["action_index"] = i
        state["current_bettor"] = order[i]
        self._start_action_timer(state)

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
                p["is_spectating"] = False  # joined during previous hand -> now eligible
            p["hand"] = []
            p["bet"] = 0
            p["is_folded"] = False
            p["in_hand"] = bool(p.get("seat_id"))

    async def _to_pre_flop(self, state: dict):
        logger.info(f"Transition -> pre_flop for room '{state.get('room_id')}'")
        deck = Deck(); deck.shuffle()
        state["deck"] = deck.to_output_format()
        state["board_cards"] = []
        state["dealer_hand"] = []
        state["pot"] = 0
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

    async def _to_flop(self, state: dict):
        # Reveal 3 community cards
        state["board_cards"].extend(self._deal_from_deck(state, 3))
        # Immediately proceed to the betting round after flop
        state["current_round"] = "pre_turn"
        self._ensure_betting_defaults(state)
        self._build_action_order(state)

    async def _to_turn(self, state: dict):
        # Reveal 1 card
        state["board_cards"].extend(self._deal_from_deck(state, 1))
        # Proceed to betting after turn
        state["current_round"] = "pre_river"
        self._ensure_betting_defaults(state)
        self._build_action_order(state)

    async def _to_river(self, state: dict):
        # Reveal 1 card
        state["board_cards"].extend(self._deal_from_deck(state, 1))
        # Proceed to betting after river
        state["current_round"] = "pre_showdown"
        self._ensure_betting_defaults(state)
        self._build_action_order(state)

    async def _to_pre_showdown(self, state: dict):
        state["current_round"] = "pre_showdown"
        self._ensure_betting_defaults(state)
        self._build_action_order(state)

    async def _to_showdown(self, state: dict):
        state["current_round"] = "showdown"
        # TODO: evaluate players vs dealer; populate winners & payouts
        self._start_phase_timer(state, POST_SHOWDOWN_WAIT_SECS)

    async def _to_post_showdown(self, state: dict):
        state["current_round"] = "post_showdown"
        # Continue showing winners (if any) for whatever remains of the 15s (timer already started in _to_showdown)

    async def _finish_betting_round_and_advance(self, state: dict):
        # If only one player remains active, jump to showdown
        if self._active_player_count(state) <= 1:
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

    # ---------------- Timer loop ----------------
    @tasks.loop(seconds=1.0)
    async def check_game_timers(self):
        if not self.rooms_with_active_timers:
            return

        for room_id in list(self.rooms_with_active_timers):
            try:
                state = await self._load_game_state(room_id)
                if not state:
                    self.rooms_with_active_timers.discard(room_id)
                    continue

                self._ensure_defaults(state)
                self._ensure_betting_defaults(state)

                phase = state.get("current_round", "pre-game")

                # Pre-game: wait 60s from first seat
                if phase == "pre-game":
                    t0 = state.get("pre_flop_timer_start_time")
                    if t0 and int(time.time()) >= int(t0) + PRE_GAME_WAIT_SECS:
                        await self._to_pre_flop(state)

                # Betting rounds: enforce per-player auto timer
                elif phase in BETTING_ROUNDS:
                    if not state.get("current_bettor"):
                        # End of orbit
                        await self._finish_betting_round_and_advance(state)
                    else:
                        # If timer expired, auto-fold and advance
                        if self._action_timer_expired(state):
                            pid = state["current_bettor"]
                            for p in state["players"]:
                                if str(p.get("discord_id")) == str(pid):
                                    p["is_folded"] = True
                                    p["in_hand"] = False
                                    # Optional: forfeit current bet to pot here
                                    # state["pot"] = int(state.get("pot", 0)) + int(p.get("bet", 0))
                                    # p["bet"] = 0
                                    break
                            if self._active_player_count(state) <= 1:
                                await self._finish_betting_round_and_advance(state)
                            else:
                                self._advance_bettor_pointer(state)

                elif phase == "showdown":
                    if self._timer_expired(state):
                        await self._to_post_showdown(state)

                elif phase == "post_showdown":
                    if self._timer_expired(state):
                        await self._to_pre_flop(state)

                # Persist + broadcast
                await self._save_game_state(room_id, state)
                await self.broadcast_game_state(room_id, state)

                # Keep polling once a room is active
                self.rooms_with_active_timers.add(room_id)

            except Exception as e:
                logger.error(f"[TIMER TASK] Error checking room '{room_id}': {e}", exc_info=True)
                self.rooms_with_active_timers.discard(room_id)

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

            state['guild_id'] = data.get('guild_id')
            state['channel_id'] = data.get('channel_id')

            # Handle pre-game timeout if anyone interacts and the timer already elapsed
            t0 = state.get('pre_flop_timer_start_time')
            if state.get('current_round') == 'pre-game' and t0 and time.time() >= t0 + PRE_GAME_WAIT_SECS:
                await self._to_pre_flop(state)
                self.rooms_with_active_timers.add(room_id)

            if action == 'player_sit':
                pdata = data.get('player_data', {})
                seat_id = pdata.get('seat_id')
                player_id = str(pdata.get('discord_id') or data.get('sender_id'))
                if not seat_id or not player_id:
                    return

                # Already seated? ignore
                if any(str(p.get('discord_id')) == player_id for p in state['players']):
                    return

                # Seat free?
                if any(str(p.get('seat_id')) == str(seat_id) for p in state['players']):
                    return

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
                })

                # First eligible sitter: start pre-game countdown
                if len([p for p in state['players'] if not p.get('is_spectating')]) == 1 \
                    and state['current_round'] == 'pre-game' and not state.get('initial_countdown_triggered'):
                    state['pre_flop_timer_start_time'] = time.time()
                    state['initial_countdown_triggered'] = True
                    self.rooms_with_active_timers.add(room_id)
                    logger.info(f"First player sat. Room '{room_id}' added to active timer checks.")

            elif action == 'player_leave':
                player_id = str(data.get('sender_id') or data.get('discord_id'))
                p = self._find_player(state, player_id)
                if p:
                    in_betting_round = state.get("current_round") in BETTING_ROUNDS
                    was_current = (state.get("current_bettor") == player_id)

                    # If mid-hand, fold/forfeit (optional: move bet to pot)
                    if p.get('in_hand') and not p.get('is_spectating') and state.get('current_round') not in ('pre-game', 'post_showdown'):
                        # state['pot'] = int(state.get('pot', 0)) + int(p.get('bet', 0))
                        # p['bet'] = 0
                        p['is_folded'] = True
                        p['in_hand'] = False

                    # Remove them from table entirely (free seat)
                    state['players'] = [q for q in state['players'] if str(q.get('discord_id')) != player_id]

                    # If it was their turn, advance immediately (or finish round)
                    if in_betting_round and was_current:
                        if self._active_player_count(state) <= 1:
                            await self._finish_betting_round_and_advance(state)
                        else:
                            self._advance_bettor_pointer(state)

            elif action == 'fold':
                player_id = str(data.get('sender_id') or data.get('discord_id'))
                p = self._find_player(state, player_id)
                if p and p.get('in_hand') and not p.get('is_spectating') and state.get('current_round') in BETTING_ROUNDS:
                    # Only the current bettor can act (server-side enforcement)
                    if state.get("current_bettor") == player_id:
                        p['is_folded'] = True
                        p['in_hand'] = False
                        if self._active_player_count(state) <= 1:
                            await self._finish_betting_round_and_advance(state)
                        else:
                            self._advance_bettor_pointer(state)

            elif action == 'player_action':
                # payload: {"move": "check"|"call"|"bet"|"raise"|"fold", "amount": optional}
                move = (data.get("move") or "").lower()
                actor = str(data.get("sender_id") or data.get("discord_id"))
                phase = state.get("current_round")

                if phase in BETTING_ROUNDS and state.get("current_bettor") == actor:
                    p = self._find_player(state, actor)
                    if p and self._eligible_for_action(p):
                        if move == "fold":
                            p["is_folded"] = True
                            p["in_hand"] = False
                        elif move in ("check", "call", "bet", "raise"):
                            # TODO: Add betting logic, pot management, min-raise, etc.
                            # For now we only advance the pointer.
                            pass

                        if self._active_player_count(state) <= 1:
                            await self._finish_betting_round_and_advance(state)
                        else:
                            self._advance_bettor_pointer(state)

            elif action == 'advance_phase':
                # Optional admin/testing action to force phase movement
                phase = state.get('current_round')
                if phase == 'pre_flop':       await self._to_flop(state)
                elif phase == 'pre_turn':     await self._to_turn(state)
                elif phase == 'pre_river':    await self._to_river(state)
                elif phase == 'pre_showdown': await self._to_showdown(state)
                elif phase == 'showdown':     await self._to_post_showdown(state)
                elif phase == 'post_showdown':await self._to_pre_flop(state)

            elif action is not None:
                # Unknown but non-null action: ignore (still save/broadcast)
                pass
            else:
                # No action at all — nothing to do
                return

            await self._save_game_state(room_id, state)
            await self.broadcast_game_state(room_id, state)

        except Exception as e:
            logger.error(f"Error in handle_websocket_game_action ('{action}'): {e}", exc_info=True)

    # ---------------- Hand evaluation (placeholder) ----------------
    async def evaluate_hands(self, state: dict):
        """
        TODO: Use evaluate_poker_hand for each player vs board to determine winners.
        Populate state['winners'] and perform payouts, then transition to showdown/post_showdown.
        """
        pass

# ---------------- Cog setup ----------------
async def setup(bot):
    await bot.add_cog(MechanicsMain(bot))
