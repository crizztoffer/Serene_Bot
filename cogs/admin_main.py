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

        @app_commands.command(name="admin", description="Admin commands for Serene bot")
        async def admin_root(interaction: discord.Interaction):
            await interaction.response.send_message("Use a subcommand under `/serene admin`.", ephemeral=True)

        self.admin_group = admin_root

        # Dynamically load admin tools
        self.load_admin_commands()

        # Add admin group under /serene
        self.serene_group.add_command(self.admin_group)

        logger.info("'/serene admin' group and subcommands loaded.")

    def load_admin_commands(self):
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
                        # Inject subcommand into admin group
                        module.start(self.admin_group, self.bot)
                        logger.info(f"Loaded admin subcommand: {module_name}")
                    else:
                        logger.warning(f"{filename} does not define a 'start()' function.")
                except Exception as e:
                    logger.error(f"Failed to load admin subcommand '{module_name}': {e}", exc_info=True)

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
