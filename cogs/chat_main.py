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
from typing import Optional

logger = logging.getLogger(__name__)

SOUND_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?\s*$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

# Serene config
SERENE_BOT_URL = "https://serenekeks.com/serene_bot.php"
SERENE_DISPLAY_NAME = "Serene"
SERENE_WORD_RE = re.compile(r"\bserene\b", re.IGNORECASE)
HAIL_SERENE_RE = re.compile(r"\bhail\s+serene\b", re.IGNORECASE)  # NEW

# 0.5x..2.0x speed by integer 50..200
MIN_SPEED_PCT = 50
MAX_SPEED_PCT = 200
DEFAULT_RATE = 1.0


class ChatMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Ensure the rooms registry exists
        if not hasattr(self.bot, "chat_ws_rooms"):
            self.bot.chat_ws_rooms = {}

        # Track which client’s next message should be treated as a Serene question
        self._awaiting_serene_question = set()

        # HTTP client session
        # Give Serene a slightly longer timeout just in case
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

    async def _broadcast_room_json(self, room_id: str, payload: dict):
        for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
            try:
                await client_ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                logger.warning("Could not send payload to a client in room %s.", room_id)

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
        """
        try:
            await asyncio.sleep(2.0)  # NEW: 2-second humanized delay
        except Exception:
            pass
        payload = {
            "type": "new_message",
            "room_id": room_id,
            "displayName": SERENE_DISPLAY_NAME,
            "message": message,
            "senderType": "bot",
            "botId": "serene",
            "timestamp": int(time.time()),
        }
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
        # Keep the question HTML-safe (your earlier requirement)
        safe_q = html.escape(question_raw or "", quote=True)
        logger.info("[Serene] QUESTION from %s in room %s: raw=\"%s\" safe=\"%s\"",
                    display_name, room_id, (question_raw or "")[:200], safe_q[:200])

        reply = await self._serene_request_get({"question": safe_q, "player": display_name})
        if reply:
            logger.info("[Serene] Broadcasting QUESTION reply to room %s (len=%d) after delay", room_id, len(reply))
            await self._delayed_broadcast_serene(room_id, reply)
        else:
            logger.info("[Serene] QUESTION produced no reply for room %s", room_id)

    async def _serene_hail(self, room_id: str, display_name: str):  # NEW
        """
        Handle 'hail serene' phrase: GET with hail=true&player=<display name>
        """
        logger.info("[Serene] HAIL triggered by %s in room %s", display_name, room_id)
        reply = await self._serene_request_get({"hail": "true", "player": display_name})
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
                        # Serene logic
                        # -------------------------
                        lowered = message_text.lower()

                        # If awaiting a question from this client, treat THIS message as the question
                        if ws in self._awaiting_serene_question:
                            logger.info("[Serene] Socket is awaiting question -> sending question now.")
                            self._awaiting_serene_question.discard(ws)
                            asyncio.create_task(self._serene_question(room_id, display_name, message_text))

                        # First: handle explicit 'hail serene' (and do NOT also trigger start/question)
                        if HAIL_SERENE_RE.search(lowered):
                            logger.info("[Serene] 'hail serene' detected in room %s by %s.", room_id, display_name)
                            asyncio.create_task(self._serene_hail(room_id, display_name))

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
                            # Normal text (no sound trigger): broadcast exactly what they typed
                            chat_message = {
                                "type": "new_message",
                                "room_id": room_id,
                                "displayName": display_name,
                                "message": message_text,
                                "timestamp": int(time.time()),
                            }
                            await self._broadcast_room_json(room_id, chat_message)

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
