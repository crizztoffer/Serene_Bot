# cogs/communication_main.py

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class TalkCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        serene_group = self.bot.tree.get_command("serene")

        if serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # Dynamically load all talk subcommands
        talk_path = os.path.join(os.path.dirname(__file__), "talking")
        for file in os.listdir(talk_path):
            if file.endswith(".py") and file != "__init__.py":
                command_name = file[:-3]
                module_path = os.path.join(talk_path, file)

                spec = importlib.util.spec_from_file_location(command_name, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "command"):
                    serene_group.add_command(module.command)
                else:
                    print(f"[!] {file} does not define a 'command' object.")

async def setup(bot):
    await bot.add_cog(TalkCommands(bot))
