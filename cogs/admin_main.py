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

        # Define the /serene admin group
        # This group will hold subcommands like /serene admin flag, /serene admin ban, etc.
        # We store it as an instance variable so other modules (like flag.py) can access it.
        self.admin_group = app_commands.Group(
            name="admin",
            description="Admin commands for Serene bot."
        )
        self.serene_group.add_command(self.admin_group)
        logger.info("'/serene admin' command group initialized.")

    async def cog_load(self):
        """
        This method is called when the cog is loaded.
        It dynamically loads all admin command files from the 'admin_commands' directory
        and calls their 'start' function to register them as subcommands of '/serene admin'.
        """
        tools_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        if not os.path.exists(tools_path):
            os.makedirs(tools_path) # Ensure the directory exists
            logger.warning(f"Admin commands directory not found, created: {tools_path}")
            return

        for filename in os.listdir(tools_path):
            if filename.endswith(".py") and filename != "__init__.py":
                tool_name = filename[:-3]
                try:
                    module_path = os.path.join(tools_path, filename)
                    spec = importlib.util.spec_from_file_location(tool_name, module_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, "start"):
                        # Pass the admin_group to the start function so it can add its command
                        await module.start(self.admin_group, self.bot)
                        logger.info(f"Loaded admin tool: {tool_name}")
                    else:
                        logger.warning(f"Admin tool '{tool_name}' does not define a `start()` function.")
                except Exception as e:
                    logger.error(f"Failed to load admin tool '{tool_name}': {e}")

    async def cog_unload(self):
        """Cleans up commands if needed when the cog is unloaded."""
        # Discord.py automatically handles unregistering commands when a cog is unloaded.
        logger.info("AdminCommands cog unloaded.")


async def setup(bot):
    """Sets up the AdminCommands cog."""
    await bot.add_cog(AdminCommands(bot))
