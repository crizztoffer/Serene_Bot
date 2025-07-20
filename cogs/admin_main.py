# --- cogs/admin_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util
import logging

logger = logging.getLogger(__name__)

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        serene_group = self.bot.tree.get_command("serene")

        if serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # Load each admin command file and attach its `command` to /serene
        admin_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        for file in os.listdir(admin_path):
            if file.endswith(".py") and file != "__init__.py":
                module_name = file[:-3]
                module_path = os.path.join(admin_path, file)

                try:
                    spec = importlib.util.spec_from_file_location(module_name, module_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, "command"):
                        serene_group.add_command(module.command)
                        logger.info(f"Loaded admin command: {module_name}")
                    else:
                        logger.warning(f"[!] {file} does not define a 'command' object.")
                except Exception as e:
                    logger.error(f"Failed to load admin command '{file}': {e}")

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
