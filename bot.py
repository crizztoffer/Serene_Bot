# bot.py
# This is your main bot file, updated to handle slash commands.

import discord
from discord.ext import commands
from discord import app_commands # Import app_commands for slash commands
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Define your bot's prefix (still useful for some legacy commands or debugging)
BOT_PREFIX = "!"

# Initialize the bot with intents.
# For slash commands, message_content intent is generally not required
# unless you also plan to process regular text messages.
intents = discord.Intents.default()
# intents.message_content = True # Uncomment if you also need to read regular message content

# Initialize the bot. We also initialize the CommandTree for slash commands.
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# --- Event Handlers ---

@bot.event
async def on_ready():
    """
    This event fires when the bot successfully connects to Discord.
    It's crucial for syncing slash commands.
    """
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('Bot is ready!')

    # Sync slash commands to Discord.
    # This should typically be done once after your bot starts and commands are loaded.
    # For development, you might sync every time. For production, sync only when commands change.
    try:
        synced = await bot.tree.sync() # Sync global commands
        # If you want to sync to a specific guild for faster testing, use:
        # guild_id = YOUR_GUILD_ID # Replace with your test guild ID
        # guild = discord.Object(id=guild_id)
        # synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

    # You can set the bot's activity here if you want
    # await bot.change_presence(activity=discord.Game(name="with slash commands!"))


@bot.event
async def on_command_error(ctx, error):
    """
    This event fires when a prefix command encounters an error.
    For slash command errors, you'd typically handle them within the slash command's try/except block.
    """
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Sorry, that prefix command doesn't exist.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: {error.param.name}. Please check the command usage.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the necessary permissions to run this prefix command.")
    else:
        print(f"An error occurred with a prefix command: {error}")
        await ctx.send(f"An unexpected error occurred with a prefix command: {error}")


# --- Cog Loading ---

async def load_cogs():
    """
    Loads all cogs (command files) from the 'cogs' directory.
    """
    cogs_dir = "cogs"
    if not os.path.exists(cogs_dir):
        print(f"Warning: '{cogs_dir}' directory not found. No cogs will be loaded.")
        os.makedirs(cogs_dir) # Create the directory if it doesn't exist for future use

    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py"):
            module_name = f"{cogs_dir}.{filename[:-3]}"
            try:
                await bot.load_extension(module_name)
                print(f"Successfully loaded cog: {module_name}")
            except Exception as e:
                print(f"Failed to load cog {module_name}: {e}")

# --- Main Execution ---

async def main():
    """
    Main function to load cogs and run the bot.
    """
    if TOKEN is None:
        print("Error: DISCORD_BOT_TOKEN not found in environment variables.")
        print("Please create a .env file in the same directory as bot.py with:")
        print("DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN_HERE")
        return

    await load_cogs()
    await bot.start(TOKEN)

# Run the main function
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

```python
# cogs/serene_slash_commands.py
# This file contains the implementation for /serene and its subcommands.

import discord
from discord.ext import commands
from discord import app_commands # Crucial import for slash commands

class SereneSlashCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Define the main command group for /serene
    # This creates the top-level slash command /serene
    serene_group = app_commands.Group(name="serene", description="The main Serene bot commands.")

    # Define a subcommand group under /serene: /serene game
    # This creates the /serene game subcommand group
    game_group = app_commands.Group(parent=serene_group, name="game", description="Commands related to game management.")

    # --- Commands under /serene game ---

    @game_group.command(name="start", description="Starts a new game.")
    @app_commands.describe(
        game_name="The name of the game to start",
        max_players="Maximum number of players (default 4)"
    )
    async def game_start(self, interaction: discord.Interaction, game_name: str, max_players: int = 4):
        """
        Starts a new game with a specified name and optional max players.
        Usage: /serene game start <game_name> [max_players]
        """
        try:
            if max_players < 2:
                await interaction.response.send_message("A game needs at least 2 players!", ephemeral=True) # ephemeral makes message visible only to user
                return

            await interaction.response.send_message(f"Game '{game_name}' started! Maximum players: {max_players}.")
            # Here you would add logic to actually start a game session
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

    # You can add more commands directly under /serene that are NOT game-related, e.g.:
    @serene_group.command(name="info", description="Displays information about the bot.")
    async def serene_info(self, interaction: discord.Interaction):
        """
        Displays general information about the bot.
        Usage: /serene info
        """
        try:
            await interaction.response.send_message("This is Serene Bot, your friendly game manager (slash edition)!")
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error in /serene info: {e}")


# This setup function is crucial for discord.py to load the cog.
async def setup(bot):
    # Add the main command group to the bot's command tree
    bot.tree.add_command(SereneSlashCommands.serene_group)
    await bot.add_cog(SereneSlashCommands(bot))
```text
# .env
# Create this file in the same directory as bot.py
# Replace YOUR_BOT_TOKEN_HERE with your actual Discord bot token.
DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN_HERE

