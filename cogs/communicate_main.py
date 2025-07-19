# --- cogs/communication_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class TalkCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        @app_commands.command(name="talk", description="Talk with Serene")
        @app_commands.describe(talk="What kinda talkin' you looking for, kek?")
        @app_commands.autocomplete(talk=self.autocomplete_talking)
        async def talk(interaction: discord.Interaction, talk: str):
            try:
                module_path = os.path.join(os.path.dirname(__file__), "talking", f"{talk}.py")
                spec = importlib.util.spec_from_file_location(talk, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(interaction, self.bot)
                else:
                    await interaction.response.send_message(f"Talk '{talk}' does not have a start() function.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to load talk '{talk}': {e}", ephemeral=True)

        self.serene_group.add_command(talk)

    async def autocomplete_talking(self, interaction: discord.Interaction, current: str):
        talk_path = os.path.join(os.path.dirname(__file__), "talking")
        if not os.path.exists(talk_path):
            return []
        files = [f[:-3] for f in os.listdir(talk_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(TalkCommands(bot))
