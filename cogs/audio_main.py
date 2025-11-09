# --- cogs/audio_main.py ---

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import os
import logging
import io
try:
    import pydub
except ImportError:
    pydub = None

logger = logging.getLogger(__name__)

# --- Constants ---
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
# --- THIS IS THE MISSING LINE ---
SOUND_NAME_RE = re.compile(r'^([A-Za-z0-9_-]{1,64})$') 
# --- END FIX ---
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"
# We no longer need the UPLOAD_URL or CONVERTER_SECRET

class AudioMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        if pydub is None:
            logger.critical("!!! pydub library is not installed! Audio conversion will fail. !!!")

    def cog_unload(self):
        """Cog shutdown cleanup."""
        try:
            if not self.http_session.closed:
                asyncio.create_task(self.http_session.close())
        except Exception:
            pass

    def _get_file_url(self, name: str, extension: str) -> str:
        """Gets the full, direct URL to a sound file."""
        return f"{SOUND_BASE_URL}/{name}.{extension}"

    async def _download_file_data(self, url: str) -> Optional[bytes]:
        """Downloads the raw bytes of a file from a URL."""
        try:
            async with self.http_session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    logger.warning(f"Failed to download {url} (status: {resp.status})")
                    return None
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
            return None

    def _convert_ogg_to_mp3(self, ogg_data: bytes) -> Optional[io.BytesIO]:
        """Converts OGG byte data to MP3 byte data in memory."""
        if pydub is None:
            logger.error("Cannot convert: pydub library is not installed.")
            return None
        try:
            ogg_in_memory = io.BytesIO(ogg_data)
            mp3_in_memory = io.BytesIO()

            audio = pydub.AudioSegment.from_file(ogg_in_memory, format="ogg")
            audio.export(mp3_in_memory, format="mp3", bitrate="192k")
            
            mp3_in_memory.seek(0)
            return mp3_in_memory
        except pydub.exceptions.CouldntFindConversionTool:
            logger.critical("!!! FFmpeg (or libav) NOT FOUND on Railway !!!")
            logger.critical("Bot cannot convert audio. Add FFmpeg to your Railway environment (e.g., via nixpacks.toml).")
            return None
        except Exception as e:
            logger.error(f"In-memory conversion failed: {e}")
            return None

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        This is the GLOBAL error handler for the bot.
        """
        
        if isinstance(error, commands.CommandNotFound):
            sound_name = ctx.invoked_with
            full_message_match = SOUND_COMMAND_RE.match(ctx.message.content)

            # 2. Check if it's a sound command
            if sound_name and SOUND_NAME_RE.match(sound_name) and full_message_match:
                mp3_url = self._get_file_url(sound_name, "mp3")
                ogg_url = self._get_file_url(sound_name, "ogg")

                try:
                    # 1. Check for existing MP3
                    mp3_data = await self._download_file_data(mp3_url)
                    if mp3_data:
                        mp3_file = discord.File(io.BytesIO(mp3_data), filename=f"{sound_name}.mp3")
                        await ctx.send(file=mp3_file)
                        return # Handled.

                    # 2. No MP3. Check for OGG
                    ogg_data = await self._download_file_data(ogg_url)
                    if ogg_data:
                        await ctx.message.add_reaction("⏳")
                        
                        # Convert OGG to MP3 in memory
                        mp3_file_data = self._convert_ogg_to_mp3(ogg_data)
                        
                        await ctx.message.remove_reaction("⏳", self.bot.user)

                        if mp3_file_data:
                            # Send the *newly converted* MP3 file
                            mp3_file = discord.File(mp3_file_data, filename=f"{sound_name}.mp3")
                            await ctx.send(file=mp3_file)
                        else:
                            # Conversion failed, upload the OGG as a fallback
                            ogg_file = discord.File(io.BytesIO(ogg_data), filename=f"{sound_name}.ogg")
                            await ctx.send(file=ogg_file)
                        return # Handled.

                    # 3. Neither file found
                    await ctx.message.add_reaction("❓")
                    return # Handled.

                except discord.errors.Forbidden:
                    logger.warning(f"Failed to send sound or react in {ctx.channel.id}.")
                    return # Still "handled"
                except Exception as e:
                    logger.error(f"Error processing sound command: {e}")
                    return # Still "handled"
            
            # 3. It IS CommandNotFound, but it was NOT a sound.
            await ctx.send("Command not found.")
            return

        # 4. Handle all OTHER error types (copied from bot.py)
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: {error.param.name}.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("You lack permissions.")
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"Command invoke error for '{ctx.command}': {error.original}")
            await ctx.send(f"An unexpected error occurred: {error.original}")
        else:
            logger.error(f"Unhandled command error: {error}")
            await ctx.send(f"Unexpected error: {error}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
