# cogs/game_main.py
# This file contains the implementation for the /serene game subcommand group.

import discord
from discord.ext import commands
from discord import app_commands # Crucial import for slash commands

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Define the main command group for /serene
    # This creates the top-level slash command /serene
    # This group will be added to the bot's tree in the setup function.
    serene_group = app_commands.Group(name="serene", description="The main Serene bot commands.")

    # Define a subcommand group under /serene: /serene game
    # This creates the /serene game subcommand group, nested under 'serene_group'
    game_group = app_commands.Group(parent=serene_group, name="game", description="Commands related to game management.")

    # --- Commands under /serene game ---

    @game_group.command(name="start", description="Starts a new game.")
    @app_commands.describe(
        game_name="The name of the game to start (e.g., Tic-Tac-Toe, Blackjack)",
        max_players="Maximum number of players (optional, default 4)"
    )
    async def game_start(self, interaction: discord.Interaction, game_name: str, max_players: int = 4):
        """
        Starts a new game with a specified name and optional max players.
        Usage: /serene game start <game_name> [max_players]
        """
        try:
            if max_players < 2:
                await interaction.response.send_message("A game needs at least 2 players!", ephemeral=True)
                return

            await interaction.response.send_message(f"Game '{game_name}' started! Maximum players: {max_players}.")
            # Here you would add logic to actually start a game session based on game_name
            # For example:
            # if game_name.lower() == "tic-tac-toe":
            #     # Call your Tic-Tac-Toe game logic here
            #     pass
            # elif game_name.lower() == "blackjack":
            #     # Call your Blackjack game logic here
            #     pass
            # else:
            #     await interaction.followup.send(f"Game type '{game_name}' is not yet implemented.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene game start: {e}")

    @game_group.command(name="join", description="Joins an existing game.")
    @app_commands.describe(
        game_name="The name of the game to join"
    )
    async def game_join(self, interaction: discord.Interaction, game_name: str):
        """
        Allows a user to join an existing game.
        Usage: /serene game join <game_name>
        """
        try:
            await interaction.response.send_message(f"{interaction.user.display_name} has joined game '{game_name}'.")
            # Here you would add logic to add the user to the game session
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene game join: {e}")

    @game_group.command(name="end", description="Ends an existing game.")
    @app_commands.describe(
        game_name="The name of the game to end"
    )
    # You can add permission checks here, e.g., @app_commands.default_permissions(manage_guild=True)
    async def game_end(self, interaction: discord.Interaction, game_name: str):
        """
        Ends a specified game. (Requires admin/host permissions)
        Usage: /serene game end <game_name>
        """
        try:
            # You'd typically add permission checks here, e.g., checking if the user is the game host
            await interaction.response.send_message(f"Game '{game_name}' has been ended.")
            # Logic to terminate the game session
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene game end: {e}")

    # You can add other commands directly under /serene if needed, e.g.:
    @serene_group.command(name="info", description="Displays information about the bot.")
    async def serene_info(self, interaction: discord.Interaction):
        """
        Displays general information about the bot.
        Usage: /serene info
        """
        try:
            await interaction.response.send_message("This is Serene Bot, your friendly game manager!")
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene info: {e}")

# This setup function is crucial for discord.py to load the cog.
async def setup(bot):
    # Add the main command group to the bot's command tree.
    # All subcommands and subcommand groups defined under serene_group
    # will be automatically registered when serene_group is added.
    bot.tree.add_command(GameCommands.serene_group)
    await bot.add_cog(GameCommands(bot))

