# --- cogs/admin_main.py ---

import discord
from discord.ext import commands
from discord import app_commands, Interaction
import os
import importlib.util
import logging

logger = logging.getLogger(__name__)

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Find the /serene group and attach a new /admin subgroup
        self.serene_group = self.bot.tree.get_command("serene")
        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # Create admin group under /serene
        self.admin_group = app_commands.Group(name="admin", description="Admin tools")
        self.serene_group.add_command(self.admin_group)

        # Dynamically load admin_commands/*.py modules
        admin_cmds_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        for filename in os.listdir(admin_cmds_path):
            if filename.endswith(".py") and filename != "__init__.py":
                task_name = filename[:-3]
                try:
                    full_path = os.path.join(admin_cmds_path, filename)
                    spec = importlib.util.spec_from_file_location(task_name, full_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, "start"):
                        await module.start(self.admin_group, self.bot)
                        logger.info(f"Loaded admin task: {task_name}")
                    else:
                        logger.warning(f"Task '{task_name}' has no start() method.")
                except Exception as e:
                    logger.error(f"Failed to load task '{task_name}': {e}", exc_info=True)

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
