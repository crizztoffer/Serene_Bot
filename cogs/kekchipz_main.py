# --- cogs/kekchipz_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class KekchipzCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        @app_commands.command(name="kekchipz", description="Kekchipz commands")
        @app_commands.describe(kekchipz_name="View, request, or give kekchipz")
        @app_commands.autocomplete(kekchipz_name=self.autocomplete_kekchipz)
        async def kekchipz(interaction: discord.Interaction, kekchipz_name: str):
            try:
                module_path = os.path.join(os.path.dirname(__file__), "kekchipz", f"{kekchipz_name}.py")
                spec = importlib.util.spec_from_file_location(kekchipz_name, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(interaction, self.bot)
                else:
                    await interaction.response.send_message(f"Kekchipz file '{kekchipz_name}' does not have a start() function.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to load kekchipz file '{kekchipz_name}': {e}", ephemeral=True)

        self.serene_group.add_command(kekchipz)

    async def autocomplete_kekchipz(self, interaction: discord.Interaction, current: str):
        kekchipz_path = os.path.join(os.path.dirname(__file__), "kekchipz")
        if not os.path.exists(kekchipz_path):
            return []
        files = [f[:-3] for f in os.listdir(kekchipz_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(KekchipzCommands(bot))
