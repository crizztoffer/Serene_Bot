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

logger = logging.getLogger(__name__)

SOUND_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?\s*$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

# Serene config
SERENE_BOT_URL = "https://serenekeks.com/serene_bot.php"
SERENE_DISPLAY_NAME = "Serene"
SERENE_WORD_RE = re.compile(r"\bserene\b", re.IGNORECASE)
HAIL_SERENE_RE = re.compile(r"\bhail\s+serene\b", re.IGNORECASE)  # detect "hail serene"

# Image/GIF detection
IMG_EXT_RE = r"(?:gif|png|jpe?g|webp)"
IMAGE_URL_RE = re.compile(rf'^\s*(https?://[^\s"\'<>]+?\.(?:{IMG_EXT_RE})(?:\?[^\s"\'<>]*)?)\s*$', re.IGNORECASE)
IMAGE_URL_IN_TEXT_RE = re.compile(rf'(https?://[^\s"\'<>]+?\.(?:{IMG_EXT_RE})(?:\?[^\s"\'<>]*)?)', re.IGNORECASE)
IMG_TAG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
DATA_URL_RE = re.compile(r'^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$', re.IGNORECASE)

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

        # Ensure the rooms registry exists
        if not hasattr(self.bot, "chat_ws_rooms"):
            self.bot.chat_ws_rooms = {}

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
    # Helpers for sound commands
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
        return f"{SOUND_BASE_URL}/{name}.ogg}"

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

    async def _broadcast_room_json(self, room_id: str, payload: dict):
        for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
            try:
                await client_ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                logger.warning("Could not send payload to a client in room %s.", room_id)

    # -------------------------
    # Image helpers
    # -------------------------

    def _extract_image_from_text(self, text: str) -> Optional[str]:
        """Return an image/gif source if present (URL, data: URL, or <img src=...>)."""
        if not text:
            return None

        # Exact image URL line
        m = IMAGE_URL_RE.match(text.strip())
        if m:
            return m.group(1)

        # Any image URL within text
        m2 = IMAGE_URL_IN_TEXT_RE.search(text)
        if m2:
            return m2.group(1)

        # <img src="...">
        m3 = IMG_TAG_SRC_RE.search(text)
        if m3:
            return m3.group(1)

        # data URL
        if DATA_URL_RE.match(text.strip()):
            return text.strip()

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
        Build a chat payload. If message contains an image/gif, wrap and flag it.
        sender_type: "user" or "bot"
        bot_id: e.g., "serene" if sender_type == "bot"
        """
        img_src = self._extract_image_from_text(message_text or "")
        if img_src:
            # Send standardized <img> wrapper and also provide imageUrl + isImage flag
            wrapped_html = f'<img class="chat-gif" src="{html.escape(img_src, quote=True)}" />'
            payload = {
                "type": "new_message",
                "room_id": room_id,
                "displayName": display_name,
                "message": wrapped_html,     # HTML-safe wrapper
                "isImage": True,
                "imageUrl": img_src,         # raw src for frontend logic if needed
                "timestamp": int(time.time()),
            }
        else:
            payload = {
                "type": "new_message",
                "room_id": room_id,
                "displayName": display_name,
                "message": message_text,
                "isImage": False,
                "timestamp": int(time.time()),
            }

        if sender_type == "bot":
            payload["senderType"] = "bot"
            if bot_id:
                payload["botId"] = bot_id

        return payload

    # -------------------------
    # GIF helpers (FIXED)
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
            # Prefer these formats if present
            for key in ("gif", "mediumgif", "tinygif"):
                fmt = media_formats.get(key)
                if fmt and "url" in fmt:
                    return fmt["url"]
            return r0.get("url")
        except Exception:
            return None

    async def _fetch_gif_url_fallback(self, query: str) -> Optional[str]:
        """
        Fallback to your crawler page with the correct params (kw, total, api).
        Must be an instance method (has `self`) and use `self.http_session`.
        """
        if not TENOR_API_KEY:
            return None

        params = {
            "kw": query,           # matches crawl.php expectation
            "total": 25,           # reasonable positive int
            "api": TENOR_API_KEY,  # pass your Tenor key through
        }

        try:
            async with self.http_session.get(
                "https://serenekeks.com/crawl.php",
                params=params,
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return None
                body = (await resp.text()).strip()
                # crawl.php echoes a single URL; validate and return
                if IMAGE_URL_RE.match(body) or IMAGE_URL_IN_TEXT_RE.search(body):
                    return body
                return None
        except Exception as e:
            logger.warning("crawl.php fallback failed: %s", e)
            return None

    async def _handle_gif_command(self, room_id: str, display_name: str, raw_text: str) -> bool:
        """
        Return True if handled.
        Only triggers when the FIRST token is exactly 'gif' and a query follows.
        """
        if not raw_text:
            return False
        parts = raw_text.strip().split(None, 1)
        if not parts or parts[0].lower() != "gif":
            return False
        if len(parts) == 1 or not parts[1].strip():
            return False

        query = parts[1].strip()
        logger.info("GIF command by %s in %s | query=%r", display_name, room_id, query)

        # Try Tenor, then fallback to crawl.php
        url = await self._fetch_gif_url_from_tenor(query)
        if not url:
            url = await self._fetch_gif_url_fallback(query)

        if url:
            payload = self._build_message_payload(
                room_id=room_id,
                display_name=display_name,
                message_text=url,  # will be wrapped as <img class="chat-gif"...>
                sender_type="user"
            )
            await self._broadcast_room_json(room_id, payload)
        else:
            await self._broadcast_room_json(room_id, {
                "type": "system_notice",
                "room_id": room_id,
                "message": f"No GIF found for “{query}”.",
                "timestamp": int(time.time()),
            })
        return True

    # -------------------------
    # Serene helpers
    # -------------------------

    async def _serene_request_get(self, params: dict) -> Optional[str]:
        """
        Call Serene using GET (matches PHP: $_GET[...] checks).
        Logs URL, status, and a preview of the body.
        """
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
        """
        Apply a human-like delay before broadcasting Serene's message.
        Wrap as <img class="chat-gif"> if it looks like an image/GIF.
        """
        try:
            await asyncio.sleep(2.0)  # 2-second humanized delay
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
            logger.info("[Serene] Broadcasting START reply to room %s (len=%d) after delay", room_id, len(reply))
            await self._delayed_broadcast_serene(room_id, reply)
        else:
            logger.info("[Serene] START produced no reply for room %s", room_id)

    async def _serene_question(self, room_id: str, display_name: str, question_raw: str):
        # Keep the question HTML-safe
        safe_q = html.escape(question_raw or "", quote=True)
        logger.info("[Serene] QUESTION from %s in room %s: raw=\"%s\" safe=\"%s\"",
                    display_name, room_id, (question_raw or "")[:200], safe_q[:200])

        reply = await self._serene_request_get({"question": safe_q, "player": display_name})
        if reply:
            logger.info("[Serene] Broadcasting QUESTION reply to room %s (len=%d) after delay", room_id, len(reply))
            await self._delayed_broadcast_serene(room_id, reply)
        else:
            logger.info("[Serene] QUESTION produced no reply for room %s", room_id)

    async def _serene_hail(self, room_id: str, display_name: str, hail_phrase: str):
        """
        Handle 'hail serene' phrase: GET with hail=<matched phrase>&player=<display name>
        The PHP lowercases and uses this string in getHails(...).
        """
        logger.info("[Serene] HAIL triggered by %s in room %s | hail_phrase=\"%s\"",
                    display_name, room_id, hail_phrase)
        reply = await self._serene_request_get({"hail": hail_phrase, "player": display_name})
        if reply:
            logger.info("[Serene] Broadcasting HAIL reply to room %s (len=%d) after delay", room_id, len(reply))
            await self._delayed_broadcast_serene(room_id, reply)
        else:
            logger.info("[Serene] HAIL produced no reply for room %s", room_id)

    # -------------------------
    # WebSocket handler
    # -------------------------

    async def handle_chat_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
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
                elif msg.type in (web.WSMsgType.PING, web.WSMsgType.PONG):
                    continue
                elif msg.type == web.WSMsgType.BINARY:
                    logger.warning("First WS frame was BINARY; ignoring and waiting for TEXT...")
                    continue
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    logger.info("Client closed before sending initial TEXT message.")
                    await ws.close()
                    return ws
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WS error before handshake: {ws.exception()}")
                    await ws.close()
                    return ws

            # Parse the initial JSON
            try:
                initial_data = json.loads(first_msg_str)
            except json.JSONDecodeError:
                logger.error(f"Malformed initial JSON from client: {first_msg_str!r}")
                await ws.send_json({"type": "error", "message": "Malformed initial JSON."})
                await ws.close()
                return ws

            room_id = initial_data.get("room_id")
            display_name = initial_data.get("displayName", "Anonymous")

            if not room_id:
                logger.error(f"Chat WebSocket initial message missing room_id: {initial_data}")
                await ws.send_json({"type": "error", "message": "room_id is required."})
                await ws.close()
                return ws

            # Register the WebSocket to the chat room
            if room_id not in self.bot.chat_ws_rooms:
                self.bot.chat_ws_rooms[room_id] = set()
            self.bot.chat_ws_rooms[room_id].add(ws)
            logger.info(
                f"Chat client '{display_name}' connected to room {room_id}. "
                f"Total chat connections: {len(self.bot.chat_ws_rooms[room_id])}"
            )

            # Broadcast join message to the room
            join_message = {
                "type": "user_joined",
                "room_id": room_id,
                "displayName": display_name,
                "timestamp": int(time.time()),
            }
            await self._broadcast_room_json(room_id, join_message)

            # Listen for subsequent messages
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.error(f"Received malformed JSON from chat client in room {room_id}: {msg.data!r}")
                        continue

                    message_text = data.get("message")
                    if message_text:
                        logger.info(f"Chat message from '{display_name}' in room {room_id}: {message_text}")

                        # -------------------------
                        # GIF command (FIRST TOKEN MUST BE 'gif')
                        # -------------------------
                        if await self._handle_gif_command(room_id, display_name, message_text):
                            # If handled as GIF, skip the rest (don’t double-post original text)
                            continue

                        # -------------------------
                        # Serene logic
                        # -------------------------
                        lowered = message_text.lower()

                        # If awaiting a question from this client, treat THIS message as the question
                        if ws in self._awaiting_serene_question:
                            logger.info("[Serene] Socket is awaiting question -> sending question now.")
                            self._awaiting_serene_question.discard(ws)
                            asyncio.create_task(self._serene_question(room_id, display_name, message_text))

                        # First: handle explicit 'hail serene' (and do NOT also trigger start/question)
                        m_hail = HAIL_SERENE_RE.search(lowered)
                        if m_hail:
                            hail_phrase = m_hail.group(0)  # e.g., "hail serene"
                            logger.info("[Serene] 'hail serene' detected in room %s by %s.", room_id, display_name)
                            asyncio.create_task(self._serene_hail(room_id, display_name, hail_phrase))

                        # Else: generic 'serene' keyword -> trigger start and arm next message as question
                        elif SERENE_WORD_RE.search(lowered):
                            logger.info("[Serene] Keyword detected in room %s by %s. Arming next message as question and calling START.", room_id, display_name)
                            self._awaiting_serene_question.add(ws)
                            asyncio.create_task(self._serene_start(room_id, display_name))

                        # -------------------------
                        # Sound trigger flow (original behavior preserved)
                        # -------------------------
                        parsed = self._parse_sound_command(message_text)

                        if parsed:
                            # Sound trigger: show ONLY the name in chat, then play sound if file exists
                            name, rate, visible_text = parsed

                            # 1) Broadcast the "visible" chat message (name only)
                            chat_message = {
                                "type": "new_message",
                                "room_id": room_id,
                                "displayName": display_name,
                                "message": visible_text,
                                "timestamp": int(time.time()),
                            }
                            await self._broadcast_room_json(room_id, chat_message)

                            # 2) Verify sound exists; if so, broadcast play_sound
                            url = self._sound_url(name)
                            if await self._sound_exists(url):
                                sound_payload = {
                                    "type": "play_sound",
                                    "room_id": room_id,
                                    "displayName": display_name,
                                    "name": name,
                                    "url": url,
                                    "rate": rate,  # 0.5..2.0
                                    "timestamp": int(time.time()),
                                }
                                await self._broadcast_room_json(room_id, sound_payload)
                            else:
                                try:
                                    await ws.send_json({
                                        "type": "system_notice",
                                        "room_id": room_id,
                                        "message": f"Sound '{name}' not found.",
                                        "timestamp": int(time.time()),
                                    })
                                except Exception:
                                    pass

                        else:
                            # Normal text (no sound trigger):
                            # If user message contains image/gif, flag and wrap it
                            user_payload = self._build_message_payload(
                                room_id=room_id,
                                display_name=display_name,
                                message_text=message_text,
                                sender_type="user"
                            )
                            await self._broadcast_room_json(room_id, user_payload)

                elif msg.type == web.WSMsgType.PING or msg.type == web.WSMsgType.PONG:
                    continue

                elif msg.type == web.WSMsgType.BINARY:
                    logger.warning(f"Ignoring unexpected BINARY message in room {room_id}.")
                    continue

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"Chat WebSocket error in room {room_id}: {ws.exception()}")

                elif msg.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSING,
                    web.WSMsgType.CLOSED,
                ):
                    logger.info(f"Chat WebSocket client '{display_name}' closing connection from room {room_id}.")
                    break

        except asyncio.CancelledError:
            logger.info(f"Chat WebSocket connection for '{display_name}' in room {room_id} cancelled.")
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
                        f"Chat client '{display_name}' disconnected from room {room_id}. "
                        f"Remaining connections: {len(self.bot.chat_ws_rooms.get(room_id, set()))}"
                    )

                    # Broadcast leave message
                    leave_message = {
                        "type": "user_left",
                        "room_id": room_id,
                        "displayName": display_name,
                        "timestamp": int(time.time()),
                    }
                    await self._broadcast_room_json(room_id, leave_message)

                    if not self.bot.chat_ws_rooms[room_id]:
                        del self.bot.chat_ws_rooms[room_id]
                        logger.info(f"Chat room {room_id} is now empty and has been closed.")
            finally:
                return ws


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatMain(bot))
