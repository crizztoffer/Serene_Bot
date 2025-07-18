# cogs/game_main.py
# This file defines subcommands for the /serene command group.

import discord
from discord.ext import commands
from discord import app_commands

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # CORRECTED: Retrieve the parent serene_group directly from the bot's command tree
        # This assumes serene_group has already been added to bot.tree in bot.py
        self.serene_group_parent = self.bot.tree.get_command('serene')
        
        if self.serene_group_parent is None:
            print("Error: /serene command group not found in bot.tree. Game subcommands may not register.")
            # It's crucial to raise an error or handle this gracefully if the parent group isn't found
            # as subcommands cannot be added without it.
            raise commands.ExtensionFailed(self.qualified_name, "Parent /serene command group not found.")


        # Define a subcommand group under the *retrieved* /serene group: /serene game
        self.game_group = app_commands.Group(parent=self.serene_group_parent, name="game", description="Commands related to game management.")

        # Add the game_group as a subcommand to the parent serene_group.
        # This makes /serene game available.
        # Note: We are adding this *subgroup* to the parent group, not directly to bot.tree.
        self.serene_group_parent.add_command(self.game_group)

    # --- Commands under /serene game ---

    @app_commands.command(name="start", description="Starts a new game.")
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

    @app_commands.command(name="join", description="Joins an existing game.")
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

    @app_commands.command(name="end", description="Ends an existing game.")
    @app_commands.describe(
        game_name="The name of the game to end"
    )
    async def game_end(self, interaction: discord.Interaction, game_name: str):
        """
        Ends a specified game. (Requires admin/host permissions)
        Usage: /serene game end <game_name>
        """
        try:
            await interaction.response.send_message(f"Game '{game_name}' has been ended.")
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene game end: {e}")

    # You can add other commands directly under /serene if needed, e.g.:
    @app_commands.command(name="info", description="Displays information about the bot.")
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
# It no longer accepts 'extras' and directly initializes the cog.
async def setup(bot):
    # Initialize the cog. The cog will retrieve the serene_group itself.
    await bot.add_cog(GameCommands(bot))
