import discord
from discord.ext import commands
from aiohttp import web
import json
import logging
import asyncio
import time

logger = logging.getLogger(__name__)

class ChatMain(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Add the WebSocket route directly to the bot's web app instance when the cog is initialized.
        if hasattr(self.bot, 'web_app'):
            # Prevent duplicate route registration if the cog is (re)loaded.
            if not getattr(self.bot, "_chat_route_added", False):
                self.bot.web_app.router.add_get('/chat_ws', self.handle_chat_websocket)
                self.bot._chat_route_added = True
                logger.info("Chat WebSocket route '/chat_ws' established by ChatMain cog.")
            else:
                logger.info("Chat WebSocket route '/chat_ws' was already added; skipping.")
        else:
            logger.error("Bot has no 'web_app' attribute. Cannot add chat WebSocket route.")

    async def handle_chat_websocket(self, request):
        """
        Handles WebSocket connections for chat rooms.
        It registers a client to a chat room and broadcasts messages to all clients in that room.
        The first TEXT frame from the client must be a JSON object with:
          - room_id (required)
          - displayName (optional; defaults to 'Anonymous')
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        room_id = None
        display_name = "Anonymous"

        try:
            # --- Robust initial read: tolerate non-TEXT frames until we get the JSON handshake ---
            while True:
                msg = await ws.receive()

                if msg.type == web.WSMsgType.TEXT:
                    first_msg = msg.data
                    break  # got the JSON payload

                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    logger.info("Client closed before sending initial TEXT message.")
                    await ws.close()
                    return ws

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WS error before handshake: {ws.exception()}")
                    await ws.close()
                    return ws

                else:
                    # Ignore other non-TEXT frames (e.g., CONTINUATION, unexpected types)
                    logger.debug(f"Ignoring non-TEXT initial frame: {msg.type!r}")
                    continue

            # Parse the JSON handshake
            try:
                initial_data = json.loads(first_msg)
            except json.JSONDecodeError:
                logger.error(f"Initial chat handshake was not valid JSON: {first_msg!r}")
                await ws.send_json({'type': 'error', 'message': 'Initial message must be valid JSON.'})
                await ws.close()
                return ws

            room_id = initial_data.get('room_id')
            display_name = initial_data.get('displayName', 'Anonymous')

            if not room_id:
                logger.error(f"Chat WebSocket initial message missing room_id: {initial_data}")
                await ws.send_json({'type': 'error', 'message': 'room_id is required.'})
                await ws.close()
                return ws

            # Register the WebSocket to the chat room
            if not hasattr(self.bot, "chat_ws_rooms"):
                self.bot.chat_ws_rooms = {}
            if room_id not in self.bot.chat_ws_rooms:
                self.bot.chat_ws_rooms[room_id] = set()
            self.bot.chat_ws_rooms[room_id].add(ws)
            logger.info(
                f"Chat client '{display_name}' connected to room {room_id}. "
                f"Total chat connections: {len(self.bot.chat_ws_rooms[room_id])}"
            )

            # Broadcast join message to the room
            join_message = {
                'type': 'user_joined',
                'room_id': room_id,
                'displayName': display_name,
                'timestamp': int(time.time())
            }
            for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
                try:
                    await client_ws.send_json(join_message)
                except (ConnectionResetError, RuntimeError):
                    logger.warning(
                        f"Could not send join message to a client in room {room_id}. Connection issue."
                    )

            # --- Main receive loop for subsequent messages ---
            async for msg
