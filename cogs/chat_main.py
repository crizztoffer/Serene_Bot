import discord
from discord.ext import commands
from aiohttp import web
import json
import logging
import asyncio
import time

# Get the logger from the main bot file
logger = logging.getLogger(__name__)

class ChatMain(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def handle_chat_websocket(self, request):
        """
        Handles WebSocket connections for chat rooms.
        It registers a client to a chat room and broadcasts messages to all clients in that room.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        room_id = None
        display_name = "Anonymous"
        
        try:
            # First message from the client should be a JSON object with room_id and display_name
            first_msg = await ws.receive_str()
            initial_data = json.loads(first_msg)
            room_id = initial_data.get('room_id')
            display_name = initial_data.get('displayName', 'Anonymous')

            if not room_id:
                logger.error(f"Chat WebSocket initial message missing room_id: {initial_data}")
                await ws.send_json({'type': 'error', 'message': 'room_id is required.'})
                return

            # Register the WebSocket to the chat room
            if room_id not in self.bot.chat_ws_rooms:
                self.bot.chat_ws_rooms[room_id] = set()
            self.bot.chat_ws_rooms[room_id].add(ws)
            logger.info(f"Chat client '{display_name}' connected to room {room_id}. Total chat connections: {len(self.bot.chat_ws_rooms[room_id])}")

            # Broadcast join message to the room
            join_message = {
                'type': 'user_joined', 
                'room_id': room_id,
                'displayName': display_name, 
                'timestamp': int(time.time())
            }
            for client_ws in self.bot.chat_ws_rooms.get(room_id, set()):
                try:
                    await client_ws.send_json(join_message)
                except ConnectionResetError:
                    logger.warning(f"Could not send join message to a client in room {room_id}. Connection was reset.")

            # Listen for subsequent messages
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        message_text = data.get('message')
                        if message_text:
                            logger.info(f"Chat message from '{display_name}' in room {room_id}: {message_text}")
                            # Broadcast the new message to all clients in the room
                            chat_message = {
                                'type': 'new_message', 
                                'room_id': room_id,
                                'displayName': display_name, 
                                'message': message_text,
                                'timestamp': int(time.time())
                            }
                            for client_ws in self.bot.chat_ws_rooms.get(room_id, set()):
                                try:
                                    await client_ws.send_json(chat_message)
                                except ConnectionResetError:
                                    logger.warning(f"Could not send chat message to a client in room {room_id}. Connection was reset.")

                    except json.JSONDecodeError:
                        logger.error(f"Received malformed JSON from chat client in room {room_id}: {msg.data}")
                    except Exception as e:
                        logger.error(f"Error processing chat message in room {room_id}: {e}", exc_info=True)
                
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"Chat WebSocket error in room {room_id}: {ws.exception()}")
                elif msg.type == web.WSMsgType.CLOSE:
                    logger.info(f"Chat WebSocket client '{display_name}' closed connection from room {room_id}.")
                    break

        except asyncio.CancelledError:
            logger.info(f"Chat WebSocket connection for '{display_name}' in room {room_id} cancelled.")
        except Exception as e:
            logger.error(f"Error in handle_chat_websocket for room {room_id}: {e}", exc_info=True)
        finally:
            # Unregister and notify others
            if room_id and room_id in self.bot.chat_ws_rooms:
                if ws in self.bot.chat_ws_rooms[room_id]:
                    self.bot.chat_ws_rooms[room_id].remove(ws)
                
                logger.info(f"Chat client '{display_name}' disconnected from room {room_id}. Remaining connections: {len(self.bot.chat_ws_rooms.get(room_id, set()))}")
                
                # Broadcast leave message
                leave_message = {
                    'type': 'user_left', 
                    'room_id': room_id,
                    'displayName': display_name, 
                    'timestamp': int(time.time())
                }
                # Use a copy of the set for iteration to avoid issues if the set is modified
                for client_ws in list(self.bot.chat_ws_rooms.get(room_id, set())):
                    try:
                        await client_ws.send_json(leave_message)
                    except ConnectionResetError:
                        logger.warning(f"Could not send leave message to a client in room {room_id}. Connection was reset.")

                if not self.bot.chat_ws_rooms[room_id]:
                    del self.bot.chat_ws_rooms[room_id]
                    logger.info(f"Chat room {room_id} is now empty and has been closed.")
                
            return ws

async def setup(bot):
    await bot.add_cog(ChatMain(bot))
