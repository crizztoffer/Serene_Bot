# bot.py
# This is your main bot file, updated to handle slash commands and all environment variables.

import discord
from discord.ext import commands
from discord import app_commands # Import app_commands for slash commands
import os
from dotenv import load_dotenv # Import load_dotenv to load variables from .env file

# Load environment variables from .env file (for local development)
# On Railway, these will be automatically provided by the platform.
load_dotenv()

# --- Retrieve all necessary environment variables ---
TOKEN = os.getenv("BOT_TOKEN") # Using BOT_TOKEN as per your sb.py
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# Define your bot's prefix (still useful for some legacy commands or debugging)
BOT_PREFIX = "!"

# Initialize the bot with intents.
# For slash commands, message_content intent is generally not required
# unless you also plan to process regular text messages.
intents = discord.Intents.default()
# intents.message_content = True # Uncomment if you also need to read regular message content
# intents.members = True # If you need on_member_join or member-related events
# intents.presences = True # If you need presence updates

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
    try:
        synced = await bot.tree.sync() # Sync global commands
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
        await ctx.send(f"An unexpected error occurred with a prefix command: {e}")


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
    # Check if the main bot token is set
    if TOKEN is None:
        print("Error: BOT_TOKEN environment variable not found.")
        print("Please ensure it's set in your Railway variables or in a local .env file.")
        return

    # You might want to add checks for other critical variables too, e.g.:
    # if GEMINI_API_KEY is None:
    #     print("Warning: GEMINI_API_KEY not set. Gemini API calls may fail.")
    # if not all([DB_USER, DB_PASSWORD, DB_HOST]):
    #     print("Warning: Database credentials (DB_USER, DB_PASSWORD, DB_HOST) not fully set. Database operations may fail.")


    await load_cogs()
    await bot.start(TOKEN)

# Run the main function
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

