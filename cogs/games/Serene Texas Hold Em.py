# cogs/games/Serene_Texas_Hold_Em.py

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


async def _get_or_create_game_room_link(guild_id: str, channel_id: str, button_custom_id: str, initiator_id: str, initiator_display_name: str, joiner_id: str = None, joiner_display_name: str = None, existing_room_name: str = None) -> tuple[str, str]:
    """
    Checks if a game room exists for the given button_custom_id.
    If it exists, returns the link to that room and its name.
    If not, creates a new game room entry in the database and returns the link and its name.
    The joiner_id and display names are optional and added to the URL if provided.
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")
    GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php") # Use environment variable

    conn = None
    room_id = button_custom_id
    room_name = existing_room_name # Use existing name if provided (for subsequent clicks)

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
            await cur.execute("SELECT room_id, room_name FROM bot_game_rooms WHERE room_id = %s", (button_custom_id,))
            result = await cur.fetchone()

            if result:
                logger.info(f"Existing game room found for room_id: {button_custom_id}")
                room_id = result['room_id']
                room_name = result['room_name'] # Retrieve existing room name
            else:
                # Room does not exist, create a new one
                game_names = [
                    "Heavay Burtations",
                    "Taris Tazens",
                    "Daris Darrisons",
                    "Behead the Pep"
                ]
                chosen_base_name = random.choice(game_names)
                
                unique_suffix = str(uuid.uuid4())[:8]
                room_name = f"{chosen_base_name} - {unique_suffix}" # Generate new room name
                room_type = "Texas Hold 'Em" # This is the display name
                player_count = 0

                # Initialize a new deck for the game state
                new_deck_obj = Deck()
                new_deck_obj.build()
                new_deck_obj.shuffle()
                initial_deck_output = new_deck_obj.to_output_format()

                # Define the initial game state JSON
                initial_game_state = {
                    'room_id': room_id,
                    'game_type': "1", # Set game_type to string "1" as requested
                    'current_round': 'pre_game',
                    'players': [], # No players yet, they will join via the web frontend
                    'deck': initial_deck_output,
                    'board_cards': [],
                    'last_evaluation': None
                }
                initial_game_state_json = json.dumps(initial_game_state)

                # Insert initial game_state
                # MODIFIED: Corrected column name from 'game_statelongtextutf8mb4_bin' to 'game_state'
                await cur.execute(
                    "INSERT INTO bot_game_rooms (room_name, room_type, guild_id, channel_id, room_id, player_count, game_state) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (room_name, room_type, str(guild_id), str(channel_id), button_custom_id, player_count, initial_game_state_json)
                )
                room_id = button_custom_id
                logger.info(f"New game room created: {room_name} with room_id: {room_id} and initial game state.")

            query_params = {
                'room_id': room_id,
                'room_name': room_name, # Added room name to query parameters
                'guild_id': str(guild_id),
                'channel_id': str(channel_id),
                'initiator_id': str(initiator_id),
                'initiator_display_name': initiator_display_name
            }
            if joiner_id:
                query_params['joiner_id'] = str(joiner_id)
            if joiner_display_name:
                query_params['joiner_display_name'] = joiner_display_name

            game_url = f"{GAME_WEB_URL}?{urllib.parse.urlencode(query_params)}"
            return game_url, room_name # Return both URL and room_name

    except Exception as e:
        logger.error(f"Database error in _get_or_create_game_room_link: {e}", exc_info=True)
        return "Error generating game link.", None # Return None for room_name on error
    finally:
        if conn:
            conn.close()


class PlayGameButton(Button):
    def __init__(self, room_id: str, guild_id: str, channel_id: str, initiator_id: str, initiator_display_name: str, room_name: str):
        super().__init__(label="Play Texas Hold 'Em Online", style=discord.ButtonStyle.primary)
        self.room_id = room_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.initiator_id = initiator_id
        self.initiator_display_name = initiator_display_name
        self.room_name = room_name # Store the room name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        joiner_id = interaction.user.id
        joiner_display_name = interaction.user.display_name

        # Pass the stored room_name to the function
        game_url, _ = await _get_or_create_game_room_link(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            button_custom_id=self.room_id,
            initiator_id=self.initiator_id,
            initiator_display_name=self.initiator_display_name,
            joiner_id=joiner_id,
            joiner_display_name=joiner_display_name,
            existing_room_name=self.room_name # Pass the existing room name
        )

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
        logger.info(f"Personalized game link sent to {interaction.user.display_name}: {game_url}")


async def start(interaction: discord.Interaction, bot):
    """
    This function is the entry point for the card game.
    It now generates a unique game room ID and sends a Discord button.
    When the button is clicked, a personalized game link is generated and sent ephemerally.
    """
    await interaction.response.send_message("Creating your Texas Hold 'Em game session...", ephemeral=False)
    await asyncio.sleep(1)

    game_session_room_id = str(uuid.uuid4())

    # Get the initial game URL and room name. This will create the room in DB if it doesn't exist.
    initial_game_url, generated_room_name = await _get_or_create_game_room_link(
        guild_id=str(interaction.guild_id),
        channel_id=str(interaction.channel_id),
        button_custom_id=game_session_room_id,
        initiator_id=str(interaction.user.id),
        initiator_display_name=interaction.user.display_name
    )

    if "Error" in initial_game_url:
        await interaction.followup.send(initial_game_url, ephemeral=True)
        return

    initial_game_button = PlayGameButton(
        room_id=game_session_room_id,
        guild_id=str(interaction.guild_id),
        channel_id=str(interaction.channel_id),
        initiator_id=str(interaction.user.id),
        initiator_display_name=interaction.user.display_name,
        room_name=generated_room_name # Pass the generated room name to the button
    )

    initial_view = View()
    initial_view.add_item(initial_game_button)

    await interaction.followup.send(
        f"A Texas Hold 'Em game session for '{generated_room_name}' is ready! Click the button below to get your personalized game link.",
        view=initial_view,
        ephemeral=False
    )
    logger.info(f"Initial game session button sent for room ID: {game_session_room_id}, room name: {generated_room_name}")


class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Starts a new Texas Hold 'Em game session."""
        # Call the standalone function to initiate the game session
        await start(ctx.interaction, self.bot) # Pass interaction and bot instance

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
