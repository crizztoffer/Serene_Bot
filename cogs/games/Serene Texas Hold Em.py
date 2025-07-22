# --- cogs/games/Serene Texas Hold Em.py ---

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


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s') # Corrected this line
handler.setFormatter(formatter)
logger.addHandler(handler)


async def _get_or_create_game_room_link(guild_id: str, channel_id: str, button_custom_id: str, initiator_id: str, joiner_id: str = None) -> str:
    """
    Checks if a game room exists for the given button_custom_id.
    If it exists, returns the link to that room.
    If not, creates a new game room entry in the database and returns the link.
    The joiner_id is optional and added to the URL if provided.
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")
    GAME_WEB_URL = "https://serenekeks.com/game_room.php"

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT room_id FROM bot_game_rooms WHERE room_id = %s", (button_custom_id,))
            result = await cur.fetchone()

            if result:
                logger.info(f"Existing game room found for room_id: {button_custom_id}")
                room_id = result['room_id']
            else:
                game_names = [
                    "Heavay Burtations",
                    "Taris Tazens",
                    "Daris Darrisons",
                    "Behead the Pep"
                ]
                chosen_base_name = random.choice(game_names)
                
                unique_suffix = str(uuid.uuid4())[:8]
                room_name = f"{chosen_base_name} - {unique_suffix}"
                room_type = "Texas Hold 'Em"
                player_count = 0

                await cur.execute(
                    "INSERT INTO bot_game_rooms (room_name, room_type, guild_id, channel_id, room_id, player_count) VALUES (%s, %s, %s, %s, %s, %s)",
                    (room_name, room_type, str(guild_id), str(channel_id), button_custom_id, player_count)
                )
                room_id = button_custom_id
                logger.info(f"New game room created: {room_name} with room_id: {room_id}")

            query_params = {
                'room_id': room_id,
                'guild_id': str(guild_id),
                'channel_id': str(channel_id),
                'initiator_id': str(initiator_id)
            }
            if joiner_id: # Add joiner_id if provided
                query_params['joiner_id'] = str(joiner_id)

            game_url = f"{GAME_WEB_URL}?{urllib.parse.urlencode(query_params)}"
            return game_url

    except Exception as e:
        logger.error(f"Database error in _get_or_create_game_room_link: {e}")
        return "Error generating game link."
    finally:
        if conn:
            conn.close()


class PlayGameButton(Button):
    def __init__(self, room_id: str, guild_id: str, channel_id: str, initiator_id: str):
        super().__init__(label="Play Texas Hold 'Em Online", style=discord.ButtonStyle.primary) # Changed to primary style
        self.room_id = room_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.initiator_id = initiator_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True) # Defer immediately, ephemeral response

        joiner_id = interaction.user.id # Get the ID of the user who clicked the button

        game_url = await _get_or_create_game_room_link(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            button_custom_id=self.room_id,
            initiator_id=self.initiator_id,
            joiner_id=joiner_id # Pass the joiner's ID
        )

        if "Error" in game_url:
            await interaction.followup.send(game_url, ephemeral=True)
            return

        # Create a new view for the ephemeral message with the direct link button
        ephemeral_link_view = View()
        ephemeral_link_button = Button(label="Click to Join Game", style=discord.ButtonStyle.link, url=game_url)
        ephemeral_link_view.add_item(ephemeral_link_button)

        await interaction.followup.send(
            f"👋 {interaction.user.mention}, here is your personalized game link:",
            view=ephemeral_link_view,
            ephemeral=True # Only the clicker sees this message
        )
        logger.info(f"Personalized game link sent to {interaction.user.display_name}: {game_url}")


async def start(interaction: discord.Interaction, bot):
    """
    This function is the entry point for the card game.
    It now generates a unique game room ID and sends a Discord button.
    When the button is clicked, a personalized game link is generated and sent ephemerally.
    """
    await interaction.response.send_message("Creating your Texas Hold 'Em game session...", ephemeral=False)
    await asyncio.sleep(1)

    # Generate a unique custom ID for this game room.
    # This ID will be consistent for all users joining THIS specific game session.
    game_session_room_id = str(uuid.uuid4())

    # Create the initial button that users will click.
    # This button itself doesn't have a dynamic URL, but its callback will.
    initial_game_button = PlayGameButton(
        room_id=game_session_room_id,
        guild_id=str(interaction.guild_id),
        channel_id=str(interaction.channel_id),
        initiator_id=str(interaction.user.id) # The initiator's ID is set once here
    )

    # Create a View to hold the initial button
    initial_view = View()
    initial_view.add_item(initial_game_button)

    # Send the initial message with the button
    await interaction.followup.send(
        "A Texas Hold 'Em game session is ready! Click the button below to get your personalized game link.",
        view=initial_view,
        ephemeral=False # Visible to everyone in the channel
    )
    logger.info(f"Initial game session button sent for room ID: {game_session_room_id}")
