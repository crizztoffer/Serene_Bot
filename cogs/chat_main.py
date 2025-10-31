import os
import json
import time
import asyncio
import logging
from aiohttp import web
import aiohttp
import re
import discord
from discord.ext import commands
import html  # for HTML-escaping the question text
from typing import Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SOUND_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?\s*$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

# Serene config
SERENE_BOT_URL = "https://serenekeks.com/serene_bot.php"
SERENE_DISPLAY_NAME = "Serene"
SERENE_WORD_RE = re.compile(r"\bserene\b", re.IGNORECASE)
HAIL_SERENE_RE = re.compile(r"\bhail\s+serene\b", re.IGNORECASE)  # detect "hail serene"

# -------------------------
# Media detection (images + video) — robust to query strings
# -------------------------
IMG_TAG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
DATA_URL_IMAGE_RE = re.compile(r'^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$', re.IGNORECASE)
DATA_URL_VIDEO_RE = re.compile(r'^data:video/(?:webm|mp4);base64,[A-Za-z0-9+/=\s]+$', re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r'(https?://[^\s"\'<>]+)', re.IGNORECASE)

IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".webm", ".mp4"}

# 0.5x..2.0x speed by integer 50..200
MIN_SPEED_PCT = 50
MAX_SPEED_PCT = 200
DEFAULT_RATE = 1.0

# Tenor
TENOR_API_KEY = os.getenv("TENOR_API_KEY")
TENOR_ENDPOINT = "https://tenor.googleapis.com/v2/search"


class ChatMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Ensure the rooms registry exists and keep 'lobby' persistent
        if not hasattr(self.bot, "chat_ws_rooms"):
            self.bot.chat_ws_rooms = {}
        self.bot.chat_ws_rooms.setdefault("lobby", set())

        # Track which client’s next message should be treated as a Serene question
        self._awaiting_serene_question = set()

        # HTTP client session (slightly longer timeout for Serene)
        self.http_timeout = aiohttp.ClientTimeout(total=5)
        self.http_session = aiohttp.ClientSession(timeout=self.http_timeout)

        # Add the WebSocket route once
        if hasattr(self.bot, "web_app"):
            if not getattr(self.bot, "_chat_route_added", False):
                self.bot.web_app.router.add_get("/chat_ws", self.handle_chat_websocket)
                self.bot._chat_route_added = True
                logger.info("Chat WebSocket route '/chat_ws' established by ChatMain cog.")
            else:
                logger.info("Chat WebSocket route already added; skipping.")
        else:
            logger.error("Bot has no 'web_app' attribute. Cannot add chat WebSocket route.")

    def cog_unload(self):
        try:
            if not self.http_session.closed:
                asyncio.create_task(self.http_session.close())
        except Exception:
            pass

    # -------------------------
    # Pretty names + presence helpers
    # -------------------------

    def _pretty_room(self, name_or_id: Optional[str]) -> str:
        """Match bot.py behavior for nice labels."""
        if not name_or_id:
            return "the lobby"
        s = str(name_or_id).strip()
        if s.lower() == "lobby":
            return "the lobby"
        return s

    async def _broadcast_room_json(self, room_id: str, payload: dict):
        """Send to all sockets in a room (gentle failure handling)."""
        for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
            try:
                if client_ws.closed:
                    continue
                await client_ws.send_json(payload)
            except Exception:
                # Do not hard-remove; leave cleanup to disconnect path
                logger.warning("Could not send payload to a client in room %s.", room_id)

    async def _presence_move_messages(
        self,
        old_room: Optional[str],
        new_room: str,
        display_name: str,
        from_name: Optional[str] = None,
        to_name: Optional[str] = None,
    ):
        """
        Emit presence system-notices with the exact copy the site expects:
          • entering a GAME: "<name> joined the game"
          • leaving a GAME:  "<name> left the game"
          • entering lobby:  "<name> joined the lobby"
        """
        pretty_from = self._pretty_room(from_name or old_room)
        pretty_to = self._pretty_room(to_name or new_room)

        # Old room notice
        if old_room:
            if str(old_room).lower() == "lobby":
                # Moving out of lobby (optional)
                await self._broadcast_room_json(old_room, {
                    "type": "system_notice",
                    "room_id": old_room,
                    "message": f"{display_name} left the lobby.",
                    "timestamp": int(time.time()),
                })
            else:
                # Leaving a game
                await self._broadcast_room_json(old_room, {
                    "type": "system_notice",
                    "room_id": old_room,
                    "message": f"{display_name} left the game.",
                    "timestamp": int(time.time()),
                })

        # New room notice
        if str(new_room).lower() == "lobby":
            await self._broadcast_room_json(new_room, {
                "type": "system_notice",
                "room_id": new_room,
                "message": f"{display_name} joined the lobby",
                "timestamp": int(time.time()),
            })
        else:
            await self._broadcast_room_json(new_room, {
                "type": "system_notice",
                "room_id": new_room,
                "message": f"{display_name} joined the game",
                "timestamp": int(time.time()),
            })

    # -------------------------
    # Sound helpers
    # -------------------------

    def _parse_sound_command(self, text: str):
        m = SOUND_NAME_RE.match(text or "")
        if not m:
            return None

        name = m.group(1)
        speed_pct = m.group(2)
        rate = DEFAULT_RATE
        if speed_pct:
            pct = int(speed_pct)
            if pct < MIN_SPEED_PCT or pct > MAX_SPEED_PCT:
                return None
            rate = pct / 100.0

        visible_text = name
        return name, rate, visible_text

    def _sound_url(self, name: str) -> str:
        return f"{SOUND_BASE_URL}/{name}.ogg"

    async def _sound_exists(self, url: str) -> bool:
        try:
            async with self.http_session.head(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (403, 405):
                    headers = {"Range": "bytes=0-0"}
                    async with self.http_session.get(url, headers=headers, allow_redirects=True) as get_resp:
                        return get_resp.status in (200, 206)
                return False
        except asyncio.TimeoutError:
            logger.warning("Timeout checking sound URL: %s", url)
            return False
        except aiohttp.ClientError as e:
            logger.warning("HTTP error checking sound URL %s: %s", url, e)
            return False
        except Exception:
            logger.exception("Unexpected error checking sound URL: %s", url)
            return False

    # -------------------------
    # Media helpers (image/video)
    # -------------------------

    def _classify_media_url(self, url: str) -> Optional[Tuple[str, str]]:
        """Return ("image"|"video", original_url) if it looks like media."""
        if not url:
            return None

        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://") or low.startswith("data:image/") or low.startswith("data:video/")):
            return None

        # Data URLs
        if DATA_URL_IMAGE_RE.match(url):
            return ("image", url)
        if DATA_URL_VIDEO_RE.match(url):
            return ("video", url)

        # http(s): check extension of path only
        parts = urlsplit(url)
        path = parts.path or ""
        ext = os.path.splitext(path.lower())[1]
        if ext in IMAGE_EXTS:
            return ("image", url)
        if ext in VIDEO_EXTS:
            return ("video", url)
        return None

    def _extract_media_from_text(self, text: str) -> Optional[Tuple[str, str]]:
        """Return (kind, src) where kind in {image, video} if present in text."""
        if not text:
            return None

        s = text.strip()

        # Exact URL line
        m = URL_IN_TEXT_RE.fullmatch(s)
        if m:
            classified = self._classify_media_url(m.group(1))
            if classified:
                return classified

        # URL inside text
        m2 = URL_IN_TEXT_RE.search(text)
        if m2:
            classified = self._classify_media_url(m2.group(1))
            if classified:
                return classified

        # <img src="...">
        m3 = IMG_TAG_SRC_RE.search(text)
        if m3:
            src = m3.group(1)
            classified = self._classify_media_url(src)
            if classified:
                return classified

        # data: URLs (whole-line)
        if DATA_URL_IMAGE_RE.match(s):
            return ("image", s)
        if DATA_URL_VIDEO_RE.match(s):
            return ("video", s)

        return None

    def _build_message_payload(
        self,
        room_id: str,
        display_name: str,
        message_text: str,
        sender_type: str = "user",
        bot_id: Optional[str] = None
    ) -> dict:
        """
        Build a chat payload. If message contains media, wrap/flag it.
        sender_type: "user" or "bot"
        bot_id: e.g., "serene" if sender_type == "bot"
        """
        ts = int(time.time())
        media = self._extract_media_from_text(message_text or "")
        if media:
            kind, src = media
            if kind == "image":
                wrapped_html = f'<img class="chat-gif" src="{html.escape(src, quote=True)}" />'
                payload = {
                    "type": "new_message",
                    "room_id": room_id,
                    "displayName": display_name,
                    "message": wrapped_html,
                    "isImage": True,
                    "imageUrl": src,
                    "timestamp": ts,
                }
            else:  # video
                mime = "video/webm" if src.lower().endswith(".webm") else "video/mp4"
                wrapped_html = (
                    f'<video class="chat-video" controls playsinline preload="metadata">'
                    f'<source src="{html.escape(src, quote=True)}" type="{mime}"></video>'
                )
                payload = {
                    "type": "new_message",
                    "room_id": room_id,
                    "displayName": display_name,
                    "message": wrapped_html,
                    "isVideo": True,
                    "videoUrl": src,
                    "timestamp": ts,
                }
        else:
            payload = {
                "type": "new_message",
                "room_id": room_id,
                "displayName": display_name,
                "message": message_text,
                "isImage": False,
                "timestamp": ts,
            }

        if sender_type == "bot":
            payload["senderType"] = "bot"
            if bot_id:
                payload["botId"] = bot_id

        return payload

    # -------------------------
    # GIF helpers
    # -------------------------

    async def _fetch_gif_url_from_tenor(self, query: str) -> Optional[str]:
        """Get a single GIF URL from Tenor v2."""
        if not TENOR_API_KEY:
            return None
        params = {
            "q": query,
            "key": TENOR_API_KEY,
            "limit": 1,
            "media_filter": "gif",
            "random": "true",
        }
        try:
            async with self.http_session.get(TENOR_ENDPOINT, params=params, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("Tenor request failed: %s", e)
            return None

        try:
            results = data.get("results") or []
            if not results:
                return None
            r0 = results[0]
            media_formats = r0.get("media_formats") or {}
            for key in ("gif", "mediumgif", "tinygif"):
                fmt = media_formats.get(key)
                if fmt and "url" in fmt:
                    return fmt["url"]
            return r0.get("url")
        except Exception:
            return None

    async def _fetch_gif_url_fallback(self, query: str) -> Optional[str]:
        """Fallback to your crawler page with the correct params (kw, total, api)."""
        if not TENOR_API_KEY:
            return None

        params = {"kw": query, "total": 25, "api": TENOR_API_KEY}
        try:
            async with self.http_session.get("https://serenekeks.com/crawl.php", params=params, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                body = (await resp.text()).strip()
                m = URL_IN_TEXT_RE.search(body)
                if not m:
                    return None
                candidate = m.group(1)
                classified = self._classify_media_url(candidate)
                if classified and classified[0] == "image":
                    return candidate
                return None
        except Exception:
            return None

    async def _handle_gif_command(self, room_id: str, display_name: str, raw_text: str) -> bool:
        """Return True if handled. Triggers when FIRST token is 'gif' and a query follows."""
        if not raw_text:
            return False
        parts = raw_text.strip().split(None, 1)
        if not parts or parts[0].lower() != "gif":
            return False
        if len(parts) == 1 or not parts[1].strip():
            return False

        query = parts[1].strip()
        logger.info("GIF command by %s in %s | query=%r", display_name, room_id, query)

        url = await self._fetch_gif_url_from_tenor(query)
        if not url:
            url = await self._fetch_gif_url_fallback(query)

        if url:
            payload = self._build_message_payload(
                room_id=room_id,
                display_name=display_name,
                message_text=url,
                sender_type="user"
            )
            await self._broadcast_room_json(room_id, payload)
        else:
            # Soft notice if desired (can remove if you prefer silence)
            try:
                await self._broadcast_room_json(room_id, {
                    "type": "system_notice",
                    "room_id": room_id,
                    "message": f"No GIF found for “{query}”.",
                    "timestamp": int(time.time()),
                })
            except Exception:
                pass

        return True

    # -------------------------
    # Serene helpers
    # -------------------------

    async def _serene_request_get(self, params: dict) -> Optional[str]:
        """Call Serene using GET (matches PHP: $_GET[...] checks)."""
        try:
            logger.info("[Serene] GET -> %s | params=%s", SERENE_BOT_URL, params)
        except Exception:
            pass

        try:
            async with self.http_session.get(SERENE_BOT_URL, params=params) as resp:
                status = resp.status
                try:
                    text = await resp.text()
                except Exception as e_read:
                    logger.error("[Serene] HTTP %s read error: %s", status, e_read)
                    return None

                body = text if isinstance(text, str) else (text or "")
                body_preview = body[:300].replace("\n", "\\n")
                logger.info("[Serene] HTTP %s <- body_len=%s preview=\"%s%s\"",
                            status, len(body), body_preview, "…" if len(body) > 300 else "")

                if status != 200:
                    logger.warning("[Serene] Non-200 response (status=%s), discarding.", status)
                    return None

                if not body.strip():
                    logger.info("[Serene] Empty or whitespace-only body returned.")
                    return None

                return body
        except asyncio.TimeoutError:
            logger.warning("[Serene] GET timed out for params=%s", params)
            return None
        except aiohttp.ClientError as e:
            logger.warning("[Serene] Client error on GET: %s", e)
            return None
        except Exception:
            logger.exception("[Serene] Unexpected error on GET request.")
            return None

    async def _delayed_broadcast_serene(self, room_id: str, message: str):
        """Apply a human-like delay before broadcasting Serene's message."""
        try:
            await asyncio.sleep(2.0)
        except Exception:
            pass
        payload = self._build_message_payload(
            room_id=room_id,
            display_name=SERENE_DISPLAY_NAME,
            message_text=message,
            sender_type="bot",
            bot_id="serene",
        )
        await self._broadcast_room_json(room_id, payload)

    async def _serene_start(self, room_id: str, display_name: str):
        logger.info("[Serene] START triggered by %s in room %s", display_name, room_id)
        reply = await self._serene_request_get({"start": "true", "player": display_name})
        if reply:
            await self._delayed_broadcast_serene(room_id, reply)

    async def _serene_question(self, room_id: str, display_name: str, question_raw: str):
        safe_q = html.escape(question_raw or "", quote=True)
        logger.info("[Serene] QUESTION from %s in room %s", display_name, room_id)
        reply = await self._serene_request_get({"question": safe_q, "player": display_name})
        if reply:
            await self._delayed_broadcast_serene(room_id, reply)

    async def _serene_hail(self, room_id: str, display_name: str, hail_phrase: str):
        logger.info("[Serene] HAIL by %s in room %s | %r", display_name, room_id, hail_phrase)
        reply = await self._serene_request_get({"hail": hail_phrase, "player": display_name})
        if reply:
            await self._delayed_broadcast_serene(room_id, reply)

    # -------------------------
    # WebSocket handler
    # -------------------------

    async def handle_chat_websocket(self, request: web.Request) -> web.WebSocketResponse:
        # Match bot.py socket options for stability & large payloads
        ws = web.WebSocketResponse(heartbeat=25.0, max_msg_size=16 * 1024 * 1024, autoping=True)
        await ws.prepare(request)

        room_id = None
        display_name = "Anonymous"

        try:
            # --- Robust initial handshake: wait for TEXT with JSON ---
            first_msg_str = None
            while True:
                msg = await ws.receive()

                if msg.type == web.WSMsgType.TEXT:
                    first_msg_str = msg.data
                    break
                elif msg.type in (web.WSMsgType.PING, web.WSMsgType.PONG, web.WSMsgType.BINARY):
                    continue
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    logger.info("Client closed before sending initial TEXT message.")
                    await ws.close()
                    return ws
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WS error before handshake: {ws.exception()}")
                    await ws.close()
                    return ws

            # Parse the initial JSON (loop until valid)
            while True:
                try:
                    initial_data = json.loads(first_msg_str)
                    break
                except json.JSONDecodeError:
                    logger.warning("Malformed initial JSON; awaiting next TEXT frame for registration.")
                    nxt = await ws.receive()
                    if nxt.type == web.WSMsgType.TEXT:
                        first_msg_str = nxt.data
                        continue
                    else:
                        continue

            room_id = initial_data.get("room_id")
            display_name = initial_data.get("displayName", "Anonymous")

            if not room_id or not display_name:
                logger.warning("Missing room_id/displayName in initial frame; waiting for proper registration.")
                while not (room_id and display_name):
                    next_msg = await ws.receive()
                    if next_msg.type != web.WSMsgType.TEXT:
                        continue
                    try:
                        jd = json.loads(next_msg.data)
                    except json.JSONDecodeError:
                        continue
                    room_id = room_id or jd.get("room_id")
                    display_name = display_name or jd.get("displayName", "Anonymous")

            # Ensure lobby bucket persists
            self.bot.chat_ws_rooms.setdefault("lobby", set())

            # Register the WebSocket to the chat room
            if room_id not in self.bot.chat_ws_rooms:
                self.bot.chat_ws_rooms[room_id] = set()
            self.bot.chat_ws_rooms[room_id].add(ws)
            logger.info("Chat client '%s' connected to room %s.", display_name, room_id)

            ts = int(time.time())

            # Broadcast join + friendly system notice on first join
            await self._broadcast_room_json(room_id, {
                "type": "user_joined",
                "room_id": room_id,
                "displayName": display_name,
                "timestamp": ts,
            })
            try:
                # SPEC: first connect to lobby should say "<name> joined the lobby"
                if str(room_id).lower() == "lobby":
                    await self._broadcast_room_json(room_id, {
                        "type": "system_notice",
                        "room_id": room_id,
                        "message": f"{display_name} joined the lobby",
                        "timestamp": ts,
                    })
                else:
                    await self._broadcast_room_json(room_id, {
                        "type": "system_notice",
                        "room_id": room_id,
                        "message": f"{display_name} joined the game",
                        "timestamp": ts,
                    })
            except Exception:
                pass

            # Listen for subsequent messages
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Parse JSON (ignore malformed frames)
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.debug("Ignoring malformed JSON frame in room %s.", room_id)
                        continue

                    # --- App-level ping/pong to keep idle sockets fresh ---
                    if data.get("type") == "ping":
                        try:
                            await ws.send_json({
                                "type": "pong",
                                "ts": data.get("ts") or int(time.time() * 1000),
                                "room_id": room_id,
                            })
                        except Exception:
                            pass
                        continue  # not a chat message

                    # --- Room rebind protocol (NO 'message', HAS 'room_id') ---
                    if 'room_id' in data and 'message' not in data:
                        new_room = str(data.get('room_id') or '').strip()
                        if new_room and new_room != room_id:
                            old_room = room_id
                            from_name = data.get('from_name')  # optional pretty labels
                            to_name = data.get('to_name')

                            # Move socket between buckets
                            try:
                                if old_room in self.bot.chat_ws_rooms:
                                    self.bot.chat_ws_rooms[old_room].discard(ws)
                                    # never delete the lobby bucket
                                    if not self.bot.chat_ws_rooms[old_room] and str(old_room).lower() != "lobby":
                                        del self.bot.chat_ws_rooms[old_room]
                            except Exception:
                                pass

                            if new_room not in self.bot.chat_ws_rooms:
                                self.bot.chat_ws_rooms[new_room] = set()
                            self.bot.chat_ws_rooms[new_room].add(ws)

                            ts_move = int(time.time())

                            # Presence events
                            await self._broadcast_room_json(old_room, {
                                "type": "user_left",
                                "room_id": old_room,
                                "displayName": display_name,
                                "timestamp": ts_move,
                            })
                            await self._broadcast_room_json(new_room, {
                                "type": "user_joined",
                                "room_id": new_room,
                                "displayName": display_name,
                                "timestamp": ts_move,
                            })

                            # SPEC presence copy
                            await self._presence_move_messages(
                                old_room, new_room, display_name, from_name=from_name, to_name=to_name
                            )

                            room_id = new_room
                            logger.info("Rebound '%s' to room '%s'.", display_name, room_id)

                        # Do not treat this as a chat message
                        continue

                    # --- Authoritative AREA CHANGE frame (frontend sends after UI bind) ---
                    # Expect: { "type":"area_change", "from":..., "to":..., "from_name":..., "to_name":... }
                    if (data.get("type") == "area_change") and isinstance(data.get("to"), str):
                        new_room = data.get("to")
                        from_name = data.get("from_name")
                        to_name = data.get("to_name")
                        if new_room and new_room != room_id:
                            old_room = room_id

                            # Move socket
                            try:
                                if old_room in self.bot.chat_ws_rooms:
                                    self.bot.chat_ws_rooms[old_room].discard(ws)
                                    if not self.bot.chat_ws_rooms[old_room] and str(old_room).lower() != "lobby":
                                        del self.bot.chat_ws_rooms[old_room]
                            except Exception:
                                pass

                            if new_room not in self.bot.chat_ws_rooms:
                                self.bot.chat_ws_rooms[new_room] = set()
                            self.bot.chat_ws_rooms[new_room].add(ws)

                            ts_move = int(time.time())

                            # Presence events
                            await self._broadcast_room_json(old_room, {
                                "type": "user_left",
                                "room_id": old_room,
                                "displayName": display_name,
                                "timestamp": ts_move,
                            })
                            await self._broadcast_room_json(new_room, {
                                "type": "user_joined",
                                "room_id": new_room,
                                "displayName": display_name,
                                "timestamp": ts_move,
                            })

                            # SPEC presence copy
                            await self._presence_move_messages(
                                old_room, new_room, display_name, from_name=from_name, to_name=to_name
                            )

                            room_id = new_room
                            logger.info("[area_change] '%s' -> room '%s'.", display_name, room_id)

                        continue

                    # --- Regular chat message path ---
                    message_text = data.get("message")
                    if message_text:
                        logger.info("Chat message from '%s' in room %s: %s", display_name, room_id, message_text)

                        # GIF command (FIRST token == 'gif')
                        if await self._handle_gif_command(room_id, display_name, message_text):
                            continue

                        lowered = message_text.lower()

                        # Serene question flow
                        if ws in self._awaiting_serene_question:
                            self._awaiting_serene_question.discard(ws)
                            asyncio.create_task(self._serene_question(room_id, display_name, message_text))

                        # 'hail serene' has priority
                        m_hail = HAIL_SERENE_RE.search(lowered)
                        if m_hail:
                            asyncio.create_task(self._serene_hail(room_id, display_name, m_hail.group(0)))
                        elif SERENE_WORD_RE.search(lowered):
                            self._awaiting_serene_question.add(ws)
                            asyncio.create_task(self._serene_start(room_id, display_name))

                        # Sound trigger flow (restored original behavior)
                        parsed = self._parse_sound_command(message_text)
                        if parsed:
                            name, rate, visible_text = parsed
                            url = self._sound_url(name)
                            if await self._sound_exists(url):
                                tsn = int(time.time())
                                # 1) show only the name in chat
                                await self._broadcast_room_json(room_id, {
                                    "type": "new_message",
                                    "room_id": room_id,
                                    "displayName": display_name,
                                    "message": visible_text,
                                    "timestamp": tsn,
                                })
                                # 2) play it
                                await self._broadcast_room_json(room_id, {
                                    "type": "play_sound",
                                    "room_id": room_id,
                                    "displayName": display_name,
                                    "name": name,
                                    "url": url,
                                    "rate": rate,  # 0.5..2.0
                                    "timestamp": tsn,
                                })
                                continue

                        # Normal message (supports media wrapping)
                        user_payload = self._build_message_payload(
                            room_id=room_id,
                            display_name=display_name,
                            message_text=message_text,
                            sender_type="user"
                        )
                        await self._broadcast_room_json(room_id, user_payload)

                elif msg.type in (web.WSMsgType.PING, web.WSMsgType.PONG):
                    continue

                elif msg.type == web.WSMsgType.BINARY:
                    logger.warning("Ignoring unexpected BINARY message in room %s.", room_id)
                    continue

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error("Chat WebSocket error in room %s: %s", room_id, ws.exception())

                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    logger.info("Chat WebSocket client '%s' closing connection from room %s.", display_name, room_id)
                    break

        except asyncio.CancelledError:
            logger.info("Chat WebSocket connection for '%s' in room %s cancelled.", display_name, room_id)
        except Exception as e:
            logger.error(f"Error in handle_chat_websocket for room {room_id}: {e}", exc_info=True)
        finally:
            # Unregister and notify others
            try:
                if room_id and room_id in self.bot.chat_ws_rooms:
                    if ws in self.bot.chat_ws_rooms[room_id]:
                        self.bot.chat_ws_rooms[room_id].remove(ws)

                    # Ensure no stale question flag for this socket
                    self._awaiting_serene_question.discard(ws)

                    logger.info(
                        "Chat client '%s' disconnected from room %s. Remaining: %d",
                        display_name, room_id, len(self.bot.chat_ws_rooms.get(room_id, set()))
                    )

                    # Broadcast leave message to that room
                    await self._broadcast_room_json(room_id, {
                        "type": "user_left",
                        "room_id": room_id,
                        "displayName": display_name,
                        "timestamp": int(time.time()),
                    })

                    # Never delete the lobby bucket; other rooms can be closed
                    if not self.bot.chat_ws_rooms[room_id] and str(room_id).lower() != "lobby":
                        del self.bot.chat_ws_rooms[room_id]
                        logger.info("Chat room %s is now empty and has been closed.", room_id)

                # Ensure lobby key persists
                self.bot.chat_ws_rooms.setdefault("lobby", set())
            finally:
                return ws


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatMain(bot))
