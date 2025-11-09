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
# Regex to match a valid sound name *without* the prefix
SOUND_NAME_RE = re.compile(r'^([A-Za-z0-9_-]{1,64})$')
# Regex to match the *full* command, including optional rate
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"
UPLOAD_URL = "https://serenekeks.com/upload_sound.php" 
UPLOADER_SECRET = os.getenv("CONVERTER_SECRET")

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

    async def _sound_exists(self, url: str) -> bool:
        """Checks if a sound file URL is accessible."""
        try:
            async with self.http_session.head(url, allow_redirects=True, timeout=5) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (403, 405): # Fallback
                    headers = {"Range": "bytes=0-0"}
                    async with self.http_session.get(url, headers=headers, allow_redirects=True, timeout=5) as get_resp:
                        return get_resp.status in (200, 206)
                return False
        except Exception as e:
            logger.warning(f"Failed to check sound URL {url}: {e}")
            return False

    async def _convert_and_upload(self, sound_name: str, ogg_url: str) -> bool:
        """
        Downloads OGG, converts to MP3 in memory, and uploads to PHP script.
        Returns True on success, False on failure.
        """
        if not UPLOADER_SECRET:
            logger.error("UPLOADER_SECRET is not set. Cannot upload MP3.")
            return False
        if pydub is None:
            logger.error("pydub is not installed. Cannot convert audio.")
            return False

        try:
            # 1. Download OGG data
            async with self.http_session.get(ogg_url) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to download OGG {ogg_url} for conversion.")
                    return False
                ogg_data = await resp.read()
            
            # 2. Convert in memory
            ogg_in_memory = io.BytesIO(ogg_data)
            mp3_in_memory = io.BytesIO()

            audio = pydub.AudioSegment.from_file(ogg_in_memory, format="ogg")
            audio.export(mp3_in_memory, format="mp3", bitrate="192k")
            
            mp3_in_memory.seek(0)
            mp3_data = mp3_in_memory.read()

        except pydub.exceptions.CouldntFindConversionTool:
            logger.critical("!!! FFmpeg (or libav) NOT FOUND on Railway !!!")
            logger.critical("Bot cannot convert audio. Add FFmpeg to your Railway environment (e.g., via nixpacks.toml).")
            return False
        except Exception as e:
            logger.error(f"In-memory conversion failed for {sound_name}: {e}")
            return False

        # 3. Upload MP3 data to PHP script
        try:
            data = aiohttp.FormData()
            data.add_field('secret', UPLOADER_SECRET)
            data.add_field('s', sound_name)
            data.add_field('mp3file', mp3_data,
                           filename=f"{sound_name}.mp3",
                           content_type='audio/mpeg')

            async with self.http_session.post(UPLOAD_URL, data=data, timeout=60) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Successfully uploaded new MP3 for '{sound_name}'.")
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"MP3 upload for '{sound_name}' failed with status {resp.status}: {text}")
                    return False
        except Exception as e:
            logger.error(f"Error uploading MP3 for '{sound_name}': {e}")
            return False

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        This is now the GLOBAL error handler for the bot.
        It must be the *only* on_command_error listener.
        """

        # 1. Check if it's a CommandNotFound error
        if isinstance(error, commands.CommandNotFound):
            sound_name = ctx.invoked_with
            # Check if the *full message* matches the sound command regex
            full_message_match = SOUND_COMMAND_RE.match(ctx.message.content)

            # 2. Check if it's a sound command
            if sound_name and SOUND_NAME_RE.match(sound_name) and full_message_match:
                mp3_url = self._get_file_url(sound_name, "mp3")
                ogg_url = self._get_file_url(sound_name, "ogg")

                try:
                    # Check for MP3
                    if await self._sound_exists(mp3_url):
                        await ctx.send(mp3_url)
                        return # Handled.

                    # Check for OGG
                    if await self._sound_exists(ogg_url):
                        await ctx.message.add_reaction("⏳")
                        conversion_success = await self._convert_and_upload(sound_name, ogg_url)
                        await ctx.message.remove_reaction("⏳", self.bot.user)

                        if conversion_success:
                            await ctx.send(mp3_url)
                        else:
                            await ctx.send(ogg_url) # Fallback
                        return # Handled.

                    # Neither found
                    await ctx.message.add_reaction("❓")
                    return # Handled.

                except discord.errors.Forbidden:
                    logger.warning(f"Failed to send sound or react in {ctx.channel.id}.")
                    return # Still "handled"
                except Exception as e:
                    logger.error(f"Error processing sound command: {e}")
                    return # Still "handled"
            
            # 3. It IS CommandNotFound, but it was NOT a sound.
            # Send the "Command not found" message.
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
