# cogs/games/lobby.py

import discord
from discord.ext import commands
import os
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
    Returns the main Serene Keks Games Lobby URL.
    """
    return os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")

class PlayGameButton(Button):
    def __init__(self):
        super().__init__(
            label="Visit Serene Keks Games Lobby",
            style=discord.ButtonStyle.primary,
            custom_id=f"visit_lobby_{uuid.uuid4()}"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        game_url = _get_lobby_link()

        # Ephemeral view with link button only
        ephemeral_link_view = View()
        ephemeral_link_button = Button(
            label="Join Now",
            style=discord.ButtonStyle.link,
            url=game_url
        )
        ephemeral_link_view.add_item(ephemeral_link_button)

        # Send ephemeral link only — no extra text
        await interaction.followup.send(view=ephemeral_link_view, ephemeral=True)

async def start(interaction: discord.Interaction, bot):
    """
    Posts a single non-ephemeral button linking users to the lobby.
    """
    await interaction.response.defer()

    play_button_view = View(timeout=None)
    play_button_view.add_item(PlayGameButton())

    # Send button only — no text
    await interaction.followup.send(view=play_button_view)

class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Posts a button that links to the Serene Keks Games Lobby."""
        if getattr(ctx, "interaction", None):
            await start(ctx.interaction, self.bot)
        else:
            view = View(timeout=None)
            view.add_item(PlayGameButton())
            await ctx.send(view=view)

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
