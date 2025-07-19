# --- cogs/game_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        self.game_group = app_commands.Group(name="game", description="Game commands")
        self.serene_group.add_command(self.game_group)

async def setup(bot):
    await bot.add_cog(GameCommands(bot))
