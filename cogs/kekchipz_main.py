import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class KekchipzCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="kekchipz", description="Kekchipz commands")
    @app_commands.describe(kekchipz_name="View, request, or give kekchipz")
    # Reference the static method directly
    @app_commands.autocomplete(kekchipz_name=autocomplete_kekchipz)
    async def kekchipz_command(self, interaction: discord.Interaction, kekchipz_name: str):
        try:
            # Construct the module path relative to the current file (kekchipz_main.py)
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

    @staticmethod # <--- ADDED THIS DECORATOR
    async def autocomplete_kekchipz(interaction: discord.Interaction, current: str):
        # __file__ refers to the current module (kekchipz_main.py)
        kekchipz_path = os.path.join(os.path.dirname(__file__), "kekchipz")
        if not os.path.exists(kekchipz_path):
            return []
        files = [f[:-3] for f in os.listdir(kekchipz_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    cog = KekchipzCommands(bot)
    await bot.add_cog(cog)

    # After adding the cog, get the serene group and add the command to it
    serene_group = bot.tree.get_command("serene")
    if serene_group:
        # Add the command method to the serene group
        serene_group.add_command(cog.kekchipz_command)
    else:
        print("WARNING: /serene group not found. Kekchipz command might not be registered correctly.")
    
    # It's crucial to sync the command tree after adding/modifying commands
    await bot.tree.sync()
