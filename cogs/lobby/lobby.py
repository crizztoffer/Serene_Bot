# cogs/games/lobby.py

import discord
from discord.ext import commands
import os
import logging
import uuid
import urllib.parse

from discord.ui import View, Button

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def _get_lobby_link_with_params(interaction: discord.Interaction) -> str:
    """
    Build the lobby URL with the params required by the PHP page:
    guild_id, channel_id, joiner_id, joiner_display_name.
    """
    base_url = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")
    params = {
        "guild_id": str(interaction.guild_id) if interaction.guild_id is not None else "",
        "channel_id": str(interaction.channel_id) if interaction.channel_id is not None else "",
        "joiner_id": str(interaction.user.id),
        "joiner_display_name": interaction.user.display_name or "",
    }
    return f"{base_url}?{urllib.parse.urlencode(params)}"

class PlayGameButton(Button):
    def __init__(self):
        super().__init__(
            label="Visit Serene Keks Games Lobby",
            style=discord.ButtonStyle.primary,
            custom_id=f"visit_lobby_{uuid.uuid4()}"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        game_url = _get_lobby_link_with_params(interaction)

        # Ephemeral view with link button only (no description text)
        ephemeral_link_view = View()
        ephemeral_link_button = Button(
            label="Join Now",
            style=discord.ButtonStyle.link,
            url=game_url
        )
        ephemeral_link_view.add_item(ephemeral_link_button)

        await interaction.followup.send(view=ephemeral_link_view, ephemeral=True)

async def start(interaction: discord.Interaction, bot):
    """
    Posts a single non-ephemeral button in the channel.
    Clicking it sends the user an ephemeral link button to the lobby (with required params).
    """
    await interaction.response.defer()

    play_button_view = View(timeout=None)
    play_button_view.add_item(PlayGameButton())

    # Send button only — no text/description
    await interaction.followup.send(view=play_button_view)

class SereneTexasHoldEm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="texasholdem")
    async def texas_hold_em_command(self, ctx: commands.Context):
        """Posts a button that links to the Serene Keks Games Lobby (with required params on click)."""
        if getattr(ctx, "interaction", None):
            await start(ctx.interaction, self.bot)
        else:
            view = View(timeout=None)
            view.add_item(PlayGameButton())
            await ctx.send(view=view)

async def setup(bot):
    await bot.add_cog(SereneTexasHoldEm(bot))
