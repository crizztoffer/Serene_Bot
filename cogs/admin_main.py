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

        # Define admin group as an app_commands.Group (to hold subcommands)
        self.admin_group = app_commands.Group(name="admin", description="Perform admin tasks")

        # Add the admin group under serene group
        self.serene_group.add_command(self.admin_group)

        @app_commands.command(name="admin", description="Perform admin tasks")
        @app_commands.describe(task_name="Choose a task to run")
        @app_commands.autocomplete(task_name=self.autocomplete_tasks)
        @app_commands.checks.has_permissions(administrator=True)
        async def admin_command(interaction: Interaction, task_name: str):
            # This can be an optional catch-all or removed if subcommands are added dynamically
            await interaction.response.send_message(f"Use /serene admin <task>", ephemeral=True)

        # Add this catch-all to admin group
        self.admin_group.add_command(admin_command)

    async def autocomplete_tasks(self, interaction: discord.Interaction, current: str):
        task_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        if not os.path.exists(task_path):
            return []
        files = [f[:-3] for f in os.listdir(task_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

    async def load_task(self, task_name):
        module_path = os.path.join(os.path.dirname(__file__), "admin_commands", f"{task_name}.py")
        if not os.path.exists(module_path):
            logger.error(f"Task file not found: {module_path}")
            return
        spec = importlib.util.spec_from_file_location(task_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if hasattr(module, "start"):
            await module.start(self.admin_group, self.bot)
            logger.info(f"Loaded task '{task_name}'")
        else:
            logger.error(f"Task '{task_name}' has no start function")

async def setup(bot):
    admin_cog = AdminCommands(bot)
    await bot.add_cog(admin_cog)

    # Load all tasks once cog is loaded to register subcommands under /serene admin
    task_path = os.path.join(os.path.dirname(__file__), "admin_commands")
    if os.path.exists(task_path):
        for filename in os.listdir(task_path):
            if filename.endswith(".py") and filename != "__init__.py":
                task_name = filename[:-3]
                await admin_cog.load_task(task_name)
