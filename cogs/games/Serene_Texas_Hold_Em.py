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

from discord.ui import View, Button, Select

# Import the Deck class from mechanics_main.py
from cogs.mechanics_main import Deck


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


async def _create_game_room(guild_id: str, channel_id: str, room_id: str, initiator_id: str, game_mode: str) -> tuple[str, str]:
    """
    Creates a new game room entry in the database with the specified settings.
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

async def _get_game_room_link(room_id: str, guild: discord.Guild, joiner_id: str, joiner_display_name: str) -> str:
    """
    Fetches room details and constructs a personalized game link for a joiner.
    """
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST")
    GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT room_name, guild_id, channel_id, game_mode, initiator FROM bot_game_rooms WHERE room_id = %s", (room_id,))
            result = await cur.fetchone()

            if not result:
                return "Error: Game room not found."

            initiator_id = result['initiator']
            initiator_display_name = "Unknown"
            try:
                initiator_member = await guild.fetch_member(int(initiator_id))
                initiator_display_name = initiator_member.display_name
            except Exception as e:
                logger.warning(f"Could not fetch initiator member {initiator_id} from guild {guild.id}: {e}")

            query_params = {
                'room_id': room_id,
                'room_name': result['room_name'],
                'guild_id': result['guild_id'],
                'channel_id': result['channel_id'],
                'initiator_id': result['initiator'],
                'initiator_display_name': initiator_display_name,
                'joiner_id': str(joiner_id),
                'joiner_display_name': joiner_display_name,
                'game_mode': result['game_mode']
            }
            return f"{GAME_WEB_URL}?{urllib.parse.urlencode(query_params)}"

    except Exception as e:
        logger.error(f"Database error in _get_game_room_link: {e}", exc_info=True)
        return "Error generating game link."
    finally:
        if conn:
            conn.close()


class GameSetupView(View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.game_mode = None
        
        stakes_options = [
            discord.SelectOption(label="Low Stakes ($5, $10, $25 chips)", value="1"),
            discord.SelectOption(label="Medium Stakes ($10, $25, $100 chips)", value="2"),
            discord.SelectOption(label="High Stakes ($25, $100, $250 chips)", value="3"),
            discord.SelectOption(label="Nose-Bleed Stakes ($50, $250, $500 chips)", value="4"),
        ]
        self.add_item(self.StakesSelect(stakes_options))
        self.add_item(self.CreateGameButton())

    class StakesSelect(Select):
        def __init__(self, options):
            super().__init__(placeholder="Choose the stakes for the game...", min_values=1, max_values=1, options=options)
        
        async def callback(self, interaction: discord.Interaction):
            self.view.game_mode = self.values[0]
            
            selected_option = discord.utils.get(self.options, value=self.values[0])
            if selected_option:
                self.placeholder = selected_option.label

            for item in self.view.children:
                if isinstance(item, Button) and item.label == "Create Game":
                    item.disabled = False

            await interaction.response.edit_message(view=self.view)

    class CreateGameButton(Button):
        def __init__(self):
            super().__init__(label="Create Game", style=discord.ButtonStyle.success, disabled=True)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            
            await self.view.interaction.delete_original_response()

            game_session_room_id = str(uuid.uuid4())
            
            room_name, room_id = await _create_game_room(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                room_id=game_session_room_id,
                initiator_id=str(interaction.user.id),
                game_mode=self.view.game_mode
            )

            if not room_name:
                await interaction.followup.send("Failed to create the game room. Please try again.", ephemeral=True)
                return

            # Set timeout=None to make the view persistent, so the button never expires.
            play_button_view = View(timeout=None)
            play_button_view.add_item(PlayGameButton(room_id=room_id))

            await interaction.followup.send(
                f"A Texas Hold 'Em game session for '{room_name}' is ready! Click the button below to get your personalized game link.",
                view=play_button_view
            )

class PlayGameButton(Button):
    def __init__(self, room_id: str):
        # The custom_id is necessary for persistent views to work correctly.
        super().__init__(label="Play Texas Hold 'Em Online", style=discord.ButtonStyle.primary, custom_id=f"play_game_{room_id}")
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        game_url = await _get_game_room_link(
            room_id=self.room_id,
            guild=interaction.guild,
            joiner_id=interaction.user.id,
            joiner_display_name=interaction.user.display_name
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

async def start(interaction: discord.Interaction, bot):
    """
    Sends an ephemeral message to the user to set up the game settings.
    """
    setup_view = GameSetupView(interaction)
    await interaction.response.send_message(
        "Please select the stakes for your Texas Hold 'Em game:",
        view=setup_view,
        ephemeral=True
    )

class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Starts a new Texas Hold 'Em game session."""
        # Using ctx.interaction requires the command to be a slash command.
        # Assuming this is a hybrid command or you're adapting. For prefix commands, 
        # you might need to handle this differently, but let's assume interaction context is available.
        await start(ctx.interaction, self.bot)

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
