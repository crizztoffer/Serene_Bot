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
from typing import Optional, Dict, Tuple

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
BOT_ENTRY = os.getenv("BOT_ENTRY")  # shared secret for admin-to-bot pushes

BOT_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# HTTP app
bot.web_app = web.Application()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Game WS room state (unchanged)
bot.ws_rooms: Dict[str, set] = {}
bot.chat_ws_rooms: Dict[str, set] = {}

# Cache of quarantine options we last saw from DB:
# { guild_id(str): (channel_name, role_name, updated_at_str_or_None) }
bot.quarantine_options_cache: Dict[str, Tuple[str, str, Optional[str]]] = {}

# ---------------- DB helpers ----------------

async def add_user_to_db_if_not_exists(guild_id, user_name, discord_id):
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        logger.error("Missing DB credentials.")
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT COUNT(*) FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            (count,) = await cursor.fetchone()
            if count == 0:
                initial_json_data = json.dumps({"warnings": {}})
                await cursor.execute(
                    "INSERT INTO discord_users (guild_id, user_name, discord_id, kekchipz, json_data) VALUES (%s, %s, %s, %s, %s)",
                    (str(guild_id), user_name, str(discord_id), 2000, initial_json_data)
                )
                logger.info(f"Added new user '{user_name}' to DB with 2000 kekchipz.")
    except Exception as e:
        logger.error(f"DB error in add_user_to_db_if_not_exists: {e}")
    finally:
        if conn:
            conn.close()

bot.add_user_to_db_if_not_exists = add_user_to_db_if_not_exists

async def load_flag_reasons():
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        logger.error("Missing DB credentials, cannot load flag reasons.")
        bot.flag_reasons = []
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT reason FROM rule_flagging WHERE guild_id = 'DEFAULT'")
            rows = await cursor.fetchall()
            bot.flag_reasons = [row[0] for row in rows]
            logger.info(f"Loaded default flag reasons (preload): {bot.flag_reasons}")
    except Exception as e:
        logger.error(f"Failed to load flag reasons: {e}")
        bot.flag_reasons = []
    finally:
        if conn:
            conn.close()

# ---------------- Rules embed post helper ----------------

async def post_and_save_embed(guild_id, rules_json_bytes, rules_channel_id):
    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db="serene_users",
            charset='utf8mb4', autocommit=True, cursorclass=aiomysql.cursors.DictCursor
        )
        async with conn.cursor() as cursor:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                logger.warning(f"Bot not in guild {guild_id}. Cannot post rules embed.")
                return

            rules_channel = guild.get_channel(int(rules_channel_id))
            if not rules_channel:
                logger.warning(f"Rules channel {rules_channel_id} not found for guild {guild_id}.")
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
    except discord.errors.Forbidden:
        logger.error(f"Bot lacks permissions to send messages in channel {rules_channel_id} for guild {guild_id}.")
    except Exception as e:
        logger.error(f"Error posting and saving embed for guild {guild_id}: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

# ---------------- Quarantine (role/channel) provisioning ----------------

async def ensure_quarantine_setup(guild: discord.Guild, channel_name: str, role_name: str) -> dict:
    """
    Ensure the quarantine role/channel exist and permissions are correct.
    Returns a summary dict of what was created/updated.
    """
    summary = {"role_created": False, "channel_created": False, "overwrites_updated": False}

    if not channel_name or not role_name:
        logger.warning(f"[{guild.name}] Missing quarantine names; skipping ensure.")
        return summary

    # 1) Ensure role exists
    q_role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
    if q_role is None:
        try:
            q_role = await guild.create_role(
                name=role_name,
                reason="Quarantine role for rules gating",
                permissions=discord.Permissions.none()
            )
            summary["role_created"] = True
            logger.info(f"[{guild.name}] Created quarantine role: {q_role.name}")
        except discord.Forbidden:
            logger.error(f"[{guild.name}] Missing MANAGE_ROLES to create role '{role_name}'.")
            return summary
        except Exception as e:
            logger.error(f"[{guild.name}] Failed to create role '{role_name}': {e}")
            return summary

    # 2) Ensure channel exists
    q_channel = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == channel_name.lower(),
        guild.text_channels
    )
    if q_channel is None:
        try:
            q_channel = await guild.create_text_channel(
                name=channel_name,
                reason="Quarantine rules-only channel"
            )
            summary["channel_created"] = True
            logger.info(f"[{guild.name}] Created quarantine channel: #{q_channel.name}")
        except discord.Forbidden:
            logger.error(f"[{guild.name}] Missing MANAGE_CHANNELS to create channel '{channel_name}'.")
            return summary
        except Exception as e:
            logger.error(f"[{guild.name}] Failed to create channel '{channel_name}': {e}")
            return summary

    # 3) Set permission overwrites on the quarantine channel
    try:
        overwrites = q_channel.overwrites
        changed = False

        everyone = guild.default_role
        want_everyone = discord.PermissionOverwrite(view_channel=False)
        if overwrites.get(everyone) != want_everyone:
            overwrites[everyone] = want_everyone
            changed = True

        want_q = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
        if overwrites.get(q_role) != want_q:
            overwrites[q_role] = want_q
            changed = True

        if changed:
            await q_channel.edit(overwrites=overwrites, reason="Ensure quarantine channel overwrites")
            summary["overwrites_updated"] = True
            logger.info(f"[{guild.name}] Updated overwrites on #{q_channel.name}")

    except discord.Forbidden:
        logger.error(f"[{guild.name}] Missing MANAGE_CHANNELS to edit overwrites for '{q_channel.name}'.")
    except Exception as e:
        logger.error(f"[{guild.name}] Failed to update overwrites on '{q_channel.name}': {e}")

    # 4) Deny the quarantine role across all other categories/channels
    try:
        for category in guild.categories:
            try:
                await category.set_permissions(q_role, view_channel=False, send_messages=False, reason="Quarantine deny across categories")
            except Exception:
                pass

        for channel in guild.channels:
            if hasattr(channel, "id") and channel.id == q_channel.id:
                continue
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.CategoryChannel)):
                try:
                    await channel.set_permissions(q_role, view_channel=False, send_messages=False, reason="Quarantine deny per channel")
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[{guild.name}] Error while applying global denies for quarantine role: {e}")

    return summary

async def fetch_all_quarantine_options() -> Dict[str, Tuple[str, str, Optional[str]]]:
    out: Dict[str, Tuple[str, str, Optional[str]]] = {}
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        return out

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db="serene_users",
            charset='utf8mb4', autocommit=True, cursorclass=aiomysql.cursors.DictCursor
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT guild_id, quarantine_channel_name, quarantine_role_name, "
                "DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS updated_at "
                "FROM bot_flag_action_options"
            )
            rows = await cursor.fetchall()
            for row in rows or []:
                gid = str(row.get("guild_id"))
                ch = (row.get("quarantine_channel_name") or "").strip()
                rl = (row.get("quarantine_role_name") or "").strip()
                ts = row.get("updated_at")
                if gid:
                    out[gid] = (ch, rl, ts)
    except Exception as e:
        logger.error(f"Failed fetching quarantine options: {e}")
    finally:
        if conn:
            conn.close()
    return out

@tasks.loop(minutes=5)
async def sync_quarantine_task():
    """
    Safety net: periodically check options and ensure provisioning if changed.
    """
    try:
        all_opts = await fetch_all_quarantine_options()
        for guild in bot.guilds:
            gid = str(guild.id)
            if gid not in all_opts:
                continue
            ch, rl, ts = all_opts[gid]
            cached = bot.quarantine_options_cache.get(gid)
            if cached is None or cached != (ch, rl, ts):
                logger.info(f"[{guild.name}] Detected quarantine options change; ensuring setup.")
                await ensure_quarantine_setup(guild, ch, rl)
                bot.quarantine_options_cache[gid] = (ch, rl, ts)
    except Exception as e:
        logger.error(f"sync_quarantine_task error: {e}", exc_info=True)

# ---------------- HTTP endpoints ----------------

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
}

async def cors_preflight_handler(request):
    return web.Response(status=200, headers=CORS_HEADERS)

async def settings_saved_handler(request):
    """
    Existing webhook for immediate triggers (still supported).
    """
    conn = None
    guild_id = None
    try:
        data = await request.json()
        guild_id = data.get('guild_id')
        bot_entry = data.get('bot_entry')
        action = data.get('action')

        if bot_entry != BOT_ENTRY:
            return web.Response(text="Unauthorized", status=401, headers=CORS_HEADERS)

        if action == "quarantine_options_updated":
            # Read options and ensure now
            conn = await aiomysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
                db="serene_users", charset='utf8mb4', autocommit=True,
                cursorclass=aiomysql.cursors.DictCursor
            )
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT quarantine_channel_name, quarantine_role_name, "
                    "DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS updated_at "
                    "FROM bot_flag_action_options WHERE guild_id = %s",
                    (str(guild_id),)
                )
                row = await cursor.fetchone()
            if not row:
                return web.Response(text="No quarantine options for guild", status=404, headers=CORS_HEADERS)
            guild = bot.get_guild(int(guild_id))
            if not guild:
                return web.Response(text="Bot not in specified guild", status=404, headers=CORS_HEADERS)
            summary = await ensure_quarantine_setup(
                guild,
                (row["quarantine_channel_name"] or "").strip(),
                (row["quarantine_role_name"] or "").strip()
            )
            bot.quarantine_options_cache[str(guild_id)] = (
                (row["quarantine_channel_name"] or "").strip(),
                (row["quarantine_role_name"] or "").strip(),
                row.get("updated_at")
            )
            return web.Response(text=json.dumps({"ok": True, "summary": summary}), status=200, headers=CORS_HEADERS)

        # You can keep your existing "rules_updated" branch here (omitted for brevity)
        return web.Response(text="OK", status=200, headers=CORS_HEADERS)

    except Exception as e:
        logger.error(f"settings_saved_handler error: {e}", exc_info=True)
        return web.Response(text="Internal Server Error", status=500, headers=CORS_HEADERS)
    finally:
        if conn:
            conn.close() if conn else None

# ---------------- WEBSOCKETS ----------------

async def websocket_handler(request):
    """
    Existing game WS (unchanged). Expects initial message with room_id, guild_id, etc.
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
            await ws.send_json({"status": "error", "message": "Missing room, guild, channel, or sender ID."})
            return ws

        if room_id not in bot.ws_rooms:
            bot.ws_rooms[room_id] = set()
        bot.ws_rooms[room_id].add(ws)

        mechanics_cog = bot.get_cog('MechanicsMain')
        if not mechanics_cog:
            await ws.send_json({"status": "error", "message": "Game mechanics not available."})
            return ws

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
                except Exception as e:
                    await ws.send_json({"status": "error", "message": f"Internal server error: {e}"})
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"Game WS error in room {room_id}: {ws.exception()}")
            elif msg.type == web.WSMsgType.CLOSE:
                break
    finally:
        if room_id and ws in bot.ws_rooms.get(room_id, set()):
            bot.ws_rooms[room_id].remove(ws)
            if not bot.ws_rooms[room_id]:
                del bot.ws_rooms[room_id]
        return ws

async def admin_ws_handler(request):
    """
    NEW: Admin WS for instant provisioning & acks from the moderation UI.

    Protocol:
      - First message must be JSON with either:
          {"type":"auth","bot_entry":"<secret>"}  OR
          {"type":"quarantine_options_updated","guild_id":"...","bot_entry":"<secret>"}
      - Subsequent messages can be:
          {"type":"quarantine_options_updated","guild_id":"..."}  (after auth)
          {"type":"ping"}
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    authed = False

    async def require_auth(payload) -> bool:
        nonlocal authed
        sec = (payload or {}).get("bot_entry")
        if sec and sec == BOT_ENTRY:
            authed = True
            await ws.send_json({"type": "auth_ok"})
            return True
        await ws.send_json({"type": "auth_error", "error": "Unauthorized"})
        return False

    try:
        # Expect an initial message
        msg = await ws.receive()
        if msg.type == web.WSMsgType.TEXT:
            try:
                payload = json.loads(msg.data)
            except Exception:
                await ws.send_json({"type": "error", "error": "Invalid JSON"})
                return ws

            mtype = payload.get("type")

            # inline auth (either explicit or piggybacked on first action)
            if mtype == "auth":
                if not await require_auth(payload):
                    return ws
            elif mtype in ("quarantine_options_updated",):
                if not await require_auth(payload):
                    return ws
                # fall through to handle this action immediately
            else:
                await ws.send_json({"type": "error", "error": "First message must be 'auth' or an authed action"})
                return ws

            # Handle immediate action (if the first message was an action)
            if mtype == "quarantine_options_updated":
                gid = str(payload.get("guild_id") or "")
                if not gid.isdigit():
                    await ws.send_json({"type":"error","error":"Missing or invalid guild_id"})
                else:
                    await handle_quarantine_update_ws(ws, gid)
        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
            return ws

        # Main loop
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    await ws.send_json({"type": "error", "error": "Invalid JSON"})
                    continue

                if payload.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                    continue

                if payload.get("type") == "quarantine_options_updated":
                    if not authed:
                        await ws.send_json({"type":"auth_error","error":"Unauthorized"})
                        continue
                    gid = str(payload.get("guild_id") or "")
                    if not gid.isdigit():
                        await ws.send_json({"type":"error","error":"Missing or invalid guild_id"})
                        continue
                    await handle_quarantine_update_ws(ws, gid)
                    continue

                await ws.send_json({"type": "error", "error": "Unknown type"})
            else:
                # ignore non-text frames
                pass

    finally:
        return ws

async def handle_quarantine_update_ws(ws: web.WebSocketResponse, guild_id: str):
    """
    Reads the latest quarantine options for guild_id and ensures provisioning.
    Sends an ack/error back over the websocket.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            await ws.send_json({"type":"quarantine_ack","ok":False,"guild_id":guild_id,"error":"Bot not in guild"})
            return

        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db="serene_users",
            charset='utf8mb4', autocommit=True, cursorclass=aiomysql.cursors.DictCursor
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT quarantine_channel_name, quarantine_role_name, "
                "DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS updated_at "
                "FROM bot_flag_action_options WHERE guild_id = %s",
                (str(guild_id),)
            )
            row = await cursor.fetchone()
        conn.close()

        if not row:
            await ws.send_json({"type":"quarantine_ack","ok":False,"guild_id":guild_id,"error":"No options found"})
            return

        ch = (row["quarantine_channel_name"] or "").strip()
        rl = (row["quarantine_role_name"] or "").strip()
        summary = await ensure_quarantine_setup(guild, ch, rl)

        # cache
        bot.quarantine_options_cache[str(guild_id)] = (ch, rl, row.get("updated_at"))

        await ws.send_json({"type":"quarantine_ack","ok":True,"guild_id":guild_id,"summary":summary})
    except Exception as e:
        logger.error(f"WS quarantine update error: {e}", exc_info=True)
        try:
            await ws.send_json({"type":"quarantine_ack","ok":False,"guild_id":guild_id,"error":str(e)})
        except Exception:
            pass

# ---------------- Web server startup ----------------

async def start_web_server():
    bot.web_app.router.add_options('/settings_saved', cors_preflight_handler)
    bot.web_app.router.add_post('/settings_saved', settings_saved_handler)
    bot.web_app.router.add_get('/ws', websocket_handler)          # game WS (existing)
    bot.web_app.router.add_get('/admin_ws', admin_ws_handler)     # NEW admin WS

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(bot.web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on http://0.0.0.0:{port}")

# ---------------- Discord events ----------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}.")

    await load_flag_reasons()

    # expose DB creds to cogs
    bot.db_user = DB_USER
    bot.db_password = DB_PASSWORD
    bot.db_host = DB_HOST

    await load_cogs()

    # sync commands
    try:
        await bot.tree.sync()
        logger.info("✅ Globally synced all commands")
    except Exception as e:
        logger.error(f"Global sync failed: {e}")

    for guild in bot.guilds:
        try:
            await bot.tree.sync(guild=guild)
            logger.info(f"✅ Resynced commands for guild: {guild.name} ({guild.id})")
        except Exception as e:
            logger.error(f"Failed to sync commands for guild {guild.name}: {e}")

    # ensure users exist in DB
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                await add_user_to_db_if_not_exists(member.guild.id, member.display_name, member.id)

    hourly_db_check.start()
    sync_quarantine_task.start()  # safety net

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
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        logger.info("DB connection OK.")
    except Exception as e:
        logger.error(f"Hourly DB check failed: {e}")
    finally:
        if conn:
            conn.close()

# ---------------- Cog loader ----------------

async def load_cogs():
    if not os.path.exists("cogs"):
        os.makedirs("cogs")

    ordered_cogs = ["mechanics_main", "communication_main"]
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
                logger.warning(f"Skipping module {full_module_name}: No 'setup' function found, not a Discord cog.")
        except ModuleNotFoundError:
            logger.error(f"Prioritized cog {full_module_name} not found.")
        except Exception as e:
            logger.error(f"Failed to load prioritized cog {full_module_name}: {e}")

    for root, dirs, files in os.walk("cogs"):
        for filename in files:
            if filename.endswith(".py") and filename != "__init__.py":
                relative_path = os.path.relpath(os.path.join(root, filename), start="cogs")
                full_module_name = f"cogs.{relative_path[:-3].replace(os.sep, '.')}"
                if ' ' in full_module_name:
                    logger.warning(f"Skipping cog '{full_module_name}' due to invalid characters (spaces) in module name.")
                    continue
                if full_module_name not in loaded_cogs_set:
                    try:
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

# ---------------- Entrypoint ----------------

async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing")
        return
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
