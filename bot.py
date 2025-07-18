# --- bot.py ---

import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
import aiomysql
import json
import logging

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

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
        conn = await aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db="serene_users", charset='utf8mb4', autocommit=True)
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT COUNT(*) FROM discord_users WHERE channel_id = %s AND discord_id = %s", (str(guild_id), str(discord_id)))
            (count,) = await cursor.fetchone()
            if count == 0:
                initial_json_data = json.dumps({"warnings": {}})
                await cursor.execute("INSERT INTO discord_users (channel_id, user_name, discord_id, kekchipz, json_data) VALUES (%s, %s, %s, %s, %s)", (str(guild_id), user_name, str(discord_id), 0, initial_json_data))
                logger.info(f"Added new user '{user_name}' to DB.")
    except Exception as e:
        logger.error(f"DB error: {e}")
    finally:
        if conn:
            await conn.ensure_closed()

# Other DB functions (update_user_kekchipz, get_user_kekchipz) follow same pattern

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}.")

    await load_cogs()
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Slash sync failed: {e}")

    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)

    hourly_db_check.start()

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
        logger.error(f"Command error: {error}")
        await ctx.send(f"Unexpected error: {error}")

@tasks.loop(hours=1)
async def hourly_db_check():
    try:
        conn = await aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db="serene_users", charset='utf8mb4', autocommit=True)
        logger.info("DB connection OK.")
    except Exception as e:
        logger.error(f"Hourly DB check failed: {e}")
    finally:
        if conn:
            await conn.ensure_closed()

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
