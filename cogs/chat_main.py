import json
import time
import asyncio
import logging
from aiohttp import web
import aiohttp
import re
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

SOUND_NAME_RE = re.compile(r'^\s*([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?\s*$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

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

        # Create an HTTP client session for HEAD checks
        self.http_timeout = aiohttp.ClientTimeout(total=3)
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
        # Ensure session is closed when cog unloads
        try:
            if not self.http_session.closed:
                asyncio.create_task(self.http_session.close())
        except Exception:
            pass

    # -------------------------
    # Helpers for sound commands
    # -------------------------

    def _parse_sound_command(self, text: str):
        """
        Detect a sound trigger like 'hahaha' or 'hahaha 150'.
        Returns (name, rate, visible_text) if valid, else None.
        - visible_text is ONLY the name (number is hidden from chat).
        - rate is 0.5..2.0 based on 50..200; defaults to 1.0 if no number.
        """
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
        # name comes from a strict regex, so no traversal
        return f"{SOUND_BASE_URL}/{name}.ogg"

    async def _sound_exists(self, url: str) -> bool:
        """
        HEAD the URL to verify the sound exists before broadcasting.
        Returns True on 200 OK, False otherwise.
        """
        try:
            async with self.http_session.head(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    return True
                # Some hosts don’t allow HEAD; fall back to GET with Range: bytes=0-0
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
        """
        Broadcast a JSON payload to all clients in a room.
        Cleans up any dead sockets.
        """
        for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
            try:
                await client_ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                logger.warning("Could not send payload to a client in room %s.", room_id)

    # -------------------------
    # WebSocket handler
    # -------------------------

    async def handle_chat_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """
        Handles WebSocket connections for chat rooms.
        It registers a client to a chat room and broadcasts messages to all clients in that room.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        room_id = None
        display_name = "Anonymous"

        try:
            # --- Robust initial handshake: wait for a TEXT frame with JSON payload ---
            first_msg_str = None
            while True:
                msg = await ws.receive()

                if msg.type == web.WSMsgType.TEXT:
                    first_msg_str = msg.data
                    break  # got the JSON handshake

                elif msg.type in (web.WSMsgType.PING, web.WSMsgType.PONG):
                    continue

                elif msg.type == web.WSMsgType.BINARY:
                    logger.warning("First WS frame was BINARY; ignoring and waiting for TEXT...")
                    continue

                elif msg.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSING,
                    web.WSMsgType.CLOSED,
                ):
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
                                # Optional: send a private notice back to the sender only
                                # Comment out if you prefer silence on missing files
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
                            # Normal text (no sound trigger)
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
