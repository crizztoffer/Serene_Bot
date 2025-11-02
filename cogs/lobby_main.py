# --- cogs/lobby_main.py ---

import discord
from discord.ext import commands
from discord import app_commands

class LobbyCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # Fetch the pre-created /serene group from bot.py
        serene = self.bot.tree.get_command("serene")
        if serene is None:
            # If /serene isn't present, fail like game_main does
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # /serene lobby  (one command, no options)
        @app_commands.command(
            name="lobby",
            description="Open the Serene Keks Games Lobby",
        )
        async def serene_lobby(interaction: discord.Interaction):
            # Import and reuse the moved lobby flow
            from .lobby.lobby import start  # cogs/lobby/lobby.py
            await start(interaction, self.bot)

        # Attach the command under /serene
        serene.add_command(serene_lobby)

async def setup(bot):
    await bot.add_cog(LobbyCommands(bot))
