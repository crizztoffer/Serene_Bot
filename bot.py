import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
import aiomysql
import json
import logging
import asyncio
from aiohttp import web
import aiohttp
import time

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# URLs used by your site features
GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")
GAME_WEBHOOK_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_update_webhook.php")

# Auth token used by your admin page to talk to the bot
BOT_ENTRY = os.getenv("BOT_ENTRY")

BOT_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# Attach aiohttp app so cogs can add routes if they want
bot.web_app = web.Application()

# ---- CRUCIAL: create /serene group BEFORE loading cogs ----
serene_group = app_commands.Group(name="serene", description="The main Serene bot commands.")
bot.tree.add_command(serene_group)
# Optional: expose it so cogs can fetch it directly if they prefer
bot.serene_group = serene_group

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- WebSocket room registries (game state & chat) ---
bot.ws_rooms = {}
bot.chat_ws_rooms = {}

# ---------------- DB helper methods ----------------

async def add_user_to_db_if_not_exists(guild_id, user_name, discord_id):
    """
    Ensure a user exists in discord_users. On first insert, also capture their current roles
    and store them in role_data as JSON: {"roles": ["<role_id>", ...]} (excluding @everyone).
    """
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
                "SELECT COUNT(*) FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            (count,) = await cursor.fetchone()
            if count == 0:
                # Build initial json_data
                initial_json_data = json.dumps({"warnings": {}})

                # Try to capture roles (IDs) for role_data, exclude @everyone
                role_ids = []
                try:
                    guild = bot.get_guild(int(guild_id))
                    member = None
                    if guild:
                        member = guild.get_member(int(discord_id))
                        if member is None:
                            # Fallback to API fetch if not cached
                            try:
                                member = await guild.fetch_member(int(discord_id))
                            except Exception:
                                member = None
                    if member:
                        role_ids = [str(r.id) for r in getattr(member, "roles", []) if not r.is_default()]
                except Exception as e:
                    logger.warning(f"Could not capture roles for new user {discord_id} in guild {guild_id}: {e}")

                role_data_json = json.dumps({"roles": role_ids})

                await cursor.execute(
                    "INSERT INTO discord_users (guild_id, user_name, discord_id, kekchipz, json_data, role_data) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (str(guild_id), user_name, str(discord_id), 2000, initial_json_data, role_data_json)
                )
                logger.info(f"Added new user '{user_name}' to DB with 2000 kekchipz and role_data={role_data_json}.")
    except Exception as e:
        logger.error(f"DB error in add_user_to_db_if_not_exists: {e}")
    finally:
        if conn:
            conn.close()

bot.add_user_to_db_if_not_exists = add_user_to_db_if_not_exists

async def post_and_save_embed(guild_id, rules_json_bytes, rules_channel_id):
    """
    Helper function to post a new Discord embed and save its details to bot_messages table.
    Expects rules_json_bytes to be bytes, will decode it.
    """
    conn = None
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

            rules_json_str = rules_json_bytes.decode('utf-8') if isinstance(rules_json_bytes, bytes) else rules_json_bytes

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

# --------------- HTTP handlers (CORS + webhook) ---------------

CORS_HEADERS = {
    'Access-Control-Allow-Origin': 'https://serenekeks.com',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
}

async def cors_preflight_handler(request):
    return web.Response(status=200, headers=CORS_HEADERS)

async def settings_saved_handler(request):
    """
    POST /settings_saved from your site. Validates BOT_ENTRY, then:
      - Loads rules & channel from DB,
      - Updates existing embed or posts a new one,
      - Updates bot_messages row to mirror content.
    """
    conn = None
    guild_id = None
    try:
        data = await request.json()
        guild_id = data.get('guild_id')
        bot_entry = data.get('bot_entry')
        action = data.get('action')

        if bot_entry != BOT_ENTRY:
            logger.warning("Unauthorized access to /settings_saved.")
            return web.Response(text="Unauthorized", status=401, headers=CORS_HEADERS)

        logger.info(f"Received signal: '{action}' for guild ID: {guild_id}")

        if not all([DB_USER, DB_PASSWORD, DB_HOST]):
            logger.error("Missing DB credentials for fetching settings.")
            return web.Response(text="Internal Server Error: DB credentials missing", status=500, headers=CORS_HEADERS)

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
                logger.warning(f"Missing 'rules' JSON or 'rules_channel' for guild ID: {guild_id}.")
                return web.Response(text="Missing rules data or channel", status=400, headers=CORS_HEADERS)

            new_rules_json_str = new_rules_json_bytes.decode('utf-8') if isinstance(new_rules_json_bytes, bytes) else new_rules_json_bytes

            try:
                embed_data_list = json.loads(new_rules_json_str)
                if not isinstance(embed_data_list, list) or not embed_data_list:
                    raise ValueError("Rules JSON is not a valid list of embeds or is empty.")
                embed_data = embed_data_list[0]
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Failed to parse rules JSON for guild {guild_id}: {e}")
                return web.Response(text="Invalid rules JSON format", status=400, headers=CORS_HEADERS)

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
                existing_message_json_bytes = bot_messages_row.get('message')
                existing_message_id = bot_messages_row.get('message_id')

                existing_message_json_str = existing_message_json_bytes.decode('utf-8') if isinstance(existing_message_json_bytes, bytes) else existing_message_json_bytes

                if existing_message_json_str != new_rules_json_str:
                    logger.info(f"Rules changed for guild {guild_id}. Updating message {existing_message_id}.")
                    try:
                        message_to_edit = await rules_channel.fetch_message(int(existing_message_id))
                        new_embed = discord.Embed.from_dict(embed_data)
                        await message_to_edit.edit(embed=new_embed)

                        await cursor.execute(
                            "UPDATE bot_messages SET message = %s WHERE guild_id = %s",
                            (new_rules_json_str, str(guild_id))
                        )
                        logger.info(f"Updated bot_messages for guild {guild_id}.")
                    except discord.errors.NotFound:
                        logger.warning(f"Message {existing_message_id} not found. Re-posting.")
                        await post_and_save_embed(guild_id, new_rules_json_str, rules_channel_id)
                    except discord.errors.Forbidden:
                        logger.error("Missing permissions to edit/send messages.")
                        return web.Response(text="Bot lacks Discord permissions", status=403, headers=CORS_HEADERS)
                else:
                    logger.info(f"No rules change for guild {guild_id}.")
            else:
                logger.info(f"No existing bot_messages for guild {guild_id}. Posting new embed.")
                await post_and_save_embed(guild_id, new_rules_json_str, rules_channel_id)

        return web.Response(text="Signal received and settings processed", status=200, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Overall error in settings_saved_handler for guild {guild_id}: {e}", exc_info=True)
        return web.Response(text="Internal Server Error", status=500, headers=CORS_HEADERS)
    finally:
        if conn:
            conn.close()

# ---------------------- GAME WS: /ws ----------------------

async def websocket_handler(request):
    """
    Game WebSocket: registers client in a room and dispatches messages to MechanicsMain.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    room_id = None
    try:
        first_msg = await ws.receive_str()
        initial_data = json.loads(first_msg)

        room_id = initial_data.get('room_id')
        guild_id = initial_data.get('guild_id')
        channel_id = initial_data.get('channel_id')
        sender_id = initial_data.get('sender_id')

        if not all([room_id, guild_id, channel_id, sender_id]):
            logger.error(f"Initial WS missing parameters: {initial_data}")
            await ws.send_str(json.dumps({"status": "error", "message": "Missing room, guild, channel, or sender ID."}))
            return

        if room_id not in bot.ws_rooms:
            bot.ws_rooms[room_id] = set()
        bot.ws_rooms[room_id].add(ws)
        logger.info(f"Game WS connected to room {room_id}. Now {len(bot.ws_rooms[room_id])} client(s).")

        mechanics_cog = bot.get_cog('MechanicsMain')
        if not mechanics_cog:
            logger.error("MechanicsMain cog not available.")
            await ws.send_str(json.dumps({"status": "error", "message": "Game mechanics not available."}))
            return

        await mechanics_cog.handle_websocket_game_action(initial_data)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    request_data = json.loads(msg.data)
                    request_data['room_id'] = room_id
                    request_data['guild_id'] = guild_id
                    request_data['channel_id'] = channel_id
                    request_data['sender_id'] = sender_id
                    await mechanics_cog.handle_websocket_game_action(request_data)
                except json.JSONDecodeError:
                    await ws.send_str(json.dumps({"status": "error", "message": "Invalid JSON format."}))
                except Exception as e:
                    logger.error(f"Game WS processing error in room {room_id}: {e}", exc_info=True)
                    await ws.send_str(json.dumps({"status": "error", "message": f"Internal server error: {e}"}))

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"Game WS error in room {room_id}: {ws.exception()}")
            elif msg.type == web.WSMsgType.CLOSE:
                logger.info(f"Game WS closed for room {room_id}.")
                break

    except asyncio.CancelledError:
        logger.info(f"Game WS to room {room_id} cancelled.")
    except Exception as e:
        logger.error(f"Game WS handler error for room {room_id}: {e}", exc_info=True)
    finally:
        if room_id and ws in bot.ws_rooms.get(room_id, set()):
            bot.ws_rooms[room_id].remove(ws)
            if not bot.ws_rooms[room_id]:
                del bot.ws_rooms[room_id]
            logger.info(f"Game WS disconnected from room {room_id}. Now {len(bot.ws_rooms.get(room_id, set()))} client(s).")
        return ws

# ---------------------- ADMIN WS: /admin_ws ----------------------

async def ensure_quarantine_objects(guild_id: str, role_name: str, channel_name: str) -> bool:
    """Create or update the quarantine role & channel for the guild."""
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            logger.error(f"ensure_quarantine_objects: Bot not in guild {guild_id}")
            return False

        # Role: create if missing
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                reason="Provision quarantine role"
            )
            logger.info(f"Created role '{role_name}' in guild {guild_id}")

        # Channel: create or update with proper overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True
            )
        }

        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if not channel:
            channel = await guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                reason="Provision quarantine channel"
            )
            logger.info(f"Created channel '{channel_name}' in guild {guild_id}")
        else:
            await channel.edit(overwrites=overwrites, reason="Ensure quarantine channel permissions")
            logger.info(f"Updated channel '{channel_name}' overwrites in guild {guild_id}")

        return True
    except discord.Forbidden:
        logger.error("Missing permissions to create/edit roles or channels.")
        return False
    except Exception as e:
        logger.error(f"ensure_quarantine_objects error: {e}", exc_info=True)
        return False

async def admin_ws_handler(request):
    """
    Admin WebSocket used by your site (wss://sbot.serenekeks.com/admin_ws).
    Expects JSON frames like:
      {"op":"provision_quarantine","bot_entry":"<BOT_ENTRY>","guild_id":"123",
       "quarantine_channel_name":"oops","quarantine_role_name":"Rule-Breaker"}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue

            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"ok": False, "error": "invalid_json"})
                continue

            if data.get("bot_entry") != BOT_ENTRY:
                await ws.send_json({"ok": False, "error": "unauthorized"})
                continue

            op = data.get("op")
            if op == "provision_quarantine":
                guild_id = data.get("guild_id")
                ch_name = data.get("quarantine_channel_name")
                rl_name = data.get("quarantine_role_name")
                if not all([guild_id, ch_name, rl_name]):
                    await ws.send_json({"ok": False, "error": "missing_fields"})
                    continue

                ok = await ensure_quarantine_objects(guild_id, rl_name, ch_name)
                await ws.send_json({"ok": ok})
            else:
                await ws.send_json({"ok": False, "error": "unknown_op"})

    except Exception as e:
        logger.error(f"admin_ws error: {e}", exc_info=True)
    finally:
        return ws

# ---------------------- WEB SERVER START ----------------------

async def start_web_server():
    """Starts the aiohttp web server."""
    bot.web_app.router.add_options('/settings_saved', cors_preflight_handler)
    bot.web_app.router.add_post('/settings_saved', settings_saved_handler)

    # WS endpoints
    bot.web_app.router.add_get('/ws', websocket_handler)          # Game state WS
    bot.web_app.router.add_get('/admin_ws', admin_ws_handler)     # Admin actions WS

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(bot.web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on http://0.0.0.0:{port}")

# ---------------------- Discord events ----------------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}.")

    # Expose DB credentials for modules like flag.py
    bot.db_user = DB_USER
    bot.db_password = DB_PASSWORD
    bot.db_host = DB_HOST

    # Load all cogs BEFORE starting the web server
    await load_cogs()

    # Global sync (optional)
    try:
        await bot.tree.sync()
        logger.info("✅ Globally synced all commands")
    except Exception as e:
        logger.error(f"Global sync failed: {e}")

    # Per-guild sync
    for guild in bot.guilds:
        try:
            await bot.tree.sync(guild=guild)
            logger.info(f"✅ Resynced commands for guild: {guild.name} ({guild.id})")
        except Exception as e:
            logger.error(f"Failed to sync commands for guild {guild.name}: {e}")

    # Ensure members exist in DB
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)

    # Post rules embed if missing (startup)
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
                await cursor.execute(
                    "SELECT rules, rules_channel FROM bot_guild_settings WHERE guild_id = %s",
                    (str(guild.id),)
                )
                settings_row = await cursor.fetchone()

                if settings_row:
                    await cursor.execute(
                        "SELECT message_id FROM bot_messages WHERE guild_id = %s",
                        (str(guild.id),)
                    )
                    bot_messages_row = await cursor.fetchone()

                    if not bot_messages_row:
                        new_rules_json_bytes = settings_row.get('rules')
                        rules_channel_id = settings_row.get('rules_channel')

                        if new_rules_json_bytes and rules_channel_id:
                            logger.info(f"Startup: posting missing rules embed for guild {guild.id}.")
                            await post_and_save_embed(str(guild.id), new_rules_json_bytes, rules_channel_id)
                        else:
                            logger.warning(f"Guild {guild.id} has settings but missing rules JSON or channel ID.")
    except Exception as e:
        logger.error(f"Startup rules embed check failed: {e}", exc_info=True)
    finally:
        if conn_on_ready:
            conn_on_ready.close()

    # Background DB check
    hourly_db_check.start()

    # Start web server AFTER cogs are loaded
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
            conn.close()

# ---------------------- Cog loader ----------------------

async def load_cogs():
    if not os.path.exists("cogs"):
        os.makedirs("cogs")

    # Order-sensitive cogs first (dependencies)
    ordered_cogs = ["mechanics_main", "communication_main"]  # Keep as-is; if missing, it's fine.
    loaded_cogs_set = set()

    for cog_name in ordered_cogs:
        try:
            full_module_name = f"cogs.{cog_name}"
            module = __import__(full_module_name, fromlist=['setup'])
            if hasattr(module, 'setup') and callable(module.setup):
                await bot.load_extension(full_module_name)
                logger.info(f"Loaded prioritized cog {full_module_name}")
                loaded_cogs_set.add(full_module_name)
            else:
                logger.warning(f"Skipping module {full_module_name}: no 'setup' function (not a cog).")
        except ModuleNotFoundError:
            logger.error(f"Prioritized cog {full_module_name} not found.")
        except Exception as e:
            logger.error(f"Failed to load prioritized cog {full_module_name}: {e}")

    # Then load everything else
    for root, dirs, files in os.walk("cogs"):
        for filename in files:
            if filename.endswith(".py") and filename != "__init__.py":
                relative_path = os.path.relpath(os.path.join(root, filename), start="cogs")
                full_module_name = f"cogs.{relative_path[:-3].replace(os.sep, '.')}"

                if ' ' in full_module_name:
                    logger.warning(f"Skipping cog '{full_module_name}' due to spaces in filename.")
                    continue

                if full_module_name not in loaded_cogs_set:
                    try:
                        module = __import__(full_module_name, fromlist=['setup'])
                        if hasattr(module, 'setup') and callable(module.setup):
                            await bot.load_extension(full_module_name)
                            logger.info(f"Loaded cog {full_module_name}")
                        else:
                            logger.info(f"Skipping module {full_module_name}: no 'setup'.")
                    except ModuleNotFoundError:
                        logger.warning(f"Cog module {full_module_name} not found, skipping.")
                    except Exception as e:
                        logger.error(f"Failed to load cog {full_module_name}: {e}")

# ---------------------- Entrypoint ----------------------

async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing")
        return
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
