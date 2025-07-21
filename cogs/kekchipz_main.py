import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class KekchipzCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Define the kekchipz command as a method of the class
    # Corrected name from "kakchipz" to "kekchipz"
    @app_commands.command(name="kekchipz", description="Kekchipz commands")
    @app_commands.describe(kekchipz_name="View, request, or give kekchipz")
    @app_commands.autocomplete(kekchipz_name=autocomplete_kekchipz) # Autocomplete refers to the method
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

    async def autocomplete_kekchipz(self, interaction: discord.Interaction, current: str):
        kekchipz_path = os.path.join(os.path.dirname(__file__), "kekchipz")
        if not os.path.exists(kekchipz_path):
            return []
        files = [f[:-3] for f in os.listdir(kekchipz_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    cog = KekchipzCommands(bot)
    await bot.add_cog(cog)

    # After adding the cog, get the serene group and add the command to it
    # This needs to be done after the cog is added, and before syncing the tree.
    serene_group = bot.tree.get_command("serene")
    if serene_group:
        # Add the command method to the serene group
        serene_group.add_command(cog.kekchipz_command)
    else:
        print("WARNING: /serene group not found. Kekchipz command might not be registered correctly.")
    
    # It's crucial to sync the command tree after adding/modifying commands
    # This is typically done once after all cogs are loaded, or when commands are changed.
    # If your bot has a global sync, this might not be needed here, but it's good practice
    # to ensure commands are updated.
    await bot.tree.sync()
