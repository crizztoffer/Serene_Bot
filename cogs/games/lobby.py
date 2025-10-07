# cogs/games/lobby.py

import discord
from discord.ext import commands
import os
import urllib.parse
import logging
import uuid

from discord.ui import View, Button

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def _get_lobby_link() -> str:
    """
    Returns the lobby URL. No query params are sent; the lobby handles session creation.
    """
    # Keep default to your existing URL; change if your lobby moved.
    GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")
    return GAME_WEB_URL  # No params

class PlayGameButton(Button):
    def __init__(self):
        # custom_id is useful if you register persistent views; keep it unique
        super().__init__(
            label="Play Texas Hold 'Em Online",
            style=discord.ButtonStyle.primary,
            custom_id=f"play_game_{uuid.uuid4()}"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        game_url = _get_lobby_link()

        ephemeral_link_view = View()
        ephemeral_link_button = Button(
            label="Click to Join Lobby",
            style=discord.ButtonStyle.link,
            url=game_url
        )
        ephemeral_link_view.add_item(ephemeral_link_button)

        await interaction.followup.send(
            f"👋 {interaction.user.mention}, here’s your lobby link:",
            view=ephemeral_link_view,
            ephemeral=True
        )

async def start(interaction: discord.Interaction, bot):
    """
    Posts a normal (non-ephemeral) Play button in the channel.
    Clicking it sends the user an ephemeral link to the lobby.
    """
    # If you prefer an immediate visible message reply:
    # await interaction.response.send_message(...)
    await interaction.response.defer()

    play_button_view = View(timeout=None)  # keep button alive; note: persistence across restarts needs view registration
    play_button_view.add_item(PlayGameButton())

    await interaction.followup.send(
        "A Texas Hold 'Em lobby is ready! Click below to open the game lobby.",
        view=play_button_view
    )

class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Posts a Play button that links to the lobby."""
        # If this is a prefix command and ctx.interaction is None, fall back to sending via ctx.send.
        if getattr(ctx, "interaction", None):
            await start(ctx.interaction, self.bot)
        else:
            # Fallback path for non-slash usage
            view = View(timeout=None)
            view.add_item(PlayGameButton())
            await ctx.send(
                "A Texas Hold 'Em lobby is ready! Click below to open the game lobby.",
                view=view
            )

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
