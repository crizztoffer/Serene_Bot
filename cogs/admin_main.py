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
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # Define /serene admin group (container for flag/view/etc.)
        self.admin_group = app_commands.Group(
            name="admin",
            description="Admin commands for Serene bot."
        )
        self.serene_group.add_command(self.admin_group)
        logger.info("'/serene admin' command group initialized.")

    async def cog_load(self):
        """
        Called automatically when cog is loaded.
        Loads all admin subcommands from the 'admin_commands' directory.
        """
        tools_path = os.path.join(os.path.dirname(__file__), "admin_commands")

        for filename in os.listdir(tools_path):
            if filename.endswith(".py") and filename != "__init__.py":
                module_name = filename[:-3]
                module_path = os.path.join(tools_path, filename)

                try:
                    spec = importlib.util.spec_from_file_location(module_name, module_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, "start"):
                        await module.start(self.admin_group, self.bot)
                        logger.info(f"Loaded admin command: {module_name}")
                    else:
                        logger.warning(f"{filename} does not define a 'start()' function.")
                except Exception as e:
                    logger.error(f"Failed to load admin command '{module_name}': {e}", exc_info=True)

    async def cog_unload(self):
        logger.info("AdminCommands cog unloaded.")

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
