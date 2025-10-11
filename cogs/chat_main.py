import json
import time
import asyncio
import logging
from aiohttp import web
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


class ChatMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Ensure the rooms registry exists
        if not hasattr(self.bot, "chat_ws_rooms"):
            self.bot.chat_ws_rooms = {}

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
                    # ignore control frames and continue waiting
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
            for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
                try:
                    await client_ws.send_json(join_message)
                except (ConnectionResetError, RuntimeError):
                    logger.warning(
                        f"Could not send join message to a client in room {room_id}. Connection issue."
                    )

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
                        # Broadcast the new message to all clients in the room
                        chat_message = {
                            "type": "new_message",
                            "room_id": room_id,
                            "displayName": display_name,
                            "message": message_text,
                            "timestamp": int(time.time()),
                        }
                        for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
                            try:
                                await client_ws.send_json(chat_message)
                            except (ConnectionResetError, RuntimeError):
                                logger.warning(
                                    f"Could not send chat message to a client in room {room_id}. Connection issue."
                                )

                elif msg.type == web.WSMsgType.PING or msg.type == web.WSMsgType.PONG:
                    # no-op; aiohttp handles pings/pongs internally too
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
                    for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
                        try:
                            await client_ws.send_json(leave_message)
                        except (ConnectionResetError, RuntimeError):
                            logger.warning(
                                f"Could not send leave message to a client in room {room_id}. Connection issue."
                            )

                    if not self.bot.chat_ws_rooms[room_id]:
                        del self.bot.chat_ws_rooms[room_id]
                        logger.info(f"Chat room {room_id} is now empty and has been closed.")
            finally:
                return ws


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatMain(bot))
