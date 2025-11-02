# --- cogs/game_main.py ---

import discord
from discord.ext import commands
from discord import app_commands
import os
import importlib.util

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # Get the pre-created /serene group
        self.serene_group = self.bot.tree.get_command("serene")
        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # ---------------- NEW NESTED PATH: /serene keks game lobby ----------------
        # Create /serene keks
        self.keks_group = app_commands.Group(
            name="keks",
            description="Serene Keks commands"
        )
        # Attach under /serene
        self.serene_group.add_command(self.keks_group)

        # Create /serene keks game
        self.keks_game_group = app_commands.Group(
            name="game",
            description="Keks game actions"
        )
        # Attach under /serene keks
        self.keks_group.add_command(self.keks_game_group)

        # Create /serene keks game lobby (no autocomplete; just runs lobby.start)
        @self.keks_game_group.command(
            name="lobby",
            description="Open the Serene Keks Games Lobby"
        )
        async def keks_game_lobby(interaction: discord.Interaction):
            from .games import lobby as lobby_module  # cogs/games/lobby.py
            await lobby_module.start(interaction, self.bot)

        # ---------------- EXISTING: /serene game <game_name> ----------------
        @app_commands.command(name="game", description="Start a game")
        @app_commands.describe(game_name="Choose a game to play")
        @app_commands.autocomplete(game_name=self.autocomplete_games)
        async def game(interaction: discord.Interaction, game_name: str):
            try:
                module_path = os.path.join(os.path.dirname(__file__), "games", f"{game_name}.py")
                spec = importlib.util.spec_from_file_location(game_name, module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "start"):
                    await module.start(interaction, self.bot)
                else:
                    await interaction.response.send_message(
                        f"Game '{game_name}' does not have a start() function.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(
                    f"Failed to load game '{game_name}': {e}", ephemeral=True)

        # Attach the existing /serene game under /serene (unchanged)
        self.serene_group.add_command(game)

    async def autocomplete_games(self, interaction: discord.Interaction, current: str):
        games_path = os.path.join(os.path.dirname(__file__), "games")
        if not os.path.exists(games_path):
            return []
        files = [f[:-3] for f in os.listdir(games_path) if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=f, value=f) for f in files if current.lower() in f.lower()]

async def setup(bot):
    await bot.add_cog(GameCommands(bot))
