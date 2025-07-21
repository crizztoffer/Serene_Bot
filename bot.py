import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
import aiomysql
import json
import logging
import asyncio # Import asyncio for running web server in a separate task
from aiohttp import web # Import aiohttp for the web server

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# Define the BOT_ENTRY key for validation
BOT_ENTRY = os.getenv("BOT_ENTRY")

BOT_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# Add /serene group BEFORE cog loading
serene_group = app_commands.Group(name="serene", description="The main Serene bot commands.")
bot.tree.add_command(serene_group)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# DB methods
async def add_user_to_db_if_not_exists(guild_id, user_name, discord_id):
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        logger.error("Missing DB credentials.")
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT COUNT(*) FROM discord_users WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            (count,) = await cursor.fetchone()
            if count == 0:
                initial_json_data = json.dumps({"warnings": {}})
                await cursor.execute(
                    "INSERT INTO discord_users (channel_id, user_name, discord_id, kekchipz, json_data) VALUES (%s, %s, %s, %s, %s)",
                    (str(guild_id), user_name, str(discord_id), 0, initial_json_data)
                )
                logger.info(f"Added new user '{user_name}' to DB.")
    except Exception as e:
        logger.error(f"DB error in add_user_to_db_if_not_exists: {e}")
    finally:
        if conn:
            conn.close() # Use conn.close() for aiomysql connections

bot.add_user_to_db_if_not_exists = add_user_to_db_if_not_exists

async def load_flag_reasons():
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        logger.error("Missing DB credentials, cannot load flag reasons.")
        bot.flag_reasons = []
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT reason FROM rule_flagging")
            rows = await cursor.fetchall()
            logger.info(f"DB rows fetched: {rows}")
            bot.flag_reasons = [row[0] for row in rows]
            logger.info(f"Loaded flag reasons: {bot.flag_reasons}")
    except Exception as e:
        logger.error(f"Failed to load flag reasons: {e}")
        bot.flag_reasons = []
    finally:
        if conn:
            conn.close() # Use conn.close() for aiomysql connections

# --- Web Server Setup ---
async def settings_saved_handler(request):
    """
    Handles POST requests to /settings_saved endpoint.
    Expects a JSON body with 'guild_id' and 'bot_entry'.
    """
    try:
        data = await request.json()
        guild_id = data.get('guild_id')
        bot_entry = data.get('bot_entry')
        action = data.get('action')

        if bot_entry == BOT_ENTRY:
            logger.info(f"Received signal: '{action}' for guild ID: {guild_id}")
            # You can add more logic here, e.g., send a message to a specific Discord channel
            # For example:
            # guild = bot.get_guild(int(guild_id))
            # if guild:
            #     # Replace 'your-log-channel-id' with an actual channel ID where you want notifications
            #     log_channel = guild.get_channel(YOUR_LOG_CHANNEL_ID)
            #     if log_channel:
            #         await log_channel.send(f"Server settings for guild `{guild.name}` have been saved!")
            return web.Response(text="Signal received", status=200)
        else:
            logger.warning(f"Unauthorized access attempt to /settings_saved. Invalid BOT_ENTRY: {bot_entry}")
            return web.Response(text="Unauthorized", status=401)
    except Exception as e:
        logger.error(f"Error in settings_saved_handler: {e}")
        return web.Response(text="Internal Server Error", status=500)

async def start_web_server():
    """Starts the aiohttp web server."""
    app = web.Application()
    app.router.add_post('/settings_saved', settings_saved_handler)

    # Get port from environment variable, default to 8080
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port) # Listen on all interfaces
    await site.start()
    logger.info(f"Web server started on http://0.0.0.0:{port}")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}.")

    # Load flag reasons before loading cogs
    await load_flag_reasons()

    # Assign DB credentials for use in modules like flag.py
    bot.db_user = DB_USER
    bot.db_password = DB_PASSWORD
    bot.db_host = DB_HOST

    # Load all cogs
    await load_cogs()

    # Global sync (optional, helpful to clear cache)
    try:
        await bot.tree.sync()
        logger.info("✅ Globally synced all commands")
    except Exception as e:
        logger.error(f"Global sync failed: {e}")

    # Force-sync commands per guild
    for guild in bot.guilds:
        try:
            await bot.tree.sync(guild=guild)
            logger.info(f"✅ Resynced commands for guild: {guild.name} ({guild.id})")
        except Exception as e:
            logger.error(f"Failed to sync commands for guild {guild.name}: {e}")

    # Ensure all users are in DB
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)

    # Start background DB check
    hourly_db_check.start()

    # Start the web server in a separate asyncio task
    bot.loop.create_task(start_web_server())


@bot.event
async def on_member_join(member):
    if not member.bot:
        await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)

@bot.event
async def on_message(message):
    if message.author.id != bot.user.id:
        await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Command not found.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: {error.param.name}.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You lack permissions.")
    else:
        logger.error(f"Command error: {e}")
        await ctx.send(f"Unexpected error: {e}")

@tasks.loop(hours=1)
async def hourly_db_check():
    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        logger.info("DB connection OK.")
    except Exception as e:
        logger.error(f"Hourly DB check failed: {e}")
    finally:
        if conn:
            conn.close() # Use conn.close() for aiomysql connections

async def load_cogs():
    if not os.path.exists("cogs"):
        os.makedirs("cogs")
    for filename in os.listdir("cogs"):
        if filename.endswith(".py"):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                logger.info(f"Loaded cog {filename}")
            except Exception as e:
                logger.error(f"Failed to load cog {filename}: {e}")

async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing")
        return
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
