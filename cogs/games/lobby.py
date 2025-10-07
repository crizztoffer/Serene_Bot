# cogs/games/lobby.py

import discord
from discord.ext import commands
import asyncio
import random
import io
import os
import urllib.parse
import json
import aiohttp
import aiomysql
import logging
import time
import uuid

from discord.ui import View, Button

# Import the Deck class from mechanics_main.py
from cogs.mechanics_main import Deck


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


async def _create_game_room(guild_id: str, channel_id: str, room_id: str, initiator_id: str, game_mode: str | None = None) -> tuple[str | None, str | None]:
    """
    Creates a new game room entry in the database with the specified settings.
    Note: game_mode remains as a nullable field for DB compatibility, but is no longer used.
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            game_names = ["Heavay Burtations", "Taris Tazens", "Daris Darrisons", "Behead the Pep"]
            chosen_base_name = random.choice(game_names)
            unique_suffix = str(uuid.uuid4())[:8]
            room_name = f"{chosen_base_name} - {unique_suffix}"
            room_type = "Texas Hold 'Em"
            
            new_deck_obj = Deck()
            new_deck_obj.build()
            new_deck_obj.shuffle()
            
            initial_game_state = {
                'room_id': room_id, 'game_type': "1", 'current_round': 'pre_game',
                'players': [], 'deck': new_deck_obj.to_output_format(),
                'board_cards': [], 'last_evaluation': None
            }
            initial_game_state_json = json.dumps(initial_game_state)

            # Keep the game_mode column if it exists in your schema; pass None since stakes are no longer used.
            await cur.execute(
                """
                INSERT INTO bot_game_rooms 
                (initiator, room_name, room_type, guild_id, channel_id, room_id, game_mode, game_state, player_count) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (initiator_id, room_name, room_type, str(guild_id), str(channel_id), room_id, game_mode, initial_game_state_json, 0)
            )
            logger.info(f"New game room created: {room_name} with room_id: {room_id}")
            return room_name, room_id

    except Exception as e:
        logger.error(f"Database error in _create_game_room: {e}", exc_info=True)
        return None, None
    finally:
        if conn:
            conn.close()

async def _get_game_room_link(room_id: str) -> str:
    """
    Returns the lobby URL for the given room_id.
    Only passes room_id to the lobby (stakes and other settings are no longer sent).
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")
    GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")

    conn = None
    try:
        # Optional: validate the room exists
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT room_id FROM bot_game_rooms WHERE room_id = %s",
                (room_id,)
            )
            result = await cur.fetchone()
            if not result:
                return "Error: Game room not found."

        query_params = {'room_id': room_id}
        return f"{GAME_WEB_URL}?{urllib.parse.urlencode(query_params)}"

    except Exception as e:
        logger.error(f"Database error in _get_game_room_link: {e}", exc_info=True)
        return "Error generating game link."
    finally:
        if conn:
            conn.close()


class PlayGameButton(Button):
    def __init__(self, room_id: str):
        # The custom_id is necessary for persistent views to work correctly.
        super().__init__(label="Play Texas Hold 'Em Online", style=discord.ButtonStyle.primary, custom_id=f"play_game_{room_id}")
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        game_url = await _get_game_room_link(room_id=self.room_id)

        if "Error" in game_url:
            await interaction.followup.send(game_url, ephemeral=True)
            return

        ephemeral_link_view = View()
        ephemeral_link_button = Button(label="Click to Join Game", style=discord.ButtonStyle.link, url=game_url)
        ephemeral_link_view.add_item(ephemeral_link_button)

        await interaction.followup.send(
            f"👋 {interaction.user.mention}, here is your personalized game link:",
            view=ephemeral_link_view,
            ephemeral=True
        )

async def start(interaction: discord.Interaction, bot):
    """
    Immediately creates a game room and posts a normal (non-ephemeral) Play button in the channel.
    Clicking that button sends the user an ephemeral link to the lobby.
    """
    await interaction.response.defer()  # in case setup takes a moment

    game_session_room_id = str(uuid.uuid4())

    room_name, room_id = await _create_game_room(
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        room_id=game_session_room_id,
        initiator_id=str(interaction.user.id),
        game_mode=None  # stakes removed
    )

    if not room_name:
        await interaction.followup.send("Failed to create the game room. Please try again.", ephemeral=True)
        return

    # Persistent view so the button doesn't expire
    play_button_view = View(timeout=None)
    play_button_view.add_item(PlayGameButton(room_id=room_id))

    await interaction.followup.send(
        f"A Texas Hold 'Em game session for '{room_name}' is ready! Click the button below to get your personalized lobby link.",
        view=play_button_view
    )

class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Starts a new Texas Hold 'Em game session."""
        # If this command is not a slash/hybrid command, ctx.interaction may be None.
        # Adjust as needed for your bot; this assumes an interaction context is available.
        await start(ctx.interaction, self.bot)

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
