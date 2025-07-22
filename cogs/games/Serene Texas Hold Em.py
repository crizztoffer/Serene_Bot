# --- cogs/games/Serene Texas Hold Em.py ---

import discord
from discord.ext import commands
import asyncio
import random
import io # Still needed for potential image handling if _create_public_board_image was to be reused elsewhere, but not for this current scope. Keeping for now.
import os # For environment variables like API keys
import urllib.parse # For URL encoding
import json # For parsing JSON data
# Removed itertools and collections as they were for poker hand evaluation
import aiohttp # Still needed for _fetch_image_bytes if it's kept, but it's now unused. Can be removed if image functions are fully scrapped.
import aiomysql # Import aiomysql for database operations
import logging # Import logging
import time # Import time for Unix timestamps
import uuid # For generating unique IDs

# Explicitly import UI components for clarity
from discord.ui import View, Button

# Removed PIL imports as image generation is scrapped
# from PIL import Image, ImageDraw, ImageFont # Pillow library for image manipulation


# Configure logging for this game module
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - '%(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# --- Removed all card generation and image generation functions ---
# _create_standard_deck()
# _get_card_image_url()
# _fetch_image_bytes()
# _create_combined_card_image()
# _create_public_board_image()
# _deal_cards_simplified()


async def _get_or_create_game_room_link(guild_id: str, channel_id: str, button_custom_id: str) -> str:
    """
    Checks if a game room exists for the given button_custom_id.
    If it exists, returns the link to that room.
    If not, creates a new game room entry in the database and returns the link.
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")
    GAME_WEB_URL = "https://serenekeks.com/game_room.php" # Updated to the provided URL

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
        async with conn.cursor(aiomysql.DictCursor) as cur: # Use DictCursor to get results as dictionaries
            # Check if room_id already exists
            await cur.execute("SELECT room_id FROM bot_game_rooms WHERE room_id = %s", (button_custom_id,))
            result = await cur.fetchone()

            if result:
                logger.info(f"Existing game room found for room_id: {button_custom_id}")
                room_id = result['room_id']
            else:
                # Room does not exist, create a new one
                game_names = [
                    "Heavay Burtations",
                    "Taris Tazens",
                    "Daris Darrisons",
                    "Behead the Pep"
                ]
                chosen_base_name = random.choice(game_names)
                
                # Generate a unique suffix for the room_name
                unique_suffix = str(uuid.uuid4())[:8] # Use first 8 chars of UUID for brevity
                room_name = f"{chosen_base_name} - {unique_suffix}"
                room_type = "Texas Hold 'Em"
                player_count = 0 # Initial player count

                await cur.execute(
                    "INSERT INTO bot_game_rooms (room_name, room_type, guild_id, channel_id, room_id, player_count) VALUES (%s, %s, %s, %s, %s, %s)",
                    (room_name, room_type, str(guild_id), str(channel_id), button_custom_id, player_count)
                )
                room_id = button_custom_id
                logger.info(f"New game room created: {room_name} with room_id: {room_id}")

            # Construct the game URL
            # Using urllib.parse.urlencode for query parameters
            query_params = urllib.parse.urlencode({
                'room_id': room_id,
                'guild_id': str(guild_id),
                'channel_id': str(channel_id)
            })
            game_url = f"{GAME_WEB_URL}?{query_params}"
            return game_url

    except Exception as e:
        logger.error(f"Database error in _get_or_create_game_room_link: {e}")
        return "Error generating game link."
    finally:
        if conn:
            conn.close()


async def start(interaction: discord.Interaction, bot):
    """
    This function is the entry point for the card game.
    It now only generates a unique game room link and sends it via a Discord button.
    All game logic (card dealing, image generation, interactions) will be handled by the web application.
    """
    await interaction.response.send_message("Generating your Texas Hold 'Em game room link...", ephemeral=False)
    await asyncio.sleep(1)

    # Generate a unique custom ID for this button/game room
    button_custom_id = str(uuid.uuid4())

    # Get or create the game room link from the database
    game_url = await _get_or_create_game_room_link(
        guild_id=str(interaction.guild_id),
        channel_id=str(interaction.channel_id),
        button_custom_id=button_custom_id
    )

    if "Error" in game_url:
        await interaction.followup.send(game_url, ephemeral=True)
        return

    # Create a View with a button that links to the game URL
    game_link_view = View()
    game_link_button = Button(label="Play Texas Hold 'Em Online", style=discord.ButtonStyle.link, url=game_url)
    game_link_view.add_item(game_link_button)

    # Send the button to the user
    await interaction.followup.send(
        "Click the button below to join your Texas Hold 'Em game!",
        view=game_link_view,
        ephemeral=False # Make it visible to everyone in the channel
    )
    logger.info(f"Game link button sent with URL: {game_url}")
