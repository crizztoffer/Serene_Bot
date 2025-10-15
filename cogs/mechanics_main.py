import logging
import json
import aiomysql
import time
from discord.ext import commands
from itertools import combinations

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Texas/Casino Hold'em Hand Evaluation Logic ---
# NOTE: This implementation adjudicates players vs. a **dealer hand** (Casino Hold’em style).
# If you want true Texas Hold’em (players competing only vs each other), remove the dealer logic
# in evaluate_hands() and compare players against players (and add side pots).
HAND_RANKINGS = {
    "High Card": 0,
    "One Pair": 1,
    "Two Pair": 2,
    "Three of a Kind": 3,
    "Straight": 4,
    "Flush": 5,
    "Full House": 6,
    "Four of a Kind": 7,
    "Straight Flush": 8,
    "Royal Flush": 9
}

def get_rank_value(rank: str) -> int:
    """
    Map rank string to numeric value.
    This implementation expects '0' to represent Ten (10).
    Valid ranks: '2'..'9', '0' (Ten), 'J','Q','K','A'
    """
    if rank.isdigit():
        # '0' is Ten
        if rank == '0':
            return 10
        return int(rank)
    return {'J': 11, 'Q': 12, 'K': 13, 'A': 14}.get(rank, 0)

def evaluate_poker_hand(cards):
    def rank_value(card): return get_rank_value(card.rank)

    def is_straight(ranks):
        # ranks: list[int]
        ranks = sorted(set(ranks), reverse=True)
        # Wheel straight A-2-3-4-5
        if {14, 2, 3, 4, 5}.issubset(set(ranks)):
            return True, 5
        for i in range(len(ranks) - 4):
            window = ranks[i:i+5]
            if all(window[j] - window[j+1] == 1 for j in range(4)):
                return True, window[0]
        return False, None

    def classify(hand):
        ranks = sorted([rank_value(c) for c in hand], reverse=True)
        suits = [c.suit[0].upper() for c in hand]
        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        # Sort by (count desc, rank desc)
        cg = sorted(rank_counts.items(), key=lambda x: (-x[1], -x[0]))
        grouped = [r for r, _ in cg]

        flush = len(set(suits)) == 1
        straight, hi = is_straight(ranks)

        if flush and straight:
            if hi == 14:
                return "Royal Flush", (HAND_RANKINGS["Royal Flush"],)
            return "Straight Flush", (HAND_RANKINGS["Straight Flush"], hi)
        if cg[0][1] == 4:
            # Four of a kind + kicker
            return "Four of a Kind", (HAND_RANKINGS["Four of a Kind"], cg[0][0], grouped[1])
        if cg[0][1] == 3 and cg[1][1] >= 2:
            # Full house: trips rank, then pair rank
            return "Full House", (HAND_RANKINGS["Full House"], cg[0][0], cg[1][0])
        if flush:
            # Flush tiebreak: kickers high-to-low
            return "Flush", (HAND_RANKINGS["Flush"], *ranks)
        if straight:
            return "Straight", (HAND_RANKINGS["Straight"], hi)
        if cg[0][1] == 3:
            # Trips + two kickers
            return "Three of a Kind", (HAND_RANKINGS["Three of a Kind"], cg[0][0], *grouped[1:3])
        if cg[0][1] == 2 and cg[1][1] == 2:
            # Two pair: high pair, low pair, kicker
            return "Two Pair", (HAND_RANKINGS["Two Pair"], cg[0][0], cg[1][0], grouped[2])
        if cg[0][1] == 2:
            # One pair + three kickers
            return "One Pair", (HAND_RANKINGS["One Pair"], cg[0][0], *grouped[1:4])
        # High card: five kickers
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
        logger.info("MechanicsMain initialized.")
        self.db_user = self.bot.db_user
        self.db_password = self.bot.db_password
        self.db_host = self.bot.db_host
        self.db_name = "serene_users"

        self.PLAYER_TURN_TIME = 60
        self.POST_SHOWDOWN_TIME = 10

        if not hasattr(self.bot, "ws_rooms") or self.bot.ws_rooms is None:
            self.bot.ws_rooms = {}

    # ------------------------------ WS room registration helpers ------------------------------
    def _normalize_room_id(self, room_id: str) -> str:
        """Trim and validate a room id. Returns normalized id or raises ValueError."""
        if room_id is None:
            raise ValueError("room_id missing")
        rid = str(room_id).strip()
        if not rid or rid.upper() == "N/A":
            raise ValueError(f"invalid room_id: {room_id!r}")
        return rid

    def register_ws_connection(self, ws, room_id: str):
        """Bind a socket to a room bucket (called after handshake)."""
        try:
            rid = self._normalize_room_id(room_id)
        except Exception as e:
            logger.warning(f"register_ws_connection refused invalid room: {room_id!r} ({e})")
            return False
        rooms = getattr(self.bot, "ws_rooms", None)
        if rooms is None:
            self.bot.ws_rooms = {}
            rooms = self.bot.ws_rooms
        rooms.setdefault(rid, set()).add(ws)
        setattr(ws, "_assigned_room", rid)
        logger.info(f"[ws] bound connection {id(ws)} to room {rid}")
        return True

    def unregister_ws_connection(self, ws):
        """Unbind a socket from its room (called on close)."""
        rooms = getattr(self.bot, "ws_rooms", None)
        room = getattr(ws, "_assigned_room", None)
        if not rooms or not room:
            return
        bucket = rooms.get(room)
        if bucket is not None:
            bucket.discard(ws)
            if not bucket:
                rooms.pop(room, None)
        logger.info(f"[ws] unbound connection {id(ws)} from room {room}")

    async def cog_load(self):
        logger.info("MechanicsMain cog loaded.")

    async def cog_unload(self):
        logger.info("MechanicsMain cog unloaded.")

    # ---- Presence hooks used by /game_was (handshake) ----
    async def player_connect(self, room_id: str, sender_id: str, guild_id: str = None):
        """Presence note: fast/no DB write. Return (ok, message)."""
        try:
            rid = self._normalize_room_id(room_id)
            logger.info(f"[player_connect] {sender_id} connected to room {rid}. (guild_id={guild_id})")
            return True, "presence recorded"
        except Exception as e:
            logger.error(f"[player_connect] error: {e}", exc_info=True)
            return False, "presence failed"

    async def player_disconnect(self, room_id: str, sender_id: str, guild_id: str = None):
        try:
            rid = self._normalize_room_id(room_id)
            logger.info(f"[player_disconnect] {sender_id} disconnected from room {rid}. (guild_id={guild_id})")
            return True, "presence removed"
        except Exception as e:
            logger.error(f"[player_disconnect] error: {e}", exc_info=True)
            return False, "presence remove failed"

    # ---- DB helpers ----
    async def _get_db_connection(self):
        if not all([self.db_user, self.db_password, self.db_host, self.db_name]):
            logger.error("DB credentials missing in MechanicsMain.")
            raise ConnectionError("Database credentials not configured.")
        return await aiomysql.connect(
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            db=self.db_name,
            charset='utf8mb4',
            autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_game_state(self, room_id: str, guild_id: str = None, channel_id: str = None) -> dict:
        conn = None
        try:
            rid = self._normalize_room_id(room_id)
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE TRIM(room_id) = %s",
                    (rid,)
                )
                row = await cursor.fetchone()
                if row and row['game_state']:
                    state = json.loads(row['game_state'])
                    state['room_id'] = rid
                    # Keep provided guild/channel if the stored state lacks them
                    if not state.get('guild_id'):
                        state['guild_id'] = guild_id
                    if not state.get('channel_id'):
                        state['channel_id'] = channel_id
                else:
                    # New state
                    deck = Deck()
                    deck.build(); deck.shuffle()
                    state = {
                        'room_id': rid,
                        'current_round': 'pre_game',
                        'players': [],
                        'dealer_hand': [],
                        'deck': deck.to_output_format(),
                        'board_cards': [],
                        'last_evaluation': None,
                        'current_player_turn_index': -1,
                        'current_betting_round_pot': 0,
                        'current_round_min_bet': 0,
                        'last_aggressive_action_player_id': None,
                        'timer_end_time': None,
                        'dealer_button_position': 0,
                        'small_blind_amount': 5,
                        'big_blind_amount': 10,
                        'game_started_once': False,
                        'guild_id': guild_id,
                        'channel_id': channel_id
                    }

                # Back-compat & defaults
                state.setdefault('current_player_turn_index', -1)
                state.setdefault('current_betting_round_pot', 0)
                state.setdefault('current_round_min_bet', 0)
                state.setdefault('last_aggressive_action_player_id', None)
                state.setdefault('timer_end_time', None)
                state.setdefault('dealer_button_position', 0)
                state.setdefault('small_blind_amount', 5)
                state.setdefault('big_blind_amount', 10)

                # Enrich players: kekchipz + fresh Discord name/avatar
                guild = self.bot.get_guild(int(guild_id)) if guild_id else None
                for p in state.get('players', []):
                    pid = p.get('discord_id')
                    if state.get('guild_id') and pid:
                        await cursor.execute(
                            "SELECT kekchipz FROM discord_users WHERE discord_id = %s AND guild_id = %s",
                            (pid, state['guild_id'])
                        )
                        k = await cursor.fetchone()
                        p['kekchipz_overall'] = (k or {}).get('kekchipz', 0)
                    else:
                        p['kekchipz_overall'] = 0

                    if guild:
                        try:
                            m = await guild.fetch_member(int(pid))
                            p['name'] = m.display_name
                            p['avatar_url'] = str(m.avatar.url) if m.avatar else str(m.default_avatar.url)
                        except Exception:
                            pass

                    p.setdefault('total_chips', 1000)
                    p.setdefault('current_bet_in_round', 0)
                    p.setdefault('has_acted_in_round', False)
                    p.setdefault('folded', False)
                    p.setdefault('hand_revealed', False)
                    p.setdefault('is_spectating', False)
                    p.setdefault('is_all_in', False)

            await conn.commit()
            return state
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"_load_game_state error for room {room_id}: {e}", exc_info=True)
            raise
        finally:
            if conn: conn.close()

    async def _save_game_state(self, room_id: str, state: dict):
        if not isinstance(state, dict):
            logger.error("Attempted to save non-dict game_state.")
            return
        rid = self._normalize_room_id(state.get("room_id") or room_id)
        js = json.dumps(state)
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                q = "UPDATE bot_game_rooms SET game_state = %s, last_activity = NOW() WHERE TRIM(room_id) = %s"
                await cursor.execute(q, (js, rid))
                if cursor.rowcount == 0:
                    raise ValueError(f"Game room '{rid}' not found for update.")
            await conn.commit()
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"_save_game_state error for room {rid}: {e}", exc_info=True)
            raise
        finally:
            if conn:
                conn.close()

    # ---- Dealing helpers ----
    async def deal_hole_cards(self, room_id: str, state: dict):
        players_in_hand = [p for p in state.get('players', []) if p.get('seat_id') and not p.get('is_spectating')]
        if not players_in_hand:
            return False, "No active players to deal cards to.", state

        deck = Deck(state.get('deck', []))
        if state['current_round'] == 'pre_game' or not deck.cards:
            deck.build(); deck.shuffle()

        for p in players_in_hand:
            p['hand'] = []
            p['folded'] = False
            p['is_all_in'] = False
            p['current_bet_in_round'] = 0
            p['has_acted_in_round'] = False
            c1, c2 = deck.deal_card(), deck.deal_card()
            if not (c1 and c2):
                return False, "Not enough cards.", state
            p['hand'] = [c1.to_output_format(), c2.to_output_format()]

        state['deck'] = deck.to_output_format()
        state['board_cards'] = []
        return True, "Hole cards dealt.", state

    async def deal_dealer_cards(self, room_id: str, state: dict):
        deck = Deck(state.get('deck', []))
        state['dealer_hand'] = []
        c1, c2 = deck.deal_card(), deck.deal_card()
        if not (c1 and c2):
            return False, "Not enough cards to deal dealer's hand.", state
        state['dealer_hand'] = [c1.to_output_format(), c2.to_output_format()]
        state['deck'] = deck.to_output_format()
        return True, "Dealer's cards dealt.", state

    async def deal_flop(self, room_id: str, state: dict):
        if state['current_round'] != 'pre_flop':
            return False, f"Cannot deal flop from {state['current_round']}.", state
        deck = Deck(state.get('deck', []))
        board = state.get('board_cards', [])
        deck.deal_card()  # burn
        for _ in range(3):
            c = deck.deal_card()
            if not c: return False, "Not enough cards for flop.", state
            board.append(c.to_output_format())
        state['deck'] = deck.to_output_format()
        state['board_cards'] = board
        return True, "Flop dealt.", state

    async def deal_turn(self, room_id: str, state: dict):
        if state['current_round'] != 'pre-turn':
            return False, f"Cannot deal turn from {state['current_round']}.", state
        deck = Deck(state.get('deck', []))
        board = state.get('board_cards', [])
        deck.deal_card()
        c = deck.deal_card()
        if not c: return False, "Not enough cards for turn.", state
        board.append(c.to_output_format())
        state['deck'] = deck.to_output_format()
        state['board_cards'] = board
        return True, "Turn dealt.", state

    async def deal_river(self, room_id: str, state: dict):
        if state['current_round'] != 'pre-river':
            return False, f"Cannot deal river from {state['current_round']}.", state
        deck = Deck(state.get('deck', []))
        board = state.get('board_cards', [])
        deck.deal_card()
        c = deck.deal_card()
        if not c: return False, "Not enough cards for river.", state
        board.append(c.to_output_format())
        state['deck'] = deck.to_output_format()
        state['board_cards'] = board
        return True, "River dealt.", state

    async def evaluate_hands(self, room_id: str, state: dict):
        if state['current_round'] != 'pre-showdown':
            return False, f"Cannot evaluate from {state['current_round']}.", state

        players = state.get('players', [])
        board = [Card.from_output_format(c) for c in state.get('board_cards', [])]
        dealer = [Card.from_output_format(c) for c in state.get('dealer_hand', [])]
        active = [p for p in players if not p.get('folded', False) and not p.get('is_spectating')]
        if len(board) != 5:
            return False, "Board not complete.", state

        d_name, d_score = evaluate_poker_hand(dealer + board)
        dealer_eval = {
            "name": "Dealer",
            "hand_type": d_name,
            "hand_score_vector": d_score,
            "hole_cards": [c.to_output_format() for c in dealer]
        }

        evals = []
        best_player_score = d_score
        winners = []

        for p in active:
            hand = [Card.from_output_format(c) for c in p.get('hand', [])]
            name, score = evaluate_poker_hand(hand + board)
            evals.append({
                "discord_id": p['discord_id'],
                "name": p['name'],
                "hand_type": name,
                "hand_score_vector": score,
                "hole_cards": [c.to_output_format() for c in hand],
                "is_winner": False
            })
            if score > best_player_score:
                best_player_score = score
                winners = [p['discord_id']]
            elif score == best_player_score and best_player_score > d_score:
                winners.append(p['discord_id'])

        evals.sort(key=lambda x: x['hand_score_vector'], reverse=True)
        for e in evals:
            if e['discord_id'] in winners:
                e['is_winner'] = True

        winning_hand_name = evals[0]['hand_type'] if winners and evals else d_name

        state['last_evaluation'] = {
            "dealer_evaluation": dealer_eval,
            "evaluations": evals,
            "winning_info": {
                "hand_type": winning_hand_name,
                "score_vector": best_player_score,
                "winners": winners
            }
        }
        
        # Payout (single pot; no side pots)
        if winners:
            pot = state.get('current_betting_round_pot', 0)
            per = pot // max(1, len(winners))
            conn = None
            try:
                conn = await self._get_db_connection()
                async with conn.cursor() as cursor:
                    for wid in winners:
                        await cursor.execute(
                            "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE discord_id = %s AND guild_id = %s",
                            (per, wid, state['guild_id'])
                        )
                await conn.commit()
            except Exception as e:
                if conn: await conn.rollback()
                logger.error(f"evaluate_hands payout error: {e}", exc_info=True)
            finally:
                if conn: conn.close()

            for p in state.get('players', []):
                if p['discord_id'] in winners:
                    p['total_chips'] += per
                    if 'kekchipz_overall' in p:
                        p['kekchipz_overall'] += per

        return True, "Hands evaluated.", state
        
    # ---- Broadcast (room-scoped; drop dead sockets) ----
    
    async def broadcast_game_state(self, room_id: str, state: dict):
        try:
            rid = self._normalize_room_id(room_id or state.get("room_id"))
        except Exception as e:
            logger.warning(f"broadcast refused invalid room id: {room_id!r} ({e})")
            return

        state['room_id'] = rid
        rooms = getattr(self.bot, "ws_rooms", None) or {}
        bucket = rooms.get(rid, set())

        # Instrumentation to catch key-mismatch issues
        try:
            logger.info(f"[broadcast] room={rid} recipients={len(bucket)}")
        except Exception:
            pass

        if not bucket:
            logger.warning(f"[broadcast] no recipients for room {rid} (key mismatch?)")
            return

        envelope = {
            "room_id": rid,
            "server_ts": int(time.time()),
            "game_state": state
        }
        msg = json.dumps(envelope)
        to_drop = []
        for ws in list(bucket):
            try:
                await ws.send_str(msg)
            except Exception as e:
                logger.error(f"broadcast error to room {rid}: {e}", exc_info=True)
                to_drop.append(ws)
        # Cleanup failed sockets so we don't spam errors forever
        for ws in to_drop:
            try:
                bucket.discard(ws)
            except Exception:
                pass
        if not bucket:
            rooms.pop(rid, None)

    # ---- Helpers for turns/betting/flow ----
    def _get_sorted_players(self, state: dict, active_only: bool = False):
        seated = [p for p in state.get('players', []) if p.get('seat_id')]
        if active_only:
            seated = [p for p in seated if not p.get('is_spectating', False)]
        return sorted(seated, key=lambda p: int(p['seat_id'].replace('seat_', '')))

    def _get_next_active_player_index(self, state: dict, current_index: int) -> int:
        sorted_players = self._get_sorted_players(state, active_only=True)
        n = len(sorted_players)
        if n == 0: return -1
        start = (current_index + 1) % n if current_index != -1 else 0
        for i in range(n):
            idx = (start + i) % n
            p = sorted_players[idx]
            if not p.get('folded', False) and not p.get('is_all_in', False) and p.get('total_chips', 0) > 0:
                return idx
        return -1

    async def _start_player_turn(self, room_id: str, state: dict):
        sorted_players = self._get_sorted_players(state, active_only=True)
        idx = state['current_player_turn_index']
        if not sorted_players:
            state['timer_end_time'] = None
            state['current_player_turn_index'] = -1
            return state
        if len(sorted_players) == 1 and idx == -1:
            idx = 0
            state['current_player_turn_index'] = 0
        elif idx == -1 or idx >= len(sorted_players):
            state['timer_end_time'] = None
            state['current_player_turn_index'] = -1
            return state

        state['timer_end_time'] = int(time.time()) + self.PLAYER_TURN_TIME
        return state

    async def _apply_blinds(self, state: dict):
        sorted_players = self._get_sorted_players(state, active_only=True)
        n = len(sorted_players)

        if n < 2:
            state['current_round_min_bet'] = 0
            state['last_aggressive_action_player_id'] = None
            return

        dealer = state['dealer_button_position']
        sb_idx = (dealer + 1) % n
        bb_idx = (dealer + 2) % n
        sb_amt = state.get('small_blind_amount', 5)
        bb_amt = state.get('big_blind_amount', 10)

        sb = sorted_players[sb_idx] if n > sb_idx else None
        bb = sorted_players[bb_idx] if n > bb_idx else None

        if sb:
            a = min(sb_amt, sb['total_chips'])
            sb['total_chips'] -= a
            sb['current_bet_in_round'] += a
            state['current_betting_round_pot'] += a

        if bb:
            a = min(bb_amt, bb['total_chips'])
            bb['total_chips'] -= a
            bb['current_bet_in_round'] += a
            state['current_betting_round_pot'] += a

        # Defensive: if BB was effectively all-in for 0 (rare), don't regress min bet.
        bb_current = bb['current_bet_in_round'] if bb else 0
        state['current_round_min_bet'] = max(state.get('current_round_min_bet', 0), bb_current)
        state['last_aggressive_action_player_id'] = bb['discord_id'] if bb else None

        for i, p in enumerate(state['players']):
            if sb and p['discord_id'] == sb['discord_id']:
                state['players'][i] = sb
            if bb and p['discord_id'] == bb['discord_id']:
                state['players'][i] = bb

    async def _start_betting_round(self, room_id: str, state: dict):
        for p in state['players']:
            if not p.get('folded', False) and not p.get('is_spectating'):
                p['current_bet_in_round'] = 0
                p['has_acted_in_round'] = False
        state['current_round_min_bet'] = 0
        state['last_aggressive_action_player_id'] = None

        sorted_players = self._get_sorted_players(state, active_only=True)
        n = len(sorted_players)
        if n == 0: return state

        if state['current_round'] == 'pre_flop':
            await self._apply_blinds(state)
            dealer = state['dealer_button_position']
            bb_idx = (dealer + 2) % n
            first = self._get_next_active_player_index(state, bb_idx)
        else:
            dealer = state['dealer_button_position']
            first = self._get_next_active_player_index(state, dealer)

        if first != -1:
            state['current_player_turn_index'] = first
            state = await self._start_player_turn(room_id, state)
        else:
            state = await self._advance_game_phase(room_id, state)
        return state

    def _check_round_completion(self, state: dict) -> bool:
        active = [p for p in state['players'] if p.get('seat_id')
                  and not p.get('folded', False)
                  and not p.get('is_spectating')]

        if len(active) <= 1:
            return True

        highest = max((x.get('current_bet_in_round', 0) for x in active), default=0)
        for p in active:
            # Treat all-in as "acted" and not required to match further
            if p.get('is_all_in', False):
                continue
            if not p.get('has_acted_in_round', False):
                return False
            if p.get('current_bet_in_round', 0) < highest and p.get('total_chips', 0) > 0:
                return False
        return True

    # REWRITTEN STATE MACHINE
    async def _advance_game_phase(self, room_id: str, state: dict):
        current_round = state['current_round']
        logger.info(f"Attempting to advance from round: {current_round}")

        while True:
            last_round = state['current_round']

            if state['current_round'] == 'pre_flop':
                ok, _, state = await self.deal_flop(room_id, state)
                if ok: state['current_round'] = 'flop'

            elif state['current_round'] == 'flop':
                state['current_round'] = 'pre-turn'
                state = await self._start_betting_round(room_id, state)

            elif state['current_round'] == 'pre-turn':
                ok, _, state = await self.deal_turn(room_id, state)
                if ok: state['current_round'] = 'turn'

            elif state['current_round'] == 'turn':
                state['current_round'] = 'pre-river'
                state = await self._start_betting_round(room_id, state)

            elif state['current_round'] == 'pre-river':
                ok, _, state = await self.deal_river(room_id, state)
                if ok: state['current_round'] = 'river'
            
            elif state['current_round'] == 'river':
                state['current_round'] = 'pre-showdown'
                state = await self._start_betting_round(room_id, state)

            elif state['current_round'] == 'pre-showdown':
                ok, _, state = await self.evaluate_hands(room_id, state)
                if ok: state['current_round'] = 'showdown'

            elif state['current_round'] == 'showdown':
                state['current_round'] = 'post-showdown'
                state['timer_end_time'] = int(time.time()) + self.POST_SHOWDOWN_TIME
            
            # Stop chaining if we reached a state that waits for input/timer
            if state['current_round'] == last_round or \
               state['current_round'] in ['pre-turn', 'pre-river', 'pre-showdown', 'post-showdown'] or \
               (state['current_round'] == 'pre_flop' and not state.get('game_started_once')):
                break
        
        logger.info(f"Advanced to round: {state['current_round']}")
        return state

    # ---- WS Action entrypoint (used by /game_was loop) ----
    async def handle_websocket_game_action(self, request_data: dict):
        action     = request_data.get('action')
        room_id    = request_data.get('room_id')
        guild_id   = request_data.get('guild_id')
        sender     = request_data.get('sender_id')
        channel    = request_data.get('channel_id')

        if not all([action, room_id, sender]):
            logger.warning(f"WS action missing fields: {request_data}")
            return

        try:
            rid = self._normalize_room_id(room_id)
            state = await self._load_game_state(rid, guild_id, channel)
            state['room_id'] = rid
            mutating = {"add_player", "leave_player", "start_new_round_pre_flop", "player_action", "auto_action_timeout"}
            ok, msg = False, ""

            if action == "get_state":
                ok, msg = True, "state"

            elif action == "add_player":
                pdata = request_data.get('player_data')
                if not isinstance(pdata, dict): return
                ok, msg, state = await self._add_player_to_game(rid, pdata, state)

            elif action == "leave_player":
                pid = request_data.get('discord_id')
                if not pid: return
                ok, msg, state = await self._leave_player(rid, pid, state)

            elif action == "start_new_round_pre_flop":
                if state.get('current_round') in ['pre_game', 'post-showdown']:
                    ok, msg, state = await self._start_new_round_pre_flop(rid, state, guild_id, channel)
                    if ok and not state.get('game_started_once', False):
                        state['game_started_once'] = True
                else:
                    logger.info(f"start_new_round_pre_flop ignored from round {state.get('current_round')}")
                    return

            elif action == "player_action":
                pid = request_data.get('player_id')
                at  = request_data.get('action_type')
                amt = request_data.get('amount', 0)
                if not all([pid, at]): return
                ok, msg, state = await self._handle_player_action(rid, pid, at, amt, state)

            elif action == "auto_action_timeout":
                pid = request_data.get('player_id')
                if not pid: return
                ok, msg, state = await self._auto_action_on_timeout(rid, pid, state)

            else:
                return

            if ok:
                if action in mutating:
                    await self._save_game_state(rid, state)
                await self.broadcast_game_state(rid, state)

        except Exception as e:
            logger.error(f"handle_websocket_game_action error: {e}", exc_info=True)
            raise

    # ---- Player actions ----
    async def _handle_player_action(self, room_id: str, player_id: str, action_type: str, amount: int = 0, state: dict = None):
        if state is None:
            return False, "Internal error", state

        sorted_players = self._get_sorted_players(state, active_only=True)
        p = next((x for x in state['players'] if x['discord_id'] == player_id), None)
        if not p:
            return False, "Player not found in game.", state

        cur_idx = state['current_player_turn_index']
        cur = sorted_players[cur_idx] if cur_idx != -1 and len(sorted_players) > cur_idx else None
        
        if not cur or cur['discord_id'] != player_id:
            return False, "It's not your turn.", state
        if p.get('folded', False):
            return False, "You have already folded.", state

        to_call = state.get('current_round_min_bet', 0) - p.get('current_bet_in_round', 0)
        ok, msg = False, ""

        if action_type == 'fold':
            p['folded'] = True
            p['has_acted_in_round'] = True
            ok, msg = True, f"{p['name']} folded."

        elif action_type == 'check':
            if to_call > 0:
                return False, "Cannot check; a bet has been made.", state
            p['has_acted_in_round'] = True
            ok, msg = True, f"{p['name']} checked."

        elif action_type == 'call':
            bet = min(max(0, to_call), p['total_chips'])
            p['total_chips'] -= bet
            p['current_bet_in_round'] += bet
            p['has_acted_in_round'] = True
            if p['total_chips'] == 0:
                p['is_all_in'] = True
            ok, msg = True, f"{p['name']} called ${bet}."

        elif action_type in ('bet', 'raise'):
            # amount here is the additional amount to put in now (not total)
            if amount <= to_call:
                return False, f"Bet/Raise must exceed ${to_call}.", state
            if p['total_chips'] < amount:
                return False, f"Not enough chips to bet/raise ${amount}.", state
            p['total_chips'] -= amount
            p['current_bet_in_round'] += amount
            state['current_round_min_bet'] = max(state['current_round_min_bet'], p['current_bet_in_round'])
            state['last_aggressive_action_player_id'] = player_id
            for x in state['players']:
                if x['discord_id'] != player_id and not x.get('folded', False) and not x.get('is_spectating'):
                    x['has_acted_in_round'] = False
            p['has_acted_in_round'] = True
            if p['total_chips'] == 0:
                p['is_all_in'] = True
            ok, msg = True, f"{p['name']} {action_type}d ${amount}."

        elif action_type == 'all_in':
            amt = p['total_chips']
            if amt == 0:
                return False, "You have no chips to go all-in.", state
            p['total_chips'] = 0
            p['current_bet_in_round'] += amt
            p['is_all_in'] = True
            if p['current_bet_in_round'] > state['current_round_min_bet']:
                state['current_round_min_bet'] = p['current_bet_in_round']
                state['last_aggressive_action_player_id'] = player_id
                for x in state['players']:
                    if x['discord_id'] != player_id and not x.get('folded', False) and not x.get('is_spectating'):
                        x['has_acted_in_round'] = False
            p['has_acted_in_round'] = True
            ok, msg = True, f"{p['name']} went All-In!"
        else:
            return False, "Invalid action.", state

        if ok:
            if self._check_round_completion(state):
                state = await self._advance_game_phase(room_id, state)
            else:
                nxt = self._get_next_active_player_index(state, state['current_player_turn_index'])
                if nxt != -1:
                    state['current_player_turn_index'] = nxt
                    state = await self._start_player_turn(room_id, state)
                else:
                    state = await self._advance_game_phase(room_id, state)
            return True, msg, state
        return ok, msg, state

    async def _auto_action_on_timeout(self, room_id: str, player_id: str, state: dict = None):
        # Guard: only act after timer expiry
        if int(time.time()) < state.get('timer_end_time', 0):
            return False, "Turn has not timed out yet.", state
        
        to_call = state.get('current_round_min_bet', 0) - next((p.get('current_bet_in_round',0) for p in state['players'] if p['discord_id'] == player_id), 0)
        action = 'check' if to_call <= 0 else 'fold'
        return await self._handle_player_action(room_id, player_id, action, 0, state)

    async def _add_player_to_game(self, room_id: str, pdata: dict, state: dict):
        pid = pdata['discord_id']
        name = pdata['name']
        seat_id = pdata.get('seat_id')
        guild_id = state.get('guild_id')
        if not all([pid, name, seat_id, guild_id]):
            return False, "Missing player data for join.", state
        
        # Prevent multi-game join using current_room_id
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT current_room_id FROM discord_users WHERE discord_id = %s AND guild_id = %s", (pid, guild_id))
                res = await cursor.fetchone()
                if res and res.get('current_room_id') and res['current_room_id'] != room_id:
                    return False, "You are already in another game.", state

                # Add to table state
                players = state.get('players', [])
                if any(p.get('seat_id') == seat_id for p in players):
                    return False, f"Seat {seat_id} is occupied.", state
                if any(p.get('discord_id') == pid for p in players):
                    return False, "You are already in this game.", state

                is_spectator = state.get('current_round') != 'pre_game'
                
                players.append({
                    'discord_id': pid, 'name': name, 'seat_id': seat_id, 'avatar_url': pdata.get('avatar_url'),
                    'total_chips': 1000, 'hand': [], 'current_bet_in_round': 0, 'has_acted_in_round': False,
                    'folded': is_spectator, 'hand_revealed': False, 'kekchipz_overall': 0, 'is_spectating': is_spectator,
                    'is_all_in': False
                })
                state['players'] = players

                await cursor.execute("UPDATE discord_users SET current_room_id = %s WHERE discord_id = %s AND guild_id = %s", (room_id, pid, guild_id))
            await conn.commit()
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"_add_player_to_game DB error: {e}", exc_info=True)
            return False, "Database error while adding player.", state
        finally:
            if conn: conn.close()

        # Auto-start game when first player sits
        seated = self._get_sorted_players(state)
        if len(seated) >= 1 and state['current_round'] == 'pre_game':
            logger.info(f"First player sat down in room {room_id}, starting game.")
            return await self._start_new_round_pre_flop(room_id, state, state.get('guild_id'), state.get('channel_id'))

        return True, "Player added.", state

    async def _leave_player(self, room_id: str, discord_id: str, state: dict):
        players = state.get('players', [])
        player_to_remove = next((p for p in players if p['discord_id'] == discord_id), None)

        if not player_to_remove:
            return False, "Player not found.", state
        
        # Forfeit any bet to pot
        state['current_betting_round_pot'] += player_to_remove.get('current_bet_in_round', 0)
        state['players'] = [p for p in players if p['discord_id'] != discord_id]

        # Clear multi-game status
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE discord_users SET current_room_id = NULL WHERE discord_id = %s AND guild_id = %s",
                    (discord_id, state.get('guild_id'))
                )
            await conn.commit()
        except Exception as e:
            if conn: await conn.rollback()
            logger.error(f"_leave_player DB error: {e}", exc_info=True)
        finally:
            if conn: conn.close()

        return True, "Player left.", state

    async def _start_new_game(self, room_id: str, state: dict, guild_id: str = None, channel_id: str = None):
        deck = Deck(); deck.build(); deck.shuffle()
        state.update({
            'current_round': 'pre_flop', 'deck': deck.to_output_format(), 'board_cards': [], 'dealer_hand': [],
            'last_evaluation': None, 'current_player_turn_index': -1, 'current_betting_round_pot': 0,
            'current_round_min_bet': 0, 'last_aggressive_action_player_id': None, 'timer_end_time': None
        })
        for p in state['players']:
            p['hand'] = []
            p['current_bet_in_round'] = 0
            p['has_acted_in_round'] = False
            p['folded'] = False
            p['hand_revealed'] = False
            p['is_spectating'] = False
            p['is_all_in'] = False
        return True, "New game started.", state

    async def _start_new_round_pre_flop(self, room_id: str, state: dict, guild_id: str = None, channel_id: str = None):
        ok, msg, state = await self._start_new_game(room_id, state, guild_id, channel_id)
        if not ok:
            return False, msg, state
        
        seated = self._get_sorted_players(state, active_only=True)
        if not seated:
            state['current_round'] = 'pre_game'
            return True, "Round ended, waiting for players.", state

        state['dealer_button_position'] = (state.get('dealer_button_position', -1) + 1) % len(seated)
        
        ok, msg, state = await self.deal_hole_cards(room_id, state)
        if not ok: return False, msg, state
        
        ok, msg, state = await self.deal_dealer_cards(room_id, state)
        if not ok: return False, msg, state
        
        state = await self._start_betting_round(room_id, state)
        return True, "Round started.", state


# Required by bot.py
async def setup(bot):
    try:
        await bot.add_cog(MechanicsMain(bot))
    except Exception as e:
        logging.error(f"MechanicsMain setup error: {e}", exc_info=True)
