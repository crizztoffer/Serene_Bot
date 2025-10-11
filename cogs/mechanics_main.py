import logging
import json
import aiomysql
from discord.ext import commands
from itertools import combinations

# Import Card from the game_models utility file
from cogs.utils.game_models import Card

logger = logging.getLogger(__name__)

# --- Texas Hold'em Hand Evaluation & Scoring Utilities ---
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

def get_rank_value(rank):
    """Returns a numerical value for poker ranks for comparison."""
    if rank.isdigit():
        if rank == '0': return 10
        return int(rank)
    elif rank == 'J': return 11
    elif rank == 'Q': return 12
    elif rank == 'K': return 13
    elif rank == 'A': return 14
    return 0

def evaluate_poker_hand(cards):
    """
    Evaluates the best possible 5-card poker hand from a given list of Card objects.
    Returns a tuple: (hand_name: str, score_vector: tuple[int])
    The score_vector is used to compare hands of the same type for tie-breaking.
    """
    def rank_value(card):
        return get_rank_value(card.rank)

    def is_straight(ranks):
        unique_ranks = sorted(list(set(ranks)), reverse=True)
        # Check for ace-low straight (A, 2, 3, 4, 5)
        if set([14, 2, 3, 4, 5]).issubset(set(unique_ranks)):
            return True, 5
        for i in range(len(unique_ranks) - 4):
            window = unique_ranks[i:i + 5]
            if all(window[j] - window[j+1] == 1 for j in range(4)):
                return True, window[0]
        return False, None

    def classify_hand(hand):
        ranks = sorted([rank_value(c) for c in hand], reverse=True)
        suits = [c.suit[0].upper() for c in hand]

        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        count_groups = sorted(rank_counts.items(), key=lambda x: (-x[1], -x[0]))
        grouped_ranks = [r for r, _ in count_groups]

        is_flush = len(set(suits)) == 1
        straight, high_straight = is_straight(ranks)

        if is_flush and straight:
            if high_straight == 14:
                return "Royal Flush", (HAND_RANKINGS["Royal Flush"],)
            return "Straight Flush", (HAND_RANKINGS["Straight Flush"], high_straight)

        if count_groups[0][1] == 4:
            return "Four of a Kind", (HAND_RANKINGS["Four of a Kind"], count_groups[0][0], grouped_ranks[1])

        if count_groups[0][1] == 3 and count_groups[1][1] >= 2:
            return "Full House", (HAND_RANKINGS["Full House"], count_groups[0][0], count_groups[1][0])

        if is_flush:
            return "Flush", (HAND_RANKINGS["Flush"], *ranks[:5])

        if straight:
            return "Straight", (HAND_RANKINGS["Straight"], high_straight)

        if count_groups[0][1] == 3:
            return "Three of a Kind", (HAND_RANKINGS["Three of a Kind"], count_groups[0][0], *grouped_ranks[1:3])

        if count_groups[0][1] == 2 and count_groups[1][1] == 2:
            return "Two Pair", (HAND_RANKINGS["Two Pair"], count_groups[0][0], count_groups[1][0], grouped_ranks[2])

        if count_groups[0][1] == 2:
            return "One Pair", (HAND_RANKINGS["One Pair"], count_groups[0][0], *grouped_ranks[1:4])

        return "High Card", (HAND_RANKINGS["High Card"], *ranks[:5])

    best_score = (-1,)
    best_hand_name = ""
    # A poker hand is 5 cards. Evaluate all 5-card combinations from the input cards.
    for combo in combinations(cards, 5):
        hand_name, score = classify_hand(combo)
        if score > best_score:
            best_score = score
            best_hand_name = hand_name

    return best_hand_name, best_score


class MechanicsMain(commands.Cog, name="MechanicsMain"):
    """
    A cog for managing player connections in game rooms and providing poker hand
    evaluation utilities. This cog does not manage active game flow.
    """
    def __init__(self, bot):
        self.bot = bot
        logger.info("MechanicsMain (Hand Evaluation & Room Manager) initialized.")
    
        # Database credentials assigned from the main bot object.
        self.db_user = self.bot.db_user
        self.db_password = self.bot.db_password
        self.db_host = self.bot.db_host
        self.db_name = "serene_users"

    async def cog_load(self):
        logger.info("MechanicsMain cog loaded successfully.")

    async def cog_unload(self):
        logger.info("MechanicsMain cog unloaded.")

    async def _get_db_connection(self):
        """Helper to establish a connection to the database."""
        if not all([self.db_user, self.db_password, self.db_host, self.db_name]):
            logger.error("Database credentials are not configured in the bot.")
            raise ConnectionError("Database credentials not configured.")
        return await aiomysql.connect(
            host=self.db_host, user=self.db_user, password=self.db_password,
            db=self.db_name, charset='utf8mb4', autocommit=False,
            cursorclass=aiomysql.cursors.DictCursor
        )

    async def _load_room_state(self, room_id: str) -> dict:
        """
        Loads the game state for a room. If not found, initializes a new state.
        The new state only contains player connection info, not active game data.
        """
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT game_state FROM bot_game_rooms WHERE room_id = %s", (room_id,)
                )
                result = await cursor.fetchone()
                if result and result.get('game_state'):
                    return json.loads(result['game_state'])
                else:
                    logger.info(f"[_load_room_state] No state found for room '{room_id}'. Initializing fresh state.")
                    return {'room_id': room_id, 'players': {}}
        except Exception as e:
            logger.error(f"Error loading room state for '{room_id}': {e}", exc_info=True)
            return {'room_id': room_id, 'players': {}} # Return default on error
        finally:
            if conn:
                conn.close()

    async def _save_room_state(self, room_id: str, game_state: dict):
        """Saves the room state, creating the room if it doesn't exist."""
        conn = None
        try:
            conn = await self._get_db_connection()
            async with conn.cursor() as cursor:
                game_state_json = json.dumps(game_state)
                # Use INSERT ... ON DUPLICATE KEY UPDATE for a single, robust query.
                await cursor.execute(
                    """
                    INSERT INTO bot_game_rooms (room_id, game_state)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE game_state = VALUES(game_state)
                    """,
                    (room_id, game_state_json)
                )
            await conn.commit()
            logger.info(f"[_save_room_state] Successfully saved state for room '{room_id}'.")
        except Exception as e:
            logger.error(f"Error saving room state for '{room_id}': {e}", exc_info=True)
            if conn:
                await conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    async def player_connect(self, room_id: str, discord_id: str) -> tuple[bool, str]:
        """
        Adds a player to a game room's persisted state.
        This marks the player as present but stores no game-specific data.
        """
        game_state = await self._load_room_state(room_id)
        players = game_state.get('players', {})

        if discord_id in players:
            logger.warning(f"[player_connect] Player {discord_id} is already in room {room_id}.")
            return False, "Player already connected to this room."

        players[discord_id] = {}  # Store an empty object as a placeholder
        game_state['players'] = players
        
        await self._save_room_state(room_id, game_state)
        logger.info(f"[player_connect] Player {discord_id} connected to room {room_id}.")
        return True, "Player connected successfully."

    async def player_disconnect(self, room_id: str, discord_id: str) -> tuple[bool, str]:
        """Removes a player from a game room's persisted state."""
        game_state = await self._load_room_state(room_id)
        players = game_state.get('players', {})

        if discord_id not in players:
            logger.warning(f"[player_disconnect] Player {discord_id} not found in room {room_id}.")
            return False, "Player not found in this room."

        del players[discord_id]
        game_state['players'] = players

        await self._save_room_state(room_id, game_state)
        logger.info(f"[player_disconnect] Player {discord_id} disconnected from room {room_id}.")
        return True, "Player disconnected successfully."

# The setup function needed for bot.py to load this cog.
async def setup(bot):
    try:
        await bot.add_cog(MechanicsMain(bot))
    except Exception as e:
        logging.error(f"An error occurred during the setup of MechanicsMain cog: {e}", exc_info=True)

