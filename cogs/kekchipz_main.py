# --- cogs/kekchipz_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util
import logging

logger = logging.getLogger(__name__)

class KekchipzCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        serene_group = self.bot.tree.get_command("serene")
        logger.info(f"In kekchipz_main.py cog_load: serene_group = {serene_group}")

        if serene_group is None:
            raise RuntimeError("/serene group not found")

        @app_commands.command(name="kekchipz", description="Kekchipz commands")
        @app_commands.describe(kekchipz_name="View, request, or give kekchipz")
        @app_commands.autocomplete(kekchipz_name=self.autocomplete_kekchipz)
        async def kekchipz(interaction: discord.Interaction, kekchipz_name: str):
            logger.info(f"Kekchipz command triggered with name: {kekchipz_name}")
            try:
                module_path = os.path.join(os.path.dirname(__file__), "kekchipz", f"{kekchipz_name}.py")
                spec = importlib.util.spec_from_file_location(kekchipz_name, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(interaction, self.bot)
                else:
                    await interaction.response.send_message(
                        f"Kekchipz file '{kekchipz_name}' does not have a start() function.",
                        ephemeral=True
                    )
            except Exception as e:
                await interaction.response.send_message(
                    f"Failed to load kekchipz file '{kekchipz_name}': {e}",
                    ephemeral=True
                )

        serene_group.add_command(kekchipz)
        logger.info("✅ Registered /serene kekchipz command.")

    async def autocomplete_kekchipz(self, interaction: discord.Interaction, current: str):
        kekchipz_path = os.path.join(os.path.dirname(__file__), "kekchipz")
        if not os.path.exists(kekchipz_path):
            return []
        files = [f[:-3] for f in os.listdir(kekchipz_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(KekchipzCommands(bot))
