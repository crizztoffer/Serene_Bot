# bot.py
# This is your main bot file, updated to handle slash commands and all environment variables.

import discord
from discord.ext import commands, tasks # Import tasks for hourly execution
from discord import app_commands # Import app_commands for slash commands
import os
from dotenv import load_dotenv # Import load_dotenv to load variables from .env file
import aiomysql # Import aiomysql for asynchronous MySQL connection
import json # Import json for handling JSON data in database

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
intents = discord.Intents.default()
intents.message_content = True # Required for bot.process_commands in on_message
intents.members = True # Required for on_member_join and iterating guild.members
intents.presences = True # If you need presence updates, as per your sb.py

# Initialize the bot. We also initialize the CommandTree for slash commands.
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# --- Database Operations (Copied from sb.py) ---

async def add_user_to_db_if_not_exists(guild_id: int, user_name: str, discord_id: int):
    """
    Checks if a user exists in the 'discord_users' table for a given guild.
    If not, inserts a new row for the user with default values.
    """
    # Use global environment variables defined at the top of bot.py
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        print("Database operation failed: Missing one or more environment variables (DB_USER, DB_PASSWORD, DB_HOST).")
        return

    db_name = "serene_users" # The database name where discord_users table resides
    table_name = "discord_users" # The table name as specified by the user

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=db_name,
            charset='utf8mb4', # Crucial for handling all Unicode characters
            autocommit=True # Set autocommit to True for simple connection check and inserts
        )
        async with conn.cursor() as cursor:
            # Check if user already exists for this guild
            # Use %s placeholders for parameters to prevent SQL injection
            await cursor.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id)) # Convert IDs to string as per VARCHAR column type
            )
            (count,) = await cursor.fetchone()

            if count == 0:
                # User does not exist, insert them
                initial_json_data = json.dumps({"warnings": {}}) # Initialize json_data as {"warnings":{}}
                await cursor.execute(
                    f"INSERT INTO {table_name} (channel_id, user_name, discord_id, kekchipz, json_data) VALUES (%s, %s, %s, %s, %s)",
                    (str(guild_id), user_name, str(discord_id), 0, initial_json_data)
                )
                print(f"Added new user '{user_name}' (ID: {discord_id}) to '{table_name}' in guild {guild_id}.")
            # else:
            #     print(f"User '{user_name}' (ID: {discord_id}) already exists in '{table_name}' for guild {guild_id}. Skipping insertion.")

    except aiomysql.Error as e:
        print(f"Database operation failed for user {user_name} (ID: {discord_id}): MySQL Error: {e}")
    except Exception as e:
        print(f"Database operation failed for user {discord_id}): An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()

async def update_user_kekchipz(guild_id: int, discord_id: int, amount: int):
    """
    Updates the kekchipz balance for a user in the database.
    """
    # Use global environment variables defined at the top of bot.py
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        print("Database operation failed: Missing one or more environment variables (DB_USER, DB_PASSWORD, DB_HOST).")
        return

    db_name = "serene_users"
    table_name = "discord_users"

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=db_name,
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            # Fetch current kekchipz
            await cursor.execute(
                f"SELECT kekchipz FROM {table_name} WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            result = await cursor.fetchone()
            
            current_kekchipz = result[0] if result else 0
            new_kekchipz = current_kekchipz + amount

            # Update kekchipz
            await cursor.execute(
                f"UPDATE {table_name} SET kekchipz = %s WHERE channel_id = %s AND discord_id = %s",
                (new_kekchipz, str(guild_id), str(discord_id))
            )
            print(f"Updated kekchipz for user {discord_id} in guild {guild_id}: {current_kekchipz} -> {new_kekchipz}")

    except aiomysql.Error as e:
        print(f"Database update failed for user {discord_id}: MySQL Error: {e}")
    except Exception as e:
        print(f"Database update failed for user {discord_id}): An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()

async def get_user_kekchipz(guild_id: int, discord_id: int) -> int:
    """
    Fetches the kekchipz balance for a user from the database.
    Returns 0 if the user is not found or an error occurs.
    """
    # Use global environment variables defined at the top of bot.py
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        print("Database operation failed: Missing one or more environment variables (DB_USER, DB_PASSWORD, DB_HOST).")
        return 0

    db_name = "serene_users"
    table_name = "discord_users"

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=db_name,
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT kekchipz FROM {table_name} WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            result = await cursor.fetchone()
            return result[0] if result else 0
    except aiomysql.Error as e:
        print(f"Database fetch failed for user {discord_id}: MySQL Error: {e}")
        return 0
    except Exception as e:
        print(f"Database fetch failed for user {discord_id}): An unexpected error occurred: {e}")
        return 0
    finally:
        if conn:
            conn.close()


# --- Bot Events ---

@bot.event
async def on_ready():
    """
    This event fires when the bot successfully connects to Discord.
    It's crucial for syncing slash commands and starting background tasks.
    """
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('Bot is ready!')

    # Sync slash commands to Discord.
    try:
        synced = await bot.tree.sync() # Sync global commands
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

    # Start the hourly database connection check
    hourly_db_check.start()

    # --- Add existing members to database on startup ---
    # Wait until the bot has cached all guilds and members
    await bot.wait_until_ready() 
    print("Checking existing guild members for database entry...")
    for guild in bot.guilds:
        print(f"Processing guild: {guild.name} (ID: {guild.id})")
        for member in guild.members:
            if not member.bot: # Only add actual users, not other bots
                await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)
    print("Finished checking existing guild members.")

    # You can set the bot's activity here if you want
    # await bot.change_presence(activity=discord.Game(name="with slash commands!"))


@bot.event
async def on_member_join(member: discord.Member):
    """
    Event handler that runs when a new member joins a guild.
    Adds the new member to the database if they don't already exist.
    """
    if member.bot: # Do not add bots to the database
        return
    print(f"New member joined: {member.display_name} (ID: {member.id}) in guild {member.guild.name} (ID: {member.guild.id}).")
    await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)


@bot.event
async def on_message(message: discord.Message):
    """Listens for messages to process commands."""
    # Ignore messages from the bot itself
    if message.author.id == bot.user.id:
        return

    # Process other commands normally (e.g., prefix commands if any are defined)
    await bot.process_commands(message)


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


# --- Hourly Database Connection Check ---
@tasks.loop(hours=1)
async def hourly_db_check():
    """
    Attempts to connect to the MySQL database every hour using environment variables.
    Logs success or failure to the console.
    This is primarily for monitoring database connectivity.
    """
    print("Attempting hourly database connection check...")
    # Use global environment variables defined at the top of bot.py
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        print("Database connection failed: Missing one or more environment variables (DB_USER, DB_PASSWORD, DB_HOST).")
        return

    db_name = "serene_users" # The user specified "serene_users" datatable

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=db_name,
            charset='utf8mb4', # Crucial for handling all Unicode characters
            autocommit=True # Set autocommit to True for simple connection check
        )
        print(f"Successfully connected to MySQL database '{db_name}' on host '{DB_HOST}' as user '{DB_USER}'.")
    except aiomysql.Error as e:
        print(f"Database connection failed: MySQL Error: {e}")
    except Exception as e:
        print(f"Database connection failed: An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()
            print("Database connection closed.")

@hourly_db_check.error
async def hourly_db_check_error(exception):
    """Error handler for the hourly_db_check task."""
    print(f"An error occurred in hourly_db_check task: {exception}")


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
    if GEMINI_API_KEY is None:
        print("Warning: GEMINI_API_KEY not set. Gemini API calls may fail.")
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        print("Warning: Database credentials (DB_USER, DB_PASSWORD, DB_HOST) not fully set. Database operations may fail.")


    await load_cogs()
    await bot.start(TOKEN)

# Run the main function
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

