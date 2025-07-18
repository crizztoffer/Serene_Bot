# --- cogs/game_main.py ---

import discord
from discord.ext import commands
from discord import app_commands

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.serene_group = self.bot.tree.get_command("serene")

        if self.serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        self.game_group = app_commands.Group(name="game", description="Game commands")

        # Register commands to game_group
        @self.game_group.command(name="start", description="Start a game")
        @app_commands.describe(game_name="Game name", max_players="Max players")
        async def start(interaction, game_name: str, max_players: int = 4):
            if max_players < 2:
                await interaction.response.send_message("Need at least 2 players.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Game '{game_name}' started with {max_players} players.")

        @self.game_group.command(name="join", description="Join a game")
        @app_commands.describe(game_name="Game name")
        async def join(interaction, game_name: str):
            await interaction.response.send_message(f"{interaction.user.display_name} joined '{game_name}'.")

        @self.game_group.command(name="end", description="End a game")
        @app_commands.describe(game_name="Game name")
        async def end(interaction, game_name: str):
            await interaction.response.send_message(f"Game '{game_name}' ended.")

        self.serene_group.add_command(self.game_group)

        # Optional info command directly under /serene
        @app_commands.command(name="info", description="Bot info")
        async def info(interaction):
            await interaction.response.send_message("Serene Bot — Game Manager!")

        self.serene_group.add_command(info)

async def setup(bot):
    await bot.add_cog(GameCommands(bot))
