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
import aiohttp # Import aiohttp for making webhooks (used by mechanics_main)

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
# NEW: Environment variables for game web URL and webhook URL
GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")
GAME_WEBHOOK_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_update_webhook.php")


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

# --- WebSocket specific global variable for rooms ---
# This will map room_id to a set of connected WebSocket clients in that room.
bot.ws_rooms = {} # Initialize this here so it's accessible to cogs

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
            # Updated to use guild_id column in the SELECT query
            await cursor.execute(
                "SELECT COUNT(*) FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            (count,) = await cursor.fetchone()
            if count == 0:
                initial_json_data = json.dumps({"warnings": {}})
                # Updated to use guild_id column in the INSERT query
                await cursor.execute(
                    "INSERT INTO discord_users (guild_id, user_name, discord_id, kekchipz, json_data) VALUES (%s, %s, %s, %s, %s)",
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

async def post_and_save_embed(guild_id, rules_json_bytes, rules_channel_id):
    """
    Helper function to post a new Discord embed and save its details to bot_messages table.
    Expects rules_json_bytes to be bytes, will decode it.
    """
    conn = None # Initialize conn to None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor
        )
        async with conn.cursor() as cursor:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                logger.warning(f"Bot not in guild {guild_id}. Cannot post rules embed.")
                return

            rules_channel = guild.get_channel(int(rules_channel_id))
            if not rules_channel:
                logger.warning(f"Rules channel {rules_channel_id} not found for guild {guild_id}. Cannot post rules embed.")
                return

            # Decode rules_json_bytes to string
            rules_json_str = rules_json_bytes.decode('utf-8') if isinstance(rules_json_bytes, bytes) else rules_json_bytes
            logger.debug(f"post_and_save_embed: Decoded rules_json_str for guild {guild_id}: {rules_json_str[:200]}...") # Log first 200 chars
            logger.debug(f"post_and_save_embed: Type of rules_json_str: {type(rules_json_str)}")


            try:
                embed_data_list = json.loads(rules_json_str)
                if not isinstance(embed_data_list, list) or not embed_data_list:
                    raise ValueError("Rules JSON is not a valid list of embeds or is empty.")
                embed_data = embed_data_list[0]
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Failed to parse rules JSON for guild {guild_id}: {e}")
                return

            new_embed = discord.Embed.from_dict(embed_data)
            sent_message = await rules_channel.send(embed=new_embed)
            logger.info(f"Posted new Discord message {sent_message.id} in channel {rules_channel_id} for guild {guild_id}.")

            await cursor.execute(
                "INSERT INTO bot_messages (guild_id, message, message_id) VALUES (%s, %s, %s)",
                (str(guild_id), rules_json_str, str(sent_message.id))
            )
            logger.info(f"Inserted new entry into bot_messages table for guild {guild_id}.")

    except discord.errors.Forbidden:
        logger.error(f"Bot lacks permissions to send messages in channel {rules_channel_id} for guild {guild_id}.")
    except Exception as e:
        logger.error(f"Error posting and saving embed for guild {guild_id}: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

# --- Web Server Setup ---

# CORS headers for preflight and actual requests
CORS_HEADERS = {
    'Access-Control-Allow-Origin': 'https://serenekeks.com', # Replace with your actual domain
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400' # Cache preflight for 24 hours
}

async def cors_preflight_handler(request):
    """Handles CORS OPTIONS preflight requests."""
    return web.Response(status=200, headers=CORS_HEADERS)

async def settings_saved_handler(request):
    """
    Handles POST requests to /settings_saved endpoint.
    Expects a JSON body with 'guild_id' and 'bot_entry'.
    """
    conn = None # Initialize conn to None
    try:
        data = await request.json()
        guild_id = data.get('guild_id')
        bot_entry = data.get('bot_entry')
        action = data.get('action')

        if bot_entry == BOT_ENTRY:
            logger.info(f"Received signal: '{action}' for guild ID: {guild_id}")

            # Fetch settings from the database (bot_guild_settings)
            if not all([DB_USER, DB_PASSWORD, DB_HOST]):
                logger.error("Missing DB credentials for fetching settings.")
                return web.Response(text="Internal Server Error: DB credentials missing", status=500, headers=CORS_HEADERS)

            conn = await aiomysql.connect(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                db="serene_users", # Assuming 'serene_users' is the database where 'bot_guild_settings' and 'bot_messages' tables reside
                charset='utf8mb4',
                autocommit=True,
                cursorclass=aiomysql.cursors.DictCursor # To get results as dictionaries
            )
            async with conn.cursor() as cursor:
                # 1. Get the rules from bot_guild_settings
                await cursor.execute(
                    "SELECT rules, rules_channel FROM bot_guild_settings WHERE guild_id = %s",
                    (str(guild_id),)
                )
                settings_row = await cursor.fetchone()

                if not settings_row:
                    logger.warning(f"No settings found for guild ID: {guild_id} in bot_guild_settings.")
                    return web.Response(text="No settings found for guild", status=404, headers=CORS_HEADERS)

                new_rules_json_bytes = settings_row.get('rules')
                rules_channel_id = settings_row.get('rules_channel')

                if not new_rules_json_bytes or not rules_channel_id:
                    logger.warning(f"Missing 'rules' JSON or 'rules_channel' for guild ID: {guild_id}. Cannot process embed.")
                    return web.Response(text="Missing rules data or channel", status=400, headers=CORS_HEADERS)

                # Decode new_rules_json from bytes to string
                new_rules_json_str = new_rules_json_bytes.decode('utf-8') if isinstance(new_rules_json_bytes, bytes) else new_rules_json_bytes
                logger.debug(f"settings_saved_handler: Decoded new_rules_json_str for guild {guild_id}: {new_rules_json_str[:200]}...") # Log first 200 chars
                logger.debug(f"settings_saved_handler: Type of new_rules_json_str: {type(new_rules_json_str)}")


                # Parse the new rules JSON
                try:
                    # Discord API expects an array of embeds, usually just one
                    embed_data_list = json.loads(new_rules_json_str)
                    if not isinstance(embed_data_list, list) or not embed_data_list:
                        raise ValueError("Rules JSON is not a valid list of embeds or is empty.")
                    embed_data = embed_data_list[0] # Take the first embed
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"Failed to parse rules JSON for guild {guild_id}: {e}")
                    return web.Response(text="Invalid rules JSON format", status=400, headers=CORS_HEADERS)

                # 2. Check bot_messages table
                await cursor.execute(
                    "SELECT message, message_id FROM bot_messages WHERE guild_id = %s",
                    (str(guild_id),)
                )
                bot_messages_row = await cursor.fetchone()

                guild = bot.get_guild(int(guild_id))
                if not guild:
                    logger.error(f"Bot is not in guild with ID: {guild_id}")
                    return web.Response(text="Bot not in specified guild", status=404, headers=CORS_HEADERS)

                rules_channel = guild.get_channel(int(rules_channel_id))
                if not rules_channel:
                    logger.error(f"Rules channel with ID {rules_channel_id} not found in guild {guild_id}.")
                    return web.Response(text="Rules channel not found", status=404, headers=CORS_HEADERS)

                if bot_messages_row:
                    # Row exists, compare messages
                    existing_message_json_bytes = bot_messages_row.get('message')
                    existing_message_id = bot_messages_row.get('message_id')

                    # Decode existing_message_json from bytes to string
                    existing_message_json_str = existing_message_json_bytes.decode('utf-8') if isinstance(existing_message_json_bytes, bytes) else existing_message_json_bytes
                    logger.debug(f"settings_saved_handler: Decoded existing_message_json_str for guild {guild_id}: {existing_message_json_str[:200]}...") # Log first 200 chars
                    logger.debug(f"settings_saved_handler: Type of existing_message_json_str: {type(existing_message_json_str)}")


                    if existing_message_json_str != new_rules_json_str:
                        logger.info(f"Rules content changed for guild {guild_id}. Attempting to update message.")
                        try:
                            # Fetch the existing message
                            message_to_edit = await rules_channel.fetch_message(int(existing_message_id))
                            # Construct new embed
                            new_embed = discord.Embed.from_dict(embed_data)
                            await message_to_edit.edit(embed=new_embed)
                            logger.info(f"Successfully updated Discord message {existing_message_id} in channel {rules_channel_id} for guild {guild_id}.")

                            # Update bot_messages table with the new JSON
                            await cursor.execute(
                                "UPDATE bot_messages SET message = %s WHERE guild_id = %s",
                                (new_rules_json_str, str(guild_id))
                            )
                            logger.info(f"Updated bot_messages table for guild {guild_id}.")
                        except discord.errors.NotFound:
                            logger.warning(f"Message {existing_message_id} not found in channel {rules_channel_id}. Re-posting new message.")
                            # Message not found, proceed to post new message
                            await post_and_save_embed(guild_id, new_rules_json_str, rules_channel_id) # Reuse helper
                        except discord.errors.Forbidden:
                            logger.error(f"Bot lacks permissions to edit/send messages in channel {rules_channel_id} for guild {guild_id}.")
                            return web.Response(text="Bot lacks Discord permissions", status=403, headers=CORS_HEADERS)
                        except Exception as discord_e:
                            logger.error(f"Error interacting with Discord API for guild {guild_id}: {discord_e}")
                            return web.Response(text="Discord API error", status=500, headers=CORS_HEADERS)
                    else:
                        logger.info(f"Rules content is identical for guild {guild_id}. No update needed.")
                else:
                    # No row exists in bot_messages, post new embed
                    logger.info(f"No existing bot_messages entry for guild {guild_id}. Posting new embed.")
                    await post_and_save_embed(guild_id, new_rules_json_str, rules_channel_id) # Reuse helper

            return web.Response(text="Signal received and settings processed", status=200, headers=CORS_HEADERS)
        else:
            logger.warning(f"Unauthorized access attempt to /settings_saved. Invalid BOT_ENTRY: {bot_entry}")
            return web.Response(text="Unauthorized", status=401, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Overall error in settings_saved_handler for guild {guild_id}: {e}", exc_info=True)
        return web.Response(text="Internal Server Error", status=500, headers=CORS_HEADERS)
    finally:
        if conn:
            conn.close()

# REMOVED: game_action_route_handler and the /game_action POST route
# as all game actions will now go through the WebSocket.

# --- AIOHTTP App for WebSocket ---
async def websocket_handler(request):
    """
    Handles WebSocket connections for game rooms.
    It registers the client to a specific room and dispatches messages
    to the MechanicsMain cog for processing and broadcasting.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    room_id = None # Initialize room_id for this specific websocket
    try:
        # The first message from the client should contain room_id, guild_id, channel_id, sender_id
        # to register this WebSocket connection to a specific game room.
        first_msg = await ws.receive_str()
        initial_data = json.loads(first_msg)
        
        room_id = initial_data.get('room_id')
        guild_id = initial_data.get('guild_id')
        channel_id = initial_data.get('channel_id')
        sender_id = initial_data.get('sender_id')

        if not all([room_id, guild_id, channel_id, sender_id]):
            logger.error(f"Initial WebSocket message missing critical parameters: {initial_data}")
            await ws.send_str(json.dumps({"status": "error", "message": "Missing room, guild, channel, or sender ID in initial message."}))
            return # Close connection if initial data is bad

        # Register the WebSocket to the bot's ws_rooms structure
        if room_id not in bot.ws_rooms:
            bot.ws_rooms[room_id] = set()
        bot.ws_rooms[room_id].add(ws)
        logger.info(f"WebSocket client connected and registered to room {room_id}. Total connections in room: {len(bot.ws_rooms[room_id])}")

        # Get the MechanicsMain cog
        mechanics_cog = bot.get_cog('MechanicsMain')
        if not mechanics_cog:
            logger.error("MechanicsMain cog not loaded or accessible in websocket_handler.")
            await ws.send_str(json.dumps({"status": "error", "message": "Game mechanics not available."}))
            return

        # Process the initial message (e.g., 'get_state' or 'add_player')
        # This will trigger the cog to handle the action and broadcast the state.
        await mechanics_cog.handle_websocket_game_action(initial_data)

        # Process subsequent messages
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                logger.info(f"Message from WebSocket client in room {room_id}: {msg.data}")
                try:
                    request_data = json.loads(msg.data)
                    # Ensure room_id, guild_id, channel_id, sender_id are always passed with requests
                    # The frontend should be sending these with every action.
                    request_data['room_id'] = room_id # Ensure room_id is consistent
                    request_data['guild_id'] = guild_id
                    request_data['channel_id'] = channel_id
                    request_data['sender_id'] = sender_id

                    # Delegate to the MechanicsMain cog for processing and broadcasting
                    await mechanics_cog.handle_websocket_game_action(request_data)
                except json.JSONDecodeError:
                    logger.error(f"Received malformed JSON from WebSocket client in room {room_id}: {msg.data}")
                    await ws.send_str(json.dumps({"status": "error", "message": "Invalid JSON format."}))
                except Exception as e:
                    logger.error(f"Error processing WebSocket message in room {room_id}: {e}", exc_info=True)
                    await ws.send_str(json.dumps({"status": "error", "message": f"Internal server error: {e}"}))

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"WebSocket error in room {room_id}: {ws.exception()}")
            elif msg.type == web.WSMsgType.CLOSE:
                logger.info(f"WebSocket client closed connection from room {room_id}.")
                break # Exit the loop on close message

    except asyncio.CancelledError:
        logger.info(f"WebSocket connection to room {room_id} cancelled (likely client disconnected).")
    except Exception as e:
        logger.error(f"Error in WebSocket handler for room {room_id}: {e}", exc_info=True)
    finally:
        # Clean up the WebSocket from the room's set
        if room_id and ws in bot.ws_rooms.get(room_id, set()):
            bot.ws_rooms[room_id].remove(ws)
            if not bot.ws_rooms[room_id]: # If no more clients in room, remove room entry
                del bot.ws_rooms[room_id]
            logger.info(f"WebSocket client disconnected from room {room_id}. Remaining connections in room: {len(bot.ws_rooms.get(room_id, set()))}")
        elif ws in bot.ws_rooms.get(room_id, set()): # Fallback if room_id somehow got unset but ws is still in a room
             bot.ws_rooms[room_id].remove(ws)
             if not bot.ws_rooms[room_id]:
                del bot.ws_rooms[room_id]
             logger.warning(f"WebSocket client disconnected, room_id was unset, but found in ws_rooms. Cleaned up.")
        return ws


async def start_web_server():
    """Starts the aiohttp web server, including WebSocket."""
    app = web.Application()
    # Add OPTIONS handler for CORS preflight
    app.router.add_options('/settings_saved', cors_preflight_handler)
    app.router.add_post('/settings_saved', settings_saved_handler)

    # REMOVED: app.router.add_options('/game_action', cors_preflight_handler)
    # REMOVED: app.router.add_post('/game_action', game_action_route_handler)

    # --- Add WebSocket route ---
    app.router.add_get('/ws', websocket_handler)

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

    # Load all cogs - ENSURE THIS COMPLETES BEFORE STARTING WEB SERVER
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

    # --- New: Check and post rules embed on startup if missing ---
    conn_on_ready = None
    try:
        conn_on_ready = await aiomysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor
        )
        async with conn_on_ready.cursor() as cursor:
            for guild in bot.guilds:
                # 1. Check bot_guild_settings for this guild
                await cursor.execute(
                    "SELECT rules, rules_channel FROM bot_guild_settings WHERE guild_id = %s",
                    (str(guild.id),)
                )
                settings_row = await cursor.fetchone()

                if settings_row:
                    # 2. Check bot_messages for this guild
                    await cursor.execute(
                        "SELECT message_id FROM bot_messages WHERE guild_id = %s",
                        (str(guild.id),)
                    )
                    bot_messages_row = await cursor.fetchone()

                    if not bot_messages_row:
                        # Case: Entry in bot_guild_settings but not in bot_messages
                        new_rules_json_bytes = settings_row.get('rules')
                        rules_channel_id = settings_row.get('rules_channel')

                        if new_rules_json_bytes and rules_channel_id:
                            logger.info(f"Detected missing rules embed for guild {guild.id} on startup. Attempting to post.")
                            # Call the helper function to post and save the embed
                            await post_and_save_embed(str(guild.id), new_rules_json_bytes, rules_channel_id)
                        else:
                            logger.warning(f"Guild {guild.id} has settings but missing rules JSON or channel ID. Skipping rules embed post on startup.")
                # else: No settings for this guild, nothing to do.
    except Exception as e:
        logger.error(f"Error during startup rules embed check: {e}", exc_info=True)
    finally:
        if conn_on_ready:
            conn_on_ready.close()
    # --- End new section ---

    # Start background DB check
    hourly_db_check.start()

    # Start the web server in a separate asyncio task AFTER cogs are loaded
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
        logger.error(f"Command error: {error}")
        await ctx.send(f"Unexpected error: {error}")

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

    # List of cogs to load in a specific order (dependencies first)
    # This assumes mechanics_main.py is directly in the cogs/ directory
    ordered_cogs = ["mechanics_main"]
    loaded_cogs_set = set()

    # First, load explicitly ordered cogs
    for cog_name in ordered_cogs:
        try:
            full_module_name = f"cogs.{cog_name}"
            # Check if the module has a setup function before attempting to load as a cog
            # This prevents errors for utility files like game_models.py that are not cogs
            module = __import__(full_module_name, fromlist=['setup'])
            if hasattr(module, 'setup') and callable(module.setup):
                await bot.load_extension(full_module_name)
                logger.info(f"Loaded prioritized cog {full_module_name}")
                loaded_cogs_set.add(full_module_name)
            else:
                logger.warning(f"Skipping module {full_module_name}: No 'setup' function found, not a Discord cog.")
        except ModuleNotFoundError:
            logger.error(f"Prioritized cog {full_module_name} not found.")
        except Exception as e:
            logger.error(f"Failed to load prioritized cog {full_module_name}: {e}")

    # Then, load remaining cogs (including those in subdirectories)
    for root, dirs, files in os.walk("cogs"):
        for filename in files:
            if filename.endswith(".py") and filename != "__init__.py__":
                # Calculate the full module path
                relative_path = os.path.relpath(os.path.join(root, filename), start="cogs")
                full_module_name = f"cogs.{relative_path[:-3].replace(os.sep, '.')}"

                # IMPORTANT: Handle invalid Python module names (e.g., spaces)
                # If a cog file has spaces in its name (like "Serene Texas Hold Em.py"),
                # it cannot be directly imported as a Python module.
                # You MUST rename such files to use underscores or camelCase (e.g., "serene_texas_hold_em.py").
                # This log will help identify such files.
                if ' ' in full_module_name:
                    logger.warning(f"Skipping cog '{full_module_name}' due to invalid characters (spaces) in module name. Please rename the file.")
                    continue

                if full_module_name not in loaded_cogs_set:
                    try:
                        # Check if the module has a setup function before attempting to load as a cog
                        module = __import__(full_module_name, fromlist=['setup'])
                        if hasattr(module, 'setup') and callable(module.setup):
                            await bot.load_extension(full_module_name)
                            logger.info(f"Loaded cog {full_module_name}")
                        else:
                            logger.info(f"Skipping module {full_module_name}: No 'setup' function found, not a Discord cog.")
                    except ModuleNotFoundError:
                        logger.warning(f"Cog module {full_module_name} not found, skipping.")
                    except Exception as e:
                        logger.error(f"Failed to load cog {full_module_name}: {e}")


async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing")
        return
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
