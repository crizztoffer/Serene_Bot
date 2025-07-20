# --- cogs/admin_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        @app_commands.command(
            name="admin",
            description="Run an admin tool"
        )
        @app_commands.checks.has_permissions(administrator=True)  # ✅ Restrict to admins only
        @app_commands.describe(tool="Select an admin tool")
        @app_commands.autocomplete(tool=self.autocomplete_admin_tools)
        async def admin(interaction: discord.Interaction, tool: str):
            try:
                module_path = os.path.join(os.path.dirname(__file__), "admin_commands", f"{tool}.py")
                spec = importlib.util.spec_from_file_location(tool, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(interaction, self.bot)
                else:
                    await interaction.response.send_message(
                        f"Admin tool '{tool}' does not define a `start()` function.",
                        ephemeral=True
                    )
            except FileNotFoundError:
                await interaction.response.send_message(f"Tool '{tool}' not found.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to load tool '{tool}': {e}", ephemeral=True)

        self.serene_group.add_command(admin)

    async def autocomplete_admin_tools(self, interaction: discord.Interaction, current: str):
        tools_path = os.path.join(os.path.dirname(__file__), "admin_commands")
        if not os.path.exists(tools_path):
            return []
        files = [f[:-3] for f in os.listdir(tools_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
