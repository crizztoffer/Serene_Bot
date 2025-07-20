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
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        @app_commands.command(name="admin", description="Perform admin tasks")
        @app_commands.describe(task_name="Choose a task to run")
        @app_commands.autocomplete(task_name=self.autocomplete_tasks)
        @app_commands.checks.has_permissions(administrator=True)  # Restrict to admins only
        async def task(interaction: Interaction, task_name: str):
            try:
                module_path = os.path.join(os.path.dirname(__file__), "admin_commands", f"{task_name}.py")
                spec = importlib.util.spec_from_file_location(task_name, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(self.serene_group, self.bot, interaction)
                else:
                    await interaction.response.send_message(f"Task '{task_name}' does not have a start() function.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to load task '{task_name}': {e}", ephemeral=True)

        @task.error
        async def task_error(interaction: Interaction, error):
            if isinstance(error, app_commands.errors.MissingPermissions):
                await interaction.response.send_message("You must be an admin to use this command.", ephemeral=True)
            else:
                logger.error(f"Error in admin task command: {error}")
                await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)

        self.serene_group.add_command(task)

    async def autocomplete_tasks(self, interaction: discord.Interaction, current: str):
        task_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        if not os.path.exists(task_path):
            return []
        files = [f[:-3] for f in os.listdir(task_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
