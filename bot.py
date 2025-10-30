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
import re
from typing import List, Optional, Tuple
import html  # <-- NEW: for HTML-escaping when sending Serene questions

# Load env vars
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")

# URLs used by your site features
GAME_WEB_URL = os.getenv("GAME_WEB_URL", "https://serenekeks.com/game_room.php")
GAME_WEBHOOK_URL = os.getenv("GAME_WEBHOOK_URL", "https://serenekeks.com/game_update_webhook.php")  # (fixed env var name typo)

# Auth token used by your admin page to talk to the bot
BOT_ENTRY = os.getenv("BOT_ENTRY")

BOT_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Access logging middleware (logs ALL HTTP requests, including 404s) ---
@web.middleware
async def access_log_mw(request, handler):
    start = time.time()
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        elapsed = (time.time() - start) * 1000
        logging.info(f"HTTP {request.method} {request.path_qs} -> {ex.status} in {elapsed:.1f}ms from {request.remote}")
        raise
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        logging.error(f"HTTP {request.method} {request.path_qs} -> 500 in {elapsed:.1f}ms from {request.remote}: {e}", exc_info=True)
        raise
    else:
        elapsed = (time.time() - start) * 1000
        status = getattr(resp, "status", 0)
        logging.info(f"HTTP {request.method} {request.path_qs} -> {status} in {elapsed:.1f}ms from {request.remote}")
        return resp

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# Attach aiohttp app with middleware so cogs can add routes if they want
bot.web_app = web.Application(middlewares=[access_log_mw])

# ---- CRUCIAL: create /serene group BEFORE loading cogs ----
serene_group = app_commands.Group(name="serene", description="The main Serene bot commands.")
bot.tree.add_command(serene_group)
# Optional: expose it so cogs can fetch it directly if they prefer
bot.serene_group = serene_group

# --- WebSocket room registries (game state & chat) ---
bot.ws_rooms = {}
bot.chat_ws_rooms = {}

# --- Online session tracking for kekchipz rewards ---
# { (guild_id:int, user_id:int): {"start": float_unix, "last_award": float_unix} }
online_sessions = {}

def _kekchipz_rate_for_minute(online_minutes: int) -> int:
    """
    Per-minute reward rate based on continuous online time.

    < 30 min:        1/min
    30–59 min:       2/min
    60–89 min:       3/min
    90–119 min:      4/min
    120–179 min:     4/min
    180+ min:        5/min
    """
    if online_minutes < 30:
        return 1
    elif online_minutes < 60:
        return 2
    elif online_minutes < 90:
        return 3
    elif online_minutes < 180:
        return 4
    else:
        return 5

# ---------------- Utility helpers ----------------

def _slugify_channel_name(name: str) -> str:
    """
    Discord sanitizes channel names: lowercase, spaces -> '-', only [a-z0-9-_].
    """
    if not name:
        return ""
    s = name.lower()
    s = s.replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-_]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-_")

def _normalize_role_name_variants(name: str) -> List[str]:
    """
    Build several variants to help fuzzy match a role by name.
    Roles in Discord CAN contain spaces, but we'll be robust in case a slug
    or spacing variant is saved in DB.
    """
    if not name:
        return []
    base = name.strip()
    alts = {base, base.lower()}
    # swap spaces <-> hyphens
    alts.add(base.replace("-", " "))
    alts.add(base.replace(" ", "-"))
    alts.add(base.lower().replace("-", " "))
    alts.add(base.lower().replace(" ", "-"))
    # collapse multiple spaces
    alts.add(re.sub(r"\s{2,}", " ", base))
    return list(alts)

def _find_role_fuzzy(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
    """
    Try to resolve a role by exact, case-insensitive, and spacing/slug variants.
    """
    if not role_name or not guild:
        return None

    # 1) exact
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        return role

    # 2) case-insensitive
    lowered = role_name.lower()
    for r in guild.roles:
        if r.name.lower() == lowered:
            return r

    # 3) spacing / hyphen variants
    for variant in _normalize_role_name_variants(role_name):
        role = discord.utils.get(guild.roles, name=variant)
        if role:
            return role

    # 4) fallback: normalize spaces->hyphens
    for r in guild.roles:
        if r.name.lower().replace(" ", "-") == lowered.replace(" ", "-"):
            return r

    return None

def _find_text_channel_fuzzy(guild: discord.Guild, channel_name: str) -> Optional[discord.TextChannel]:
    """
    Try to resolve a text channel by exact and slugified variants.
    """
    if not channel_name or not guild:
        return None

    # exact name match
    ch = discord.utils.get(guild.text_channels, name=channel_name)
    if ch:
        return ch

    # slugified (what Discord would do to a requested name)
    slug = _slugify_channel_name(channel_name)
    if slug:
        ch = discord.utils.get(guild.text_channels, name=slug)
        if ch:
            return ch

    # case-insensitive attempt
    lowered = channel_name.lower()
    for c in guild.text_channels:
        if c.name.lower() == lowered or c.name == _slugify_channel_name(lowered):
            return c

    return None

def _merge_role_overwrite(existing: Optional[discord.PermissionOverwrite], **kwargs) -> discord.PermissionOverwrite:
    """
    Merge or create an overwrite, only changing the keys we pass in.
    """
    ow = existing or discord.PermissionOverwrite()
    for k, v in kwargs.items():
        setattr(ow, k, v)
    return ow

async def _enforce_quarantine_visibility(
    guild: discord.Guild,
    quarantine_role: discord.Role,
    quarantine_channel: discord.TextChannel
):
    """
    Lock the server down for the quarantine role:
      • DENY view/send/etc on EVERY category/channel,
      • EXCEPT explicitly ALLOW in the quarantine channel.
    This overrides @everyone allows on a fresh server.
    """
    # 1) Categories: deny view_channel for the quarantine role
    for cat in guild.categories:
        try:
            current = cat.overwrites_for(quarantine_role)
            new_ow = _merge_role_overwrite(current, view_channel=False)
            await cat.set_permissions(quarantine_role, overwrite=new_ow, reason="Serene quarantine: hide categories")
        except discord.Forbidden:
            logger.warning(f"Missing perms to edit category {cat} for quarantine overwrites.")
        except Exception as e:
            logger.error(f"Error setting category overwrite {cat}: {e}", exc_info=True)

    # 2) Text channels: deny everywhere except quarantine channel
    for ch in guild.text_channels:
        try:
            if ch.id == quarantine_channel.id:
                # Explicitly allow in the quarantine channel
                current = ch.overwrites_for(quarantine_role)
                new_ow = _merge_role_overwrite(
                    current,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    add_reactions=True
                )
                await ch.set_permissions(quarantine_role, overwrite=new_ow, reason="Serene quarantine: allow quarantine channel")
            else:
                current = ch.overwrites_for(quarantine_role)
                new_ow = _merge_role_overwrite(
                    current,
                    view_channel=False,
                    send_messages=False,
                    add_reactions=False,
                    create_public_threads=False,
                    create_private_threads=False,
                    send_messages_in_threads=False,
                    attach_files=False,
                    embed_links=False,
                )
                await ch.set_permissions(quarantine_role, overwrite=new_ow, reason="Serene quarantine: deny non-quarantine channel")
        except discord.Forbidden:
            logger.warning(f"Missing perms to edit channel {ch} for quarantine overwrites.")
        except Exception as e:
            logger.error(f"Error setting channel overwrite {ch}: {e}", exc_info=True)

    # 3) Voice/Stage channels: deny view/connect just in case
    for vch in guild.voice_channels + guild.stage_channels:
        try:
            current = vch.overwrites_for(quarantine_role)
            new_ow = _merge_role_overwrite(
                current,
                view_channel=False,
                connect=False,
                speak=False,
                stream=False
            )
            await vch.set_permissions(quarantine_role, overwrite=new_ow, reason="Serene quarantine: deny voice/stage")
        except discord.Forbidden:
            logger.warning(f"Missing perms to edit voice/stage channel {vch} for quarantine overwrites.")
        except Exception as e:
            logger.error(f"Error setting voice/stage overwrite {vch}: {e}", exc_info=True)

async def _db_connect_dict():
    return await aiomysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db="serene_users",
        charset='utf8mb4',
        autocommit=True,
        cursorclass=aiomysql.cursors.DictCursor
    )

async def _fetch_quarantine_options(guild_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (role_name, channel_name) from bot_flag_action_options, or (None, None)
    """
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        return (None, None)
    conn = None
    try:
        conn = await _db_connect_dict()
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT quarantine_channel_name, quarantine_role_name "
                "FROM bot_flag_action_options WHERE guild_id = %s",
                (str(guild_id),)
            )
            row = await cursor.fetchone()
            if not row:
                return (None, None)
            return (row.get("quarantine_role_name"), row.get("quarantine_channel_name"))
    except Exception as e:
        logger.error(f"_fetch_quarantine_options error for guild {guild_id}: {e}")
        return (None, None)
    finally:
        if conn:
            conn.close()

async def _fetch_rules_embed_for_guild(guild_id: str) -> Optional[discord.Embed]:
    """
    Load the saved embed JSON from bot_messages.message and return a discord.Embed.
    Uses the first embed if an array is stored.
    """
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        return None
    conn = None
    try:
        conn = await _db_connect_dict()
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT message FROM bot_messages WHERE guild_id = %s",
                (str(guild_id),)
            )
            row = await cursor.fetchone()

            if not row:
                return None

            raw = row.get("message")
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")

            if not raw:
                return None

            try:
                data = json.loads(raw)
                # Accept either a single embed dict or a list[dict]
                if isinstance(data, list) and data:
                    embed_dict = data[0]
                elif isinstance(data, dict):
                    embed_dict = data
                else:
                    return None
                return discord.Embed.from_dict(embed_dict)
            except Exception as e:
                logger.error(f"Failed to parse saved embed JSON for guild {guild_id}: {e}")
                return None
    except Exception as e:
        logger.error(f"_fetch_rules_embed_for_guild DB error for guild {guild_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

async def _fetch_member_saved_roles(guild_id: str, discord_id: str) -> List[int]:
    """
    Returns list of role IDs (ints) stored in discord_users.role_data, excluding @everyone and quarantine.
    """
    if not all([DB_USER, DB_PASSWORD, DB_HOST]):
        return []
    conn = None
    roles: List[int] = []
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
                "SELECT role_data FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            row = await cursor.fetchone()
            if not row:
                return []
            raw = row[0]
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            try:
                jd = json.loads(raw) if isinstance(raw, str) else (raw or {})
                arr = jd.get("roles", []) if isinstance(jd, dict) else []
                for rid in arr:
                    try:
                        roles.append(int(rid))
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"Failed to parse role_data for user {discord_id} in {guild_id}: {e}")
                return []
    except Exception as e:
        logger.error(f"_fetch_member_saved_roles DB error: {e}")
    finally:
        if conn:
            conn.close()
    return roles

async def _restore_member_roles(member: discord.Member, quarantine_role: Optional[discord.Role]):
    """
    Remove the quarantine role (if present) and restore roles from DB role_data.
    """
    guild = member.guild
    if not guild:
        return

    # Remove quarantine role first
    try:
        if quarantine_role and quarantine_role in member.roles:
            await member.remove_roles(quarantine_role, reason="Accepted rules")
    except discord.Forbidden:
        logger.error(f"Missing permissions to remove quarantine role from {member}.")
    except Exception as e:
        logger.error(f"Error removing quarantine role from {member}: {e}")

    # Fetch saved role IDs
    saved_ids = await _fetch_member_saved_roles(str(guild.id), str(member.id))
    if not saved_ids:
        return

    # Map to Role objects, filter out quarantine role & unmanaged / above bot
    me = guild.me
    roles_to_add: List[discord.Role] = []
    for rid in saved_ids:
        r = guild.get_role(rid)
        if not r:
            continue
        if quarantine_role and r.id == quarantine_role.id:
            continue
        # Only add roles we can manage
        if r.managed:
            continue
        if me and r >= me.top_role:
            continue
        roles_to_add.append(r)

    if not roles_to_add:
        return

    try:
        await member.add_roles(*roles_to_add, reason="Restore roles after accepting rules")
    except discord.Forbidden:
        logger.error(f"Missing permissions to add roles to {member}.")
    except Exception as e:
        logger.error(f"Error adding roles to {member}: {e}")

# ---------------- Views (Buttons) ----------------

class AcceptRulesView(discord.ui.View):
    """
    Persistent view for the "I Accept" button in the quarantine channel.
    Clicking it removes the quarantine role and restores saved roles.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I Accept", style=discord.ButtonStyle.success, custom_id="serene:accept_rules")
    async def accept_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild = interaction.guild
            if not guild or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("Something went wrong (no guild or member).", ephemeral=True)
                return

            # Resolve quarantine role by reading the configured name and fuzzy matching
            qrole_name, _ = await _fetch_quarantine_options(str(guild.id))
            quarantine_role = _find_role_fuzzy(guild, qrole_name or "")

            await _restore_member_roles(interaction.user, quarantine_role)

            # Acknowledge
            await interaction.response.send_message("✅ You're all set! Welcome back to the server.", ephemeral=True)
        except Exception as e:
            logger.error(f"accept_rules error: {e}", exc_info=True)
            # Try to at least notify the user
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Sorry, something went wrong restoring your roles.", ephemeral=True)
            except Exception:
                pass

async def _seed_quarantine_readme_message(guild: discord.Guild, channel: discord.TextChannel):
    """
    Posts the saved rules embed into the quarantine channel with an 'I Accept' button.
    Called when we just created the quarantine channel.
    """
    try:
        embed = await _fetch_rules_embed_for_guild(str(guild.id))
        if not embed:
            # Minimal fallback if nothing saved
            embed = discord.Embed(
                title="Read Me",
                description="Please review the server rules below and click **I Accept** to continue.",
                color=discord.Color.blurple()
            )
    except Exception:
        # if the DB lookup failed or was invalid, still send a fallback
        embed = discord.Embed(
            title="Read Me",
            description="Please review the server rules below and click **I Accept** to continue.",
            color=discord.Color.blurple()
        )

    try:
        view = AcceptRulesView()
        await channel.send(content="**Read Me**", embed=embed, view=view)
        logger.info(f"Seeded quarantine 'Read Me' message in #{channel} for guild {guild.id}")
    except discord.Forbidden:
        logger.error("Missing permission to send the 'Read Me' message in quarantine channel.")
    except Exception as e:
        logger.error(f"Failed to seed 'Read Me' message: {e}", exc_info=True)

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

# ---------------------- Health & Probe endpoints ----------------------

async def health(_):
    return web.json_response({"ok": True, "ts": int(time.time())})

async def game_was_probe(_):
    return web.Response(text="game_was endpoint is here; use WebSocket upgrade.", status=426)

# ===================== SERENE INTEGRATION HELPERS (NEW) =====================

# Config for Serene
SERENE_BOT_URL = "https://serenekeks.com/serene_bot.php"
SERENE_WORD_RE = re.compile(r"\bserene\b", re.IGNORECASE)

# Shared HTTP client for Serene calls
_serene_http_session: Optional[aiohttp.ClientSession] = None

def _get_serene_http() -> aiohttp.ClientSession:
    global _serene_http_session
    if _serene_http_session is None or _serene_http_session.closed:
        _serene_http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    return _serene_http_session

async def _serene_post(data: dict) -> Optional[str]:
    """
    POST to Serene and return response text, or None on error / empty / 'false'.
    """
    try:
        session = _get_serene_http()
        async with session.post(SERENE_BOT_URL, data=data) as resp:
            if resp.status != 200:
                logger.warning("Serene HTTP %s for data=%s", resp.status, data)
                return None
            txt = (await resp.text()).strip()
            if not txt or txt.lower() == "false":
                return None
            return txt
    except asyncio.TimeoutError:
        logger.warning("Serene request timed out: %s", data)
        return None
    except aiohttp.ClientError as e:
        logger.warning("Serene client error: %s", e)
        return None
    except Exception:
        logger.exception("Unexpected error posting to Serene")
        return None

async def _broadcast_room_json_str(room_id: str, payload: dict):
    """
    Broadcast JSON (string) to all clients in chat room. Safe if room disappears.
    """
    try:
        room = bot.chat_ws_rooms.get(room_id) or set()
        msg = json.dumps(payload)
        for client_ws in list(room):
            try:
                await client_ws.send_str(msg)
            except Exception:
                try:
                    room.discard(client_ws)
                except Exception:
                    pass
    except Exception:
        logger.exception("Broadcast error for room %s", room_id)

async def _serene_start(room_id: str, display_name: str):
    reply = await _serene_post({"start": "true", "player": display_name})
    if reply:
        payload = {
            "type": "new_message",
            "displayName": "Serene",
            "message": reply,
            "room_id": room_id,
            "senderType": "bot",
            "botId": "serene",
        }
        await _broadcast_room_json_str(room_id, payload)

async def _serene_question(room_id: str, display_name: str, question_raw: str):
    safe_q = html.escape(question_raw or "", quote=True)
    reply = await _serene_post({"question": safe_q, "player": display_name})
    if reply:
        payload = {
            "type": "new_message",
            "displayName": "Serene",
            "message": reply,
            "room_id": room_id,
            "senderType": "bot",
            "botId": "serene",
        }
        await _broadcast_room_json_str(room_id, payload)

# ---------------------- CHAT WS: /chat_ws (current + Serene) ----------------------
async def chat_websocket_handler(request):
    """
    Handles chat WebSocket connections for game lobbies and rooms.

    Preserves existing behavior:
      • initial TEXT with {"room_id","displayName"}
      • broadcast user_joined / user_left
      • rebroadcast every user message as type="new_message"

    Adds Serene:
      • If a message contains the word 'serene' (case-insensitive), post {start:true, player}
        and mark the NEXT message from that same socket as the Serene question.
      • That next message is posted as {question:<html-escaped>, player}.
      • Serene replies are broadcast with senderType="bot", botId="serene".
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Log once the upgrade succeeded – helpful in deployment logs
    logger.info("✅ [/chat_ws] WebSocket upgraded successfully from %s", request.remote)

    room_id = None
    display_name = None

    # Track whether this specific socket's next message is a Serene question
    awaiting_serene_question = False

    try:
        # 1. Handle initial registration message (unchanged)
        first_msg_str = await ws.receive_str()
        initial_data = json.loads(first_msg_str)
        room_id = initial_data.get('room_id')
        display_name = initial_data.get('displayName')

        if not room_id or not display_name:
            logger.error(f"Chat WS initial frame missing room_id or displayName: {initial_data}")
            await ws.close()
            return ws

        # 2. Add user to the chat room registry (unchanged)
        if room_id not in bot.chat_ws_rooms:
            bot.chat_ws_rooms[room_id] = set()
        bot.chat_ws_rooms[room_id].add(ws)
        logger.info(f"'{display_name}' connected to chat room '{room_id}'.")

        # 3. Announce user joining (unchanged)
        join_message = {
            "type": "user_joined",
            "displayName": display_name,
            "room_id": room_id
        }
        await _broadcast_room_json_str(room_id, join_message)

        # 4. Listen for and broadcast messages (now with Serene)
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # Parse incoming JSON
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if 'message' in data:
                    user_text = str(data['message'])

                    # (A) Broadcast user's message as-is (existing behavior)
                    broadcast_message = {
                        "type": "new_message",
                        "displayName": display_name,
                        "message": user_text,
                        "room_id": room_id
                    }
                    await _broadcast_room_json_str(room_id, broadcast_message)

                    # (B) Serene: if awaiting a question from this socket, post it
                    if awaiting_serene_question:
                        awaiting_serene_question = False  # clear first to avoid re-entrancy
                        asyncio.create_task(_serene_question(room_id, display_name, user_text))

                    # (C) Serene: trigger start if message mentions 'serene', and arm next msg as question
                    if SERENE_WORD_RE.search(user_text or ""):
                        awaiting_serene_question = True
                        asyncio.create_task(_serene_start(room_id, display_name))

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"Chat WS error for '{display_name}' in room '{room_id}': {ws.exception()}")

    except Exception as e:
        logger.error(f"Chat WS handler error for '{display_name}' in room '{room_id}': {e}", exc_info=True)
    finally:
        # 5. Handle disconnection (unchanged)
        try:
            if room_id and ws in bot.chat_ws_rooms.get(room_id, set()):
                bot.chat_ws_rooms[room_id].remove(ws)
                if not bot.chat_ws_rooms[room_id]:
                    del bot.chat_ws_rooms[room_id]

                logger.info(f"'{display_name}' disconnected from chat room '{room_id}'.")

                # Announce user leaving
                leave_message = {
                    "type": "user_left",
                    "displayName": display_name,
                    "room_id": room_id
                }
                if room_id in bot.chat_ws_rooms:
                    await _broadcast_room_json_str(room_id, leave_message)
        finally:
            return ws


# ---------------------- GAME WS: /game_was (robust + loud logs) ----------------------
async def game_was_handler(request):
    """
    Game WebSocket: registers a player's presence in a room via MechanicsMain.
    - Robust initial handshake (waits for first TEXT frame; ignores ping/pong/binary/close).
    - After handshake, forwards any JSON frames with an 'action' to MechanicsMain.handle_websocket_game_action.
    - Uses MechanicsMain.register_ws_connection/unregister_ws_connection so room keys are normalized.
    - Does NOT hard-close on mechanics/DB failures; it warns the client and keeps the WS open.
    - Loud logs before/after prepare() to confirm upgrade attempts.
    """
    logger.info("[/game_was] HTTP request received from %s (will attempt WS upgrade)", request.remote)

    ws = web.WebSocketResponse()
    try:
        ok = await ws.prepare(request)
        logger.info("[/game_was] prepare() returned %s — upgrade %s", ok, "OK" if ok else "FAILED")
    except Exception as e:
        logger.error(f"[/game_was] prepare() raised: {e}", exc_info=True)
        return web.Response(text="Upgrade failed", status=400)

    logger.info("✅ [/game_was] WebSocket upgraded successfully from %s — endpoint is live", request.remote)

    room_id = None
    sender_id = None
    guild_id = None  # captured from client payload if provided
    mechanics_cog = None
    presence_persisted = False  # track whether player_connect succeeded
    registered_in_bucket = False

    try:
        # --- 1) Robust initial handshake: wait for TEXT JSON ---
        first_msg_str = None
        while True:
            msg = await ws.receive()

            if msg.type == web.WSMsgType.TEXT:
                first_msg_str = msg.data
                break
            elif msg.type in (web.WSMsgType.PING, web.WSMsgType.PONG):
                continue
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                logger.info("[/game_was] Client closed before sending initial TEXT handshake.")
                await ws.close()
                return ws
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"[/game_was] WS error before handshake: {ws.exception()}")
                await ws.close()
                return ws

        # --- 2) Parse JSON payload: expect room_id & sender_id (+ guild_id optional) ---
        try:
            initial_data = json.loads(first_msg_str)
        except json.JSONDecodeError:
            logger.error(f"[/game_was] Malformed initial JSON: {first_msg_str!r}")
            await ws.send_str(json.dumps({"status": "error", "message": "Malformed initial JSON."}))
            await ws.close()
            return ws

        room_id = initial_data.get('room_id')
        sender_id = initial_data.get('sender_id')
        guild_id = initial_data.get('guild_id')
        channel_id = initial_data.get('channel_id')

        if not room_id or not sender_id:
            logger.error(f"[/game_was] Initial WS message missing room_id or sender_id: {initial_data}")
            await ws.send_str(json.dumps({"status": "error", "message": "Missing room_id or sender_id."}))
            await ws.close()
            return ws

        # --- 3) Add to in-memory presence registry immediately (normalized) ---
        mechanics_cog = bot.get_cog('MechanicsMain')
        if mechanics_cog:
            registered_in_bucket = mechanics_cog.register_ws_connection(ws, room_id)
            if not registered_in_bucket:
                await ws.send_str(json.dumps({"status": "error", "message": "Room id invalid."}))
                await ws.close()
                return ws
        
        # --- NEW BLOCK: Proactively send the current game state to the new client ---
        if mechanics_cog:
            try:
                # Load the state directly from the database
                state = await mechanics_cog._load_game_state(room_id)
                if state:
                    # Construct the same envelope the broadcast function uses
                    envelope = {"game_state": state, "server_ts": int(time.time())}
                    # Send the state directly to the newly connected client
                    await ws.send_str(json.dumps(envelope))
                    logger.info(f"Sent initial game_state for room '{room_id}' to new client {sender_id}.")
            except Exception as e:
                logger.error(f"Failed to send initial game state for room '{room_id}': {e}", exc_info=True)
        # --- END NEW BLOCK ---

        # --- 5) Main receive loop: keepalive + DISPATCH GAME ACTIONS ---
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                raw = msg.data
                if raw == '{"action":"ping"}' or raw.strip().lower() == 'ping':
                    await ws.send_str('{"action":"pong"}')
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if isinstance(data, dict) and "action" in data:
                    if mechanics_cog:
                        try:
                            data.setdefault("room_id", room_id)
                            data.setdefault("sender_id", sender_id)
                            if guild_id is not None: data.setdefault("guild_id", guild_id)
                            if channel_id is not None: data.setdefault("channel_id", channel_id)
                            await mechanics_cog.handle_websocket_game_action(data)
                        except Exception as e:
                            logger.error(f"[/game_was] Dispatch error for action={data.get('action')} r={room_id}: {e}", exc_info=True)
            
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                logger.info(f"[/game_was] Closing for player {sender_id} in room {room_id}. Reason: {msg.type.name}")
                break
    
    except Exception as e:
        logger.error(f"[/game_was] Handler error for room {room_id}: {e}", exc_info=True)
    
    finally:
        # --- 6) Cleanup ---
        if registered_in_bucket and mechanics_cog:
            mechanics_cog.unregister_ws_connection(ws)
        return ws

# ---------------------- AVATAR WS: /avatar_ws ----------------------

async def _resolve_member_avatar(guild_id: int, user_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (avatar_url, display_name) for a member, or (None, None) if not found.
    Uses cache first; falls back to API fetch. Provides a safe default avatar if needed.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return (None, None)

        member: Optional[discord.Member] = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except Exception:
                member = None

        if not member:
            return (None, None)

        # Preferred: display_avatar (handles server avatar / global avatar / default)
        try:
            asset = member.display_avatar
            if hasattr(asset, "with_size"):
                url = asset.with_size(128).url
            else:
                url = str(asset.url)
        except Exception:
            url = getattr(getattr(member, "avatar", None), "url", None)

        if not url:
            url = "https://cdn.discordapp.com/embed/avatars/0.png"

        display_name = getattr(member, "display_name", None) or getattr(member, "name", None) or str(member.id)
        return (str(url), str(display_name))
    except Exception:
        return (None, None)

async def avatar_ws_handler(request):
    """
    Lightweight WS for resolving Discord avatar URLs.
    Usage:
      send {"op":"get_avatar","guild_id":"123","discord_id":"456"}
      or   {"op":"get_avatar","guild_id":"123","discord_ids":["456","789"]}
    Replies one message per requested user with type="avatar".
    Keeps the socket open for multiple requests; client may close anytime.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("✅ [/avatar_ws] WebSocket upgraded successfully from %s", request.remote)

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue

            # Parse JSON
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid_json"})
                continue

            # Allow simple ping
            if isinstance(data, str) and data.lower() == "ping":
                await ws.send_str("pong")
                continue

            if not isinstance(data, dict):
                await ws.send_json({"type": "error", "error": "invalid_payload"})
                continue

            op = data.get("op") or "get_avatar"  # default to get_avatar for back-compat
            if op != "get_avatar":
                await ws.send_json({"type": "error", "error": "unknown_op"})
                continue

            guild_id = data.get("guild_id")
            one_id = data.get("discord_id")
            many_ids = data.get("discord_ids")

            if not guild_id or (not one_id and not many_ids):
                await ws.send_json({"type": "error", "error": "missing_fields"})
                continue

            # Normalize to a list
            ids: List[str] = []
            if one_id:
                ids.append(str(one_id))
            if isinstance(many_ids, list):
                ids.extend([str(x) for x in many_ids if x is not None])

            # Resolve each and emit a per-user response
            for uid in ids:
                avatar_url, display_name = await _resolve_member_avatar(int(guild_id), int(uid))
                if avatar_url:
                    await ws.send_json({
                        "type": "avatar",
                        "ok": True,
                        "guild_id": str(guild_id),
                        "discord_id": str(uid),
                        "display_name": display_name,
                        "avatar_url": avatar_url
                    })
                else:
                    await ws.send_json({
                        "type": "avatar",
                        "ok": False,
                        "guild_id": str(guild_id),
                        "discord_id": str(uid),
                        "error": "not_found"
                    })

    except Exception as e:
        logger.error(f"[/avatar_ws] error: {e}", exc_info=True)
    finally:
        return ws

# ---------------------- ADMIN WS: /admin_ws ----------------------

async def ensure_quarantine_objects(guild_id: str, role_name: str, channel_name: str) -> bool:
    """Create or update the quarantine role & channel for the guild.
        When a new channel is created, seed it with the saved rules embed + Accept button.
        Also ensure the quarantine role is hidden from every other channel/category."""
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            logger.error(f"ensure_quarantine_objects: Bot not in guild {guild_id}")
            return False

        # Role: create if missing (be robust about lookups)
        role = _find_role_fuzzy(guild, role_name)
        if not role:
            role = await guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                reason="Provision quarantine role"
            )
            logger.info(f"Created role '{role_name}' in guild {guild_id}")

        # Channel: create or update with proper overwrites for quarantine channel itself
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True
            )
        }

        # Try fuzzy find channel (Discord may slugify)
        channel = _find_text_channel_fuzzy(guild, channel_name)

        created = False
        if not channel:
            # Create with slug-like name for consistency
            desired_name = _slugify_channel_name(channel_name) or channel_name
            channel = await guild.create_text_channel(
                desired_name,
                overwrites=overwrites,
                reason="Provision quarantine channel"
            )
            created = True
            logger.info(f"Created channel '{channel.name}' in guild {guild_id}")
        else:
            await channel.edit(overwrites=overwrites, reason="Ensure quarantine channel permissions")
            logger.info(f"Updated channel '{channel.name}' overwrites in guild {guild_id}")

        # Enforce deny on all other channels/categories for the quarantine role
        await _enforce_quarantine_visibility(guild, role, channel)

        # If newly created, seed the Read Me message with Accept button
        if created and isinstance(channel, discord.TextChannel):
            await _seed_quarantine_readme_message(guild, channel)

        return True
    except discord.Forbidden:
        logger.error("Missing permissions to create/edit roles or channels.")
        return False
    except Exception as e:
        logger.error(f"ensure_quarantine_objects error: {e}", exc_info=True)
        return False

# Expose to other modules (e.g., flag.py) so they can enforce after assigning the role
bot.ensure_quarantine_objects = ensure_quarantine_objects

async def admin_ws_handler(request):
    """
    Admin WebSocket used by your site (wss://sbot.serenekeks.com/admin_ws).
    Expects JSON frames like:
      {"op":"provision_quarantine","bot_entry":"<BOT_ENTRY>","guild_id":"123",
       "quarantine_channel_name":"oops","quarantine_role_name":"Rule-Breaker"}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Success log on upgrade
    logger.info("✅ [/admin_ws] WebSocket upgraded successfully from %s", request.remote)

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

def _log_registered_routes(app: web.Application):
    """Helper to dump the route table in a concise way for Railway logs."""
    try:
        lines = []
        for r in app.router.routes():
            method = getattr(r, 'method', None) or ",".join(sorted(getattr(r, 'methods', []) or []))
            path = getattr(getattr(r, 'resource', None), 'canonical', None) or str(r.resource)
            lines.append(f"    • {method:<6} {path}")
        if lines:
            logger.info("📡 aiohttp routes registered:\n%s", "\n".join(lines))
    except Exception as e:
        logger.warning(f"Could not log route table: {e}")

async def _on_web_started(app: web.Application):
    """Called after the site starts—ideal place to announce readiness."""
    _log_registered_routes(app)
    logger.info("✅ aiohttp web server is fully started and routing is active.")

async def start_web_server():
    """Starts the aiohttp web server."""
    # REST + CORS
    bot.web_app.router.add_options('/settings_saved', cors_preflight_handler)
    logger.info("🛠️  Registered OPTIONS route: /settings_saved")

    bot.web_app.router.add_post('/settings_saved', settings_saved_handler)
    logger.info("🛠️  Registered POST route: /settings_saved")

    # Health & probe
    bot.web_app.router.add_get('/healthz', health)
    logger.info("🛠️  Registered GET route: /healthz")
    bot.web_app.router.add_get('/game_was_probe', game_was_probe)
    logger.info("🛠️  Registered GET route: /game_was_probe")

    # WS endpoints
    bot.web_app.router.add_get('/game_was', game_was_handler)
    logger.info("🛠️  Registered WebSocket route: /game_was")

    bot.web_app.router.add_get('/chat_ws', chat_websocket_handler)  # Chat WS
    logger.info("🛠️  Registered WebSocket route: /chat_ws")

    bot.web_app.router.add_get('/admin_ws', admin_ws_handler)
    logger.info("🛠️  Registered WebSocket route: /admin_ws")

    bot.web_app.router.add_get('/avatar_ws', avatar_ws_handler)
    logger.info("🛠️  Registered WebSocket route: /avatar_ws")

    # Log routes again once the server is started and accepting connections
    bot.web_app.on_startup.append(_on_web_started)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(bot.web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🚀 Web server started on http://0.0.0.0:{port} (PORT={port})")

# ---------------------- Discord events ----------------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}.")

    # Register persistent views so the button keeps working across restarts
    try:
        bot.add_view(AcceptRulesView())
    except Exception as e:
        logger.error(f"Failed to add persistent AcceptRulesView: {e}")

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

    # Start reward loop (kekchipz)
    try:
        if not award_kekchipz_loop.is_running():
            award_kekchipz_loop.start()
            logger.info("✅ Started award_kekchipz_loop")
    except Exception as e:
        logger.error(f"Failed to start award_kekchipz_loop: {e}")

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

# reset continuous session if user goes offline/invisible
@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.bot:
        return
    key = (after.guild.id, after.id)
    if after.status == discord.Status.offline:
        online_sessions.pop(key, None)

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

# ---------------------- Rewards loop ----------------------

@tasks.loop(seconds=60)
async def award_kekchipz_loop():
    """
    Every minute, iterate all guild members and award kekchipz to those currently online.
    Tiers:
      < 30 min:        +1/min
      30–59 min:       +2/min
      60–89 min:       +3/min
      90–119 min:      +4/min
      120–179 min:     +4/min
      180+ min:        +5/min
    Resets when user goes offline.
    """
    now = time.time()
    increments = {}  # {(guild_id, user_id): delta}

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue

            # Consider online, idle, dnd as "online"
            if member.status not in (discord.Status.online, discord.Status.idle, discord.Status.dnd):
                online_sessions.pop((guild.id, member.id), None)
                continue

            key = (guild.id, member.id)
            sess = online_sessions.get(key)
            if not sess:
                online_sessions[key] = {"start": now, "last_award": now}
                continue

            start_ts = sess["start"]
            last_award_ts = sess["last_award"]
            minutes_due = int((now - last_award_ts) // 60)
            if minutes_due <= 0:
                continue

            # Cap catch-up to avoid huge spikes if the loop stalls
            minutes_to_process = min(minutes_due, 10)
            delta = 0

            for i in range(minutes_to_process):
                online_minutes = int((last_award_ts - start_ts) // 60) + i
                delta += _kekchipz_rate_for_minute(online_minutes)

            increments[key] = increments.get(key, 0) + delta
            online_sessions[key]["last_award"] = last_award_ts + (minutes_to_process * 60)

    if not increments:
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            db="serene_users", charset='utf8mb4', autocommit=True
        )
        async with conn.cursor() as cursor:
            for (guild_id, user_id), add_amount in increments.items():
                try:
                    await cursor.execute(
                        "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE guild_id = %s AND discord_id = %s",
                        (add_amount, str(guild_id), str(user_id))
                    )
                except Exception as e:
                    logger.error(f"Failed to update kekchipz for {user_id} in {guild_id}: {e}")
    except Exception as e:
        logger.error(f"DB error during award_kekchipz_loop: {e}")
    finally:
        if conn:
            conn.close()

# ---------------------- DB helper methods ----------------------

async def add_user_to_db_if_not_exists(guild_id, user_name, discord_id):
    """
    Ensure a user exists in discord_users. On first insert, also capture their current roles
    and store them in role_data as JSON: {"roles": ["<role_id>", ...]} (excluding @everyone).
    Also initializes current_room_id to NULL (meaning: not in any room).
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
                    "INSERT INTO discord_users (guild_id, user_name, discord_id, kekchipz, json_data, role_data, current_room_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (str(guild_id), user_name, str(discord_id), 2000, initial_json_data, role_data_json, None)  # None -> NULL
                )
                logger.info(f"Added new user '{user_name}' to DB with 2000 kekchipz, role_data={role_data_json}, current_room_id=NULL.")
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
