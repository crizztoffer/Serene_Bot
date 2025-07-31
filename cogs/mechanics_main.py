import logging
import json
import aiomysql
import time
import asyncio # Import asyncio for connection pooling and locks
from discord.ext import commands

# Import Card and Deck from the new game_models utility file
from cogs.utils.game_models import Card, Deck

logger = logging.getLogger(__name__)

# --- Global In-Memory Game State Cache ---
# This cache will store game states to reduce database reads.
# Key: room_id (str)
# Value: { 'game_state': dict, 'timestamp': float }
# We'll use a simple time-based invalidation for demonstration.
# In a production environment, consider a more robust caching solution like Redis.
game_state_cache = {}
CACHE_TTL_SECONDS = 5 # Cache entries expire after 5 seconds of inactivity/no update

# A lock to protect access to the cache for concurrent updates
cache_lock = asyncio.Lock()

# --- Database Connection Pool (Global) ---
# This will be initialized once and reused for all database operations.
db_pool = None

async def init_db_pool(host, user, password, db, loop):
    """Initializes the MySQL connection pool."""
    global db_pool
    if db_pool is None:
        try:
            db_pool = await aiomysql.create_pool(
                host=host, port=3306,
                user=user, password=password,
                db=db, autocommit=True, # Autocommit is generally fine for simple transactions
                loop=loop,
                min_size=1, # Minimum number of connections in the pool
                max_size=10 # Maximum number of connections in the pool
            )
            logger.info("Database connection pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize database connection pool: {e}")
            raise # Re-raise to prevent the application from starting without a DB connection

# --- Texas Hold'em Hand Evaluation Logic (Simplified Placeholder) ---
def get_rank_value(rank):
    """Returns numerical value for poker ranks for comparison."""
    if rank.isdigit():
        if rank == '0': return 10 # '0' typically represents 'T' for Ten
        return int(rank)
    elif rank == 'J': return 11
    elif rank == 'Q': return 12
    elif rank == 'K': return 13
    elif rank == 'A': return 14
    return 0 # Should not happen for valid ranks

def evaluate_poker_hand(cards):
    """
    Evaluates a 7-card poker hand (5 community + 2 hole) and returns its type and value.
    This is a very simplified placeholder and DOES NOT correctly implement full poker rules.
    For a real poker game, this function would be significantly more complex and
    optimized for performance, potentially using pre-computed lookup tables or
    specialized algorithms.
    """
    if len(cards) < 5:
        return "Not enough cards", 0

    # Example: Check for a flush (all cards of the same suit)
    suit_counts = {}
    for card in cards:
        suit_counts[card.suit] = suit_counts.get(card.suit, 0) + 1
    
    for suit, count in suit_counts.items():
        if count >= 5:
            # Simple flush detection, not full poker logic
            return "Flush", 500 + len(cards) # Placeholder value

    # Example: Check for pairs (very basic)
    rank_counts = {}
    for card in cards:
        rank_counts[card.rank] = rank_counts.get(card.rank, 0) + 1
    
    pairs = 0
    for rank, count in rank_counts.items():
        if count == 2:
            pairs += 1
    
    if pairs >= 2:
        return "Two Pair", 200 # Placeholder value
    elif pairs == 1:
        return "One Pair", 100 # Placeholder value

    # Sort cards by rank for other evaluations (e.g., straight, high card)
    processed_cards = []
    for card in cards:
        processed_cards.append((get_rank_value(card.rank), card.suit[0].upper()))
    processed_cards.sort(key=lambda x: x[0], reverse=True)

    return "High Card", processed_cards[0][0] # Very basic, just returns highest card value


class GameBackend:
    def __init__(self):
        # Database connection details should ideally come from environment variables
        self.db_config = {
            'host': 'localhost',
            'user': 'root',
            'password': '',
            'db': 'serenekeks'
        }
        self.loop = asyncio.get_event_loop() # Get the current event loop

    async def initialize(self):
        """Initializes the database connection pool."""
        await init_db_pool(
            self.db_config['host'],
            self.db_config['user'],
            self.db_config['password'],
            self.db_config['db'],
            self.loop
        )

    async def _execute_query(self, query, params=None, fetchone=False, fetchall=False):
        """Helper to execute database queries using the connection pool."""
        if db_pool is None:
            logger.error("Database pool not initialized.")
            return None

        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor: # Use DictCursor for easier access
                try:
                    await cursor.execute(query, params)
                    if fetchone:
                        return await cursor.fetchone()
                    if fetchall:
                        return await cursor.fetchall()
                    return None # For INSERT, UPDATE, DELETE
                except Exception as e:
                    logger.error(f"Database query failed: {query} with params {params}. Error: {e}")
                    raise # Re-raise to be handled by the calling function

    async def _load_game_state(self, room_id: str) -> dict:
        """
        Loads the game state for a given room_id from the database or cache.
        If not in cache or expired, fetches from DB and updates cache.
        """
        async with cache_lock:
            cached_entry = game_state_cache.get(room_id)
            if cached_entry and (time.time() - cached_entry['timestamp']) < CACHE_TTL_SECONDS:
                logger.debug(f"[_load_game_state] Cache hit for room {room_id}")
                return cached_entry['game_state']

        logger.info(f"[_load_game_state] Cache miss or expired for room {room_id}. Loading from DB.")
        query = "SELECT game_state_json FROM game_rooms WHERE room_id = %s"
        result = await self._execute_query(query, (room_id,), fetchone=True)

        if result and result['game_state_json']:
            game_state = json.loads(result['game_state_json'])
            async with cache_lock:
                game_state_cache[room_id] = {'game_state': game_state, 'timestamp': time.time()}
            return game_state
        
        logger.warning(f"No game state found for room_id: {room_id}. Returning default.")
        # Return a default empty state if not found, to prevent errors
        return {
            "room_id": room_id,
            "current_round": "pre_game",
            "players": [],
            "dealer_hand": [],
            "deck": [],
            "board_cards": [],
            "last_evaluation": None,
            "current_player_turn_index": -1,
            "current_betting_round_pot": 0,
            "current_round_min_bet": 0,
            "timer_end_time": None,
            "game_started_once": False,
            "small_blind_amount": 5, # Default small blind
            "big_blind_amount": 10,  # Default big blind
            "dealer_button_index": -1 # Index of the player with the dealer button
        }

    async def _save_game_state(self, game_state: dict):
        """
        Saves the current game state to the database and updates the cache.
        """
        room_id = game_state['room_id']
        game_state_json = json.dumps(game_state)
        
        query = """
            INSERT INTO game_rooms (room_id, game_state_json)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE game_state_json = %s
        """
        await self._execute_query(query, (room_id, game_state_json, game_state_json))
        logger.info(f"Game state for room {room_id} saved to DB.")

        async with cache_lock:
            game_state_cache[room_id] = {'game_state': game_state, 'timestamp': time.time()}
            logger.debug(f"Game state for room {room_id} updated in cache.")

    async def _broadcast_game_state(self, room_id: str, websocket_manager):
        """Broadcasts the current game state to all clients in the room."""
        game_state = await self._load_game_state(room_id)
        message = json.dumps({"game_state": game_state})
        await websocket_manager.broadcast_to_room(room_id, message)
        logger.info(f"Game state for room {room_id} broadcasted.")

    async def _get_next_active_player_index(self, game_state: dict, current_index: int) -> int:
        """
        Finds the index of the next active player in the sorted player list.
        An active player is one who is not folded and not all-in (unless all-in and it's their turn to act).
        This function iterates through players starting from the next player after `current_index`.
        """
        players = game_state.get('players', [])
        num_players = len(players)
        if num_players == 0:
            return -1

        # Start search from the player immediately after the current player
        start_search_index = (current_index + 1) % num_players if current_index != -1 else 0

        for i in range(num_players):
            player_index = (start_search_index + i) % num_players
            player = players[player_index]
            # A player is 'active' if they are not folded and not all-in (unless all-in and it's their turn to act)
            # For simplicity, we'll just check for 'folded' here.
            # More complex logic for all-in players would be needed for full poker rules.
            if not player.get('folded', False):
                return player_index
        return -1 # No active players found

    async def _start_betting_round(self, game_state: dict):
        """Initializes a new betting round."""
        game_state['current_betting_round_pot'] = 0 # Reset pot for the round
        game_state['current_round_min_bet'] = 0 # Reset min bet for the round

        # Reset player states for the new betting round
        for player in game_state['players']:
            player['current_bet_in_round'] = 0
            player['has_acted_in_round'] = False
            # Do NOT reset 'folded' status here. Folded players remain folded for the hand.

        # Determine who acts first in the new round
        if game_state['current_round'] == 'pre_flop':
            # Pre-flop: Player after Big Blind acts first
            big_blind_index = (game_state['dealer_button_index'] + 2) % len(game_state['players'])
            game_state['current_player_turn_index'] = await self._get_next_active_player_index(game_state, big_blind_index)
            if game_state['current_player_turn_index'] == -1: # Fallback if BB is the only active player left
                 game_state['current_player_turn_index'] = (game_state['dealer_button_index'] + 1) % len(game_state['players'])
        else:
            # Post-flop: First active player after the dealer button acts first
            # Find the first active player starting from the small blind position (left of dealer button)
            start_index = (game_state['dealer_button_index'] + 1) % len(game_state['players'])
            game_state['current_player_turn_index'] = await self._get_next_active_player_index(game_state, start_index - 1) # -1 because _get_next_active_player_index increments first

        # Set a timer for the current player's action
        game_state['timer_end_time'] = time.time() + 60 # 60 seconds for action

        logger.info(f"Started {game_state['current_round']} betting round. Current player turn: {game_state['players'][game_state['current_player_turn_index']]['name'] if game_state['current_player_turn_index'] != -1 else 'None'}")
        await self._save_game_state(game_state)


    async def _apply_blinds(self, game_state: dict):
        """Applies small and big blinds to players."""
        players = game_state['players']
        num_players = len(players)

        if num_players < 2:
            logger.warning("Not enough players to apply blinds.")
            return

        # Determine Small Blind (SB) and Big Blind (BB) positions
        # SB is immediately left of the dealer button
        # BB is immediately left of the small blind
        
        # Ensure dealer_button_index is valid, default to 0 if not set
        if game_state['dealer_button_index'] == -1:
            game_state['dealer_button_index'] = 0 # Start with player 0 as dealer button

        dealer_button_index = game_state['dealer_button_index']
        small_blind_index = (dealer_button_index + 1) % num_players
        big_blind_index = (dealer_button_index + 2) % num_players

        small_blind_amount = game_state['small_blind_amount']
        big_blind_amount = game_state['big_blind_amount']

        # Apply Small Blind
        sb_player = players[small_blind_index]
        bet_amount_sb = min(small_blind_amount, sb_player['total_chips'])
        sb_player['total_chips'] -= bet_amount_sb
        sb_player['current_bet_in_round'] += bet_amount_sb
        game_state['current_betting_round_pot'] += bet_amount_sb
        sb_player['has_acted_in_round'] = True # Blinds count as action
        game_state['current_round_min_bet'] = max(game_state['current_round_min_bet'], bet_amount_sb)
        logger.info(f"Player {sb_player['name']} posts Small Blind of ${bet_amount_sb}.")

        # Apply Big Blind
        bb_player = players[big_blind_index]
        bet_amount_bb = min(big_blind_amount, bb_player['total_chips'])
        bb_player['total_chips'] -= bet_amount_bb
        bb_player['current_bet_in_round'] += bet_amount_bb
        game_state['current_betting_round_pot'] += bet_amount_bb
        bb_player['has_acted_in_round'] = True # Blinds count as action
        game_state['current_round_min_bet'] = max(game_state['current_round_min_bet'], bet_amount_bb)
        logger.info(f"Player {bb_player['name']} posts Big Blind of ${bet_amount_bb}.")

        await self._save_game_state(game_state)


    async def _deal_hole_cards(self, game_state: dict):
        """Deals 2 hole cards to each player."""
        deck = Deck()
        deck.shuffle()
        game_state['deck'] = deck.to_list() # Store the shuffled deck state

        for player in game_state['players']:
            player['hand'] = [deck.deal().code, deck.deal().code]
            player['folded'] = False # Reset folded status for new hand
            player['current_bet_in_round'] = 0 # Reset current bet for new hand
            player['has_acted_in_round'] = False # Reset action status

        game_state['dealer_hand'] = [] # Clear dealer's hand
        game_state['board_cards'] = [] # Clear board cards
        game_state['current_betting_round_pot'] = 0 # Reset pot
        game_state['current_round_min_bet'] = 0 # Reset min bet
        game_state['last_evaluation'] = None # Clear previous evaluation
        
        await self._save_game_state(game_state)

    async def _deal_community_cards(self, game_state: dict, num_cards: int):
        """Deals community cards (flop, turn, or river)."""
        deck = Deck(game_state['deck']) # Recreate deck from current state
        
        # Burn a card before dealing (standard poker practice)
        if len(deck.cards) > 0:
            deck.deal()

        dealt_cards = []
        for _ in range(num_cards):
            if len(deck.cards) > 0:
                dealt_cards.append(deck.deal().code)
            else:
                logger.warning("Deck is empty, cannot deal more community cards.")
                break
        
        game_state['board_cards'].extend(dealt_cards)
        game_state['deck'] = deck.to_list() # Update deck state after dealing
        await self._save_game_state(game_state)

    async def _check_round_completion(self, game_state: dict) -> bool:
        """
        Checks if the current betting round is complete.
        A betting round is complete if:
        1. All active players have acted.
        2. All active players who have acted have matched the current_round_min_bet (or are all-in).
        3. There is more than one active player remaining.
        """
        players = game_state['players']
        active_players = [p for p in players if not p.get('folded', False)]
        
        if len(active_players) <= 1:
            logger.info("Round completion check: 1 or fewer active players remaining, round is complete.")
            return True # Round ends if only one player left (they win the pot)

        all_acted = True
        all_bets_matched = True
        
        current_min_bet = game_state['current_round_min_bet']

        for player in active_players:
            if not player.get('has_acted_in_round', False):
                all_acted = False
                break
            
            # Check if player's current bet matches the round's minimum bet,
            # or if they are all-in and have contributed their maximum.
            # This simplified check assumes 'all-in' status is managed elsewhere.
            if player['current_bet_in_round'] < current_min_bet and player['total_chips'] > 0:
                all_bets_matched = False
                break
        
        if all_acted and all_bets_matched:
            logger.info("Round completion check: All active players have acted and matched bets. Round is complete.")
            return True
        
        logger.debug(f"Round not complete. All acted: {all_acted}, All bets matched: {all_bets_matched}")
        return False

    async def _advance_game_round(self, game_state: dict, websocket_manager):
        """Advances the game to the next round (flop, turn, river, showdown, or new hand)."""
        current_round = game_state['current_round']
        logger.info(f"Advancing game from round: {current_round}")

        if current_round == 'pre_game': # This is the initial state before any betting
            game_state['current_round'] = 'pre_flop'
            await self._deal_hole_cards(game_state)
            await self._apply_blinds(game_state) # Blinds are part of pre-flop setup
            await self._start_betting_round(game_state)
            logger.info("Advanced to Pre-Flop.")
        elif current_round == 'pre_flop':
            game_state['current_round'] = 'flop'
            await self._deal_community_cards(game_state, 3) # Deal 3 cards for the flop
            await self._start_betting_round(game_state)
            logger.info("Advanced to Flop.")
        elif current_round == 'flop':
            game_state['current_round'] = 'turn'
            await self._deal_community_cards(game_state, 1) # Deal 1 card for the turn
            await self._start_betting_round(game_state)
            logger.info("Advanced to Turn.")
        elif current_round == 'turn':
            game_state['current_round'] = 'river'
            await self._deal_community_cards(game_state, 1) # Deal 1 card for the river
            await self._start_betting_round(game_state)
            logger.info("Advanced to River.")
        elif current_round == 'river':
            game_state['current_round'] = 'showdown'
            await self._handle_showdown(game_state)
            logger.info("Advanced to Showdown.")
            # Set a timer for the new game to start automatically after showdown
            game_state['timer_end_time'] = time.time() + 10 # 10 seconds to auto-start new game
        elif current_round == 'showdown':
            # This means the timer for showdown expired or 'Play Again' was clicked
            await self.start_new_round_pre_flop(game_state['room_id'])
            logger.info("Starting new round from Showdown.")
        else:
            logger.warning(f"Unknown or unhandled current_round state: {current_round}")
        
        await self._broadcast_game_state(game_state['room_id'], websocket_manager)


    async def _handle_showdown(self, game_state: dict):
        """Determines the winner(s) at showdown and distributes the pot."""
        players = game_state['players']
        board_cards = [Card.from_code(code) for code in game_state['board_cards']]
        
        best_hand_value = -1
        winners = []

        # Evaluate hands for all active players
        for player in players:
            if not player.get('folded', False):
                player_hole_cards = [Card.from_code(code) for code in player['hand']]
                all_seven_cards = board_cards + player_hole_cards
                hand_type, hand_value = evaluate_poker_hand(all_seven_cards)
                player['last_hand_evaluated_type'] = hand_type
                player['last_hand_evaluated_value'] = hand_value
                logger.info(f"Player {player['name']} hand: {hand_type} (Value: {hand_value})")

                if hand_value > best_hand_value:
                    best_hand_value = hand_value
                    winners = [player['discord_id']]
                elif hand_value == best_hand_value:
                    winners.append(player['discord_id'])
        
        # Distribute pot to winners
        pot_amount = game_state['current_betting_round_pot']
        if winners:
            share_per_winner = pot_amount // len(winners)
            for player_id in winners:
                for player in players:
                    if player['discord_id'] == player_id:
                        player['total_chips'] += share_per_winner
                        logger.info(f"Player {player['name']} wins ${share_per_winner}.")
                        break
        else:
            logger.warning("No winners found in showdown, pot not distributed.")

        game_state['last_evaluation'] = {
            "best_hand_value": best_hand_value,
            "winners": winners,
            "pot_distributed": pot_amount
        }
        game_state['current_betting_round_pot'] = 0 # Pot is now distributed

        # Move dealer button for the next hand
        num_players = len(game_state['players'])
        if num_players > 0:
            game_state['dealer_button_index'] = (game_state['dealer_button_index'] + 1) % num_players
            logger.info(f"Dealer button moved to player at index {game_state['dealer_button_index']}.")

        await self._save_game_state(game_state)


    async def handle_websocket_message(self, room_id: str, sender_id: str, message_data: dict, websocket_manager):
        """
        Main handler for incoming WebSocket messages.
        Dispatches actions based on the 'action' key in message_data.
        """
        action = message_data.get('action')
        logger.info(f"[handle_websocket_message] Received action '{action}' for room {room_id} from {sender_id}")

        game_state = await self._load_game_state(room_id)
        
        # Mark game as started once the 'start_new_round_pre_flop' action is received initially
        if action == 'start_new_round_pre_flop' and not game_state.get('game_started_once', False):
            game_state['game_started_once'] = True
            await self._save_game_state(game_state) # Save this flag immediately

        if action == 'get_state':
            # This is a client requesting the current state, simply broadcast it back.
            await self._broadcast_game_state(room_id, websocket_manager)
            return True, "State requested and sent.", game_state

        elif action == 'add_player':
            player_data = message_data.get('player_data')
            if player_data and player_data['discord_id'] not in [p['discord_id'] for p in game_state['players']]:
                # Initialize new player with starting chips and default status
                player_data['total_chips'] = 1000 # Starting chips
                player_data['hand'] = []
                player_data['folded'] = False
                player_data['current_bet_in_round'] = 0
                player_data['has_acted_in_round'] = False
                game_state['players'].append(player_data)
                await self._save_game_state(game_state)
                await self._broadcast_game_state(room_id, websocket_manager)
                logger.info(f"Player {player_data['name']} added to room {room_id}.")
                return True, "Player added.", game_state
            return False, "Player already in room or invalid data.", game_state

        elif action == 'leave_player':
            discord_id_to_remove = message_data.get('discord_id')
            initial_player_count = len(game_state['players'])
            game_state['players'] = [p for p in game_state['players'] if p['discord_id'] != discord_id_to_remove]
            if len(game_state['players']) < initial_player_count:
                await self._save_game_state(game_state)
                await self._broadcast_game_state(room_id, websocket_manager)
                logger.info(f"Player {discord_id_to_remove} left room {room_id}.")
                return True, "Player removed.", game_state
            return False, "Player not found.", game_state

        elif action == 'start_new_round_pre_flop':
            # This action can be triggered by initiator or by auto-timer after showdown
            initiator_id = message_data.get('initiator_id')
            sender_id = message_data.get('sender_id')

            # Allow initiator to start, or allow auto-start if it's from the timer (sender_id matches initiator_id)
            # For auto-start, the frontend sends the initiator_id as sender_id if it's the initiator's timer
            is_initiator_request = (sender_id == initiator_id)
            
            # Only allow starting a new round if in 'pre_game' or 'showdown' state
            if game_state['current_round'] in ['pre_game', 'showdown']:
                if is_initiator_request or (game_state['current_round'] == 'showdown' and sender_id == MY_PLAYER_ID): # MY_PLAYER_ID is a frontend concept, this should be handled by backend logic (e.g., if sender is the initiator)
                    # Reset game state for a new hand
                    game_state['current_round'] = 'pre_game' # Temporarily set to pre_game to trigger _advance_game_round
                    game_state['game_started_once'] = True # Mark that the game has started at least once
                    game_state['current_betting_round_pot'] = 0
                    game_state['current_round_min_bet'] = 0
                    game_state['timer_end_time'] = None
                    game_state['board_cards'] = []
                    game_state['dealer_hand'] = []
                    game_state['last_evaluation'] = None
                    
                    # Reset player specific states for a new hand
                    for player in game_state['players']:
                        player['hand'] = []
                        player['folded'] = False
                        player['current_bet_in_round'] = 0
                        player['has_acted_in_round'] = False

                    # Advance to pre_flop which will deal cards and apply blinds
                    await self._advance_game_round(game_state, websocket_manager)
                    logger.info(f"New round started for room {room_id}.")
                    return True, "New round started, hole cards and dealer cards dealt, moved to pre_game.", game_state
                else:
                    return False, "Only the game initiator can start a new round.", game_state
            else:
                return False, f"Cannot start new round from current state: {game_state['current_round']}.", game_state

        elif action == 'player_action':
            player_id = message_data.get('player_id')
            action_type = message_data.get('action_type') # 'call', 'bet', 'fold'
            amount = message_data.get('amount', 0) # Only for 'bet'

            # Find the player in the game state
            player_index = -1
            for i, p in enumerate(game_state['players']):
                if p['discord_id'] == player_id:
                    player_index = i
                    break

            if player_index == -1:
                return False, "Player not found.", game_state

            current_player = game_state['players'][player_index]

            # Basic turn validation
            if game_state['current_player_turn_index'] != player_index:
                logger.warning(f"Player {current_player['name']} tried to act out of turn.")
                return False, "It's not your turn.", game_state
            
            if current_player.get('folded', False):
                logger.warning(f"Folded player {current_player['name']} tried to act.")
                return False, "You have already folded.", game_state

            # Handle player actions
            success = False
            message = "Invalid action."

            if action_type == 'fold':
                current_player['folded'] = True
                current_player['has_acted_in_round'] = True
                success = True
                message = f"{current_player['name']} folded."
                logger.info(message)
            elif action_type == 'call':
                amount_to_call = game_state['current_round_min_bet'] - current_player['current_bet_in_round']
                if amount_to_call > current_player['total_chips']:
                    # Player goes all-in if they don't have enough to call
                    bet_amount = current_player['total_chips']
                    current_player['total_chips'] = 0
                    current_player['current_bet_in_round'] += bet_amount
                    game_state['current_betting_round_pot'] += bet_amount
                    current_player['has_acted_in_round'] = True
                    success = True
                    message = f"{current_player['name']} went all-in for ${bet_amount}."
                    logger.info(message)
                else:
                    current_player['total_chips'] -= amount_to_call
                    current_player['current_bet_in_round'] += amount_to_call
                    game_state['current_betting_round_pot'] += amount_to_call
                    current_player['has_acted_in_round'] = True
                    success = True
                    message = f"{current_player['name']} called ${amount_to_call}."
                    logger.info(message)
            elif action_type == 'bet':
                # For simplicity, a 'bet' action implies a raise if there's already a min_bet
                # Validate amount
                if amount <= 0 or amount > current_player['total_chips']:
                    return False, "Invalid bet amount.", game_state
                
                # If it's a bet/raise, it must be at least the current min bet or a valid raise amount
                # Simplified: assume 'amount' is the total bet for the round
                if amount < game_state['current_round_min_bet']:
                    return False, f"Bet must be at least the current minimum bet of ${game_state['current_round_min_bet']}.", game_state

                bet_difference = amount - current_player['current_bet_in_round']
                if bet_difference > current_player['total_chips']:
                    # Player goes all-in if they don't have enough to cover the full bet
                    bet_difference = current_player['total_chips']
                    current_player['total_chips'] = 0
                    current_player['current_bet_in_round'] += bet_difference
                    game_state['current_betting_round_pot'] += bet_difference
                    game_state['current_round_min_bet'] = max(game_state['current_round_min_bet'], current_player['current_bet_in_round'])
                    current_player['has_acted_in_round'] = True
                    success = True
                    message = f"{current_player['name']} went all-in for ${bet_difference}."
                    logger.info(message)
                else:
                    current_player['total_chips'] -= bet_difference
                    current_player['current_bet_in_round'] += bet_difference
                    game_state['current_betting_round_pot'] += bet_difference
                    game_state['current_round_min_bet'] = max(game_state['current_round_min_bet'], current_player['current_bet_in_round'])
                    current_player['has_acted_in_round'] = True
                    success = True
                    message = f"{current_player['name']} bet/raised to ${current_player['current_bet_in_round']}."
                    logger.info(message)

                # When a player bets/raises, all other players who have already acted must act again
                for p in game_state['players']:
                    if p['discord_id'] != player_id and not p.get('folded', False):
                        p['has_acted_in_round'] = False # They need to re-act

            if success:
                await self._save_game_state(game_state)
                
                # After action, check if the round is complete
                if await self._check_round_completion(game_state):
                    logger.info(f"Betting round {game_state['current_round']} completed. Advancing round.")
                    game_state['current_player_turn_index'] = -1 # No one's turn during round transition
                    await self._save_game_state(game_state) # Save state before advancing
                    await self._advance_game_round(game_state, websocket_manager)
                else:
                    # Advance turn to the next active player
                    game_state['current_player_turn_index'] = await self._get_next_active_player_index(game_state, player_index)
                    if game_state['current_player_turn_index'] == -1:
                        logger.warning("No next active player found, but round not complete. This indicates a logic error or all players folded/all-in.")
                        # Force round completion if no one else can act
                        await self._advance_game_round(game_state, websocket_manager)
                    else:
                        # Set timer for the next player's action
                        game_state['timer_end_time'] = time.time() + 60 # 60 seconds for action
                        await self._save_game_state(game_state)
                        await self._broadcast_game_state(room_id, websocket_manager)
                        logger.info(f"Turn advanced to {game_state['players'][game_state['current_player_turn_index']]['name']}.")
                return True, message, game_state
            return False, message, game_state

        elif action == 'auto_action_timeout':
            player_id = message_data.get('player_id')
            # Only process if it's actually their turn and timer has expired
            if game_state['current_player_turn_index'] != -1 and \
               game_state['players'][game_state['current_player_turn_index']]['discord_id'] == player_id and \
               game_state['timer_end_time'] and time.time() >= game_state['timer_end_time']:
                
                # Automatically fold the player
                current_player = game_state['players'][game_state['current_player_turn_index']]
                current_player['folded'] = True
                current_player['has_acted_in_round'] = True
                logger.info(f"Player {current_player['name']} auto-folded due to timeout.")
                await self._save_game_state(game_state)

                if await self._check_round_completion(game_state):
                    logger.info(f"Betting round {game_state['current_round']} completed after auto-fold. Advancing round.")
                    game_state['current_player_turn_index'] = -1
                    await self._save_game_state(game_state)
                    await self._advance_game_round(game_state, websocket_manager)
                else:
                    game_state['current_player_turn_index'] = await self._get_next_active_player_index(game_state, game_state['current_player_turn_index'])
                    if game_state['current_player_turn_index'] == -1: # Should not happen if round not complete, but as a safeguard
                        await self._advance_game_round(game_state, websocket_manager)
                    else:
                        game_state['timer_end_time'] = time.time() + 60 # Reset timer for next player
                        await self._save_game_state(game_state)
                        await self._broadcast_game_state(room_id, websocket_manager)
                        logger.info(f"Turn advanced to {game_state['players'][game_state['current_player_turn_index']]['name']} after auto-fold.")
                return True, "Player auto-folded due to timeout.", game_state
            return False, "Auto-action timeout not applicable.", game_state

        elif action == 'send_message':
            message_content = message_data.get('message_content')
            if message_content:
                # Load game state to get sender's name (optional but good for context)
                sender_name = "Unknown Player"
                for player in game_state.get('players', []):
                    if player['discord_id'] == sender_id:
                        sender_name = player['name']
                        break

                response_data = {
                    "status": "success",
                    "message": "Message received and echoed.",
                    "echo_message": {
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "content": message_content
                    },
                    "game_state": game_state # Include the current game state
                }
                # Broadcast the message along with the current game state
                message = json.dumps(response_data)
                await websocket_manager.broadcast_to_room(room_id, message)
                logger.info(f"Message from {sender_id} in room {room_id} echoed and game state broadcasted.")
                return True, "Message processed.", game_state
            return False, "Message content is empty.", game_state

        return False, "Unknown action.", game_state

# The setup function for your bot (assuming discord.py or similar structure)
# This part would typically be in your main bot file.
# Example usage:
# from your_game_backend_file import GameBackend
# game_backend = GameBackend()
# await game_backend.initialize() # Call this during bot startup

# In your websocket handler:
# async def websocket_endpoint(websocket, path):
#     room_id = extract_room_id_from_path(path)
#     sender_id = extract_sender_id_from_auth(websocket) # Or from message payload
#     websocket_manager.add_client(room_id, sender_id, websocket)
#     try:
#         async for message in websocket:
#             message_data = json.loads(message)
#             await game_backend.handle_websocket_message(room_id, sender_id, message_data, websocket_manager)
#     except websockets.exceptions.ConnectionClosedOK:
#         logger.info(f"Client {sender_id} disconnected from room {room_id}.")
#     finally:
#         websocket_manager.remove_client(room_id, sender_id)
