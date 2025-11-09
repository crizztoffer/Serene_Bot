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
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

# URL to your uploader script
UPLOAD_URL = "https://serenekeks.com/upload_sound.php" 
# Get the secret from your bot's environment variables
UPLOADER_SECRET = os.getenv("CONVERTER_SECRET") # Or whatever you name it

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
            # This is synchronous, but should be fast for small sound files
            ogg_in_memory = io.BytesIO(ogg_data)
            mp3_in_memory = io.BytesIO()

            audio = pydub.AudioSegment.from_file(ogg_in_memory, format="ogg")
            audio.export(mp3_in_memory, format="mp3", bitrate="192k")
            
            mp3_in_memory.seek(0)
            mp3_data = mp3_in_memory.read()

        except pydub.exceptions.CouldntFindConversionTool as e:
            logger.critical("!!! FFmpeg (or libav) NOT FOUND on Railway !!!")
            logger.critical("Bot cannot convert audio. Add FFmpeg to your Railway environment (e.g., via nixpacks.toml).")
            logger.critical(f"Details: {e}")
            return False
        except Exception as e:
            logger.error(f"In-memory conversion failed for {sound_name}: {e}")
            return False

        # 3. Upload MP3 data to PHP script
        try:
            data = aiohttp.FormData()
            data.add_field('secret', UPLOADER_SECRET)
            data.add_field('s', sound_name)
            # 'mp3file' *must* match the $_FILES key in the PHP script
            data.add_field('mp3file', mp3_data,
                           filename=f"{sound_name}.mp3",
                           content_type='audio/mpeg')

            async with self.http_session.post(UPLOAD_URL, data=data, timeout=60) as resp:
                # 201 Created (new file) or 200 OK (already exists)
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
    async def on_message(self, message: discord.Message):
        """
        Listens for messages that look like a sound command.
        """
        if message.author.bot or not message.guild or not message.content.startswith('!'):
            return

        match = SOUND_COMMAND_RE.match(message.content)
        if not match:
            # This isn't a sound command, let the regular command processor handle it
            return

        sound_name = match.group(1)
        mp3_url = self._get_file_url(sound_name, "mp3")
        ogg_url = self._get_file_url(sound_name, "ogg")

        try:
            # 1. Check for MP3 (the preferred format)
            if await self._sound_exists(mp3_url):
                await message.channel.send(mp3_url)
                return

            # 2. No MP3. Check for OGG (the source format)
            if await self._sound_exists(ogg_url):
                # We found the OGG, so let's try to convert it.
                await message.add_reaction("⏳")
                
                conversion_success = await self._convert_and_upload(sound_name, ogg_url)
                
                await message.remove_reaction("⏳", self.bot.user)

                if conversion_success:
                    # Post the NEW MP3 link
                    await message.channel.send(mp3_url)
                else:
                    # Conversion failed, post the OGG as a fallback
                    await message.channel.send(ogg_url)
                return

            # 3. Neither file found
            await message.add_reaction("❓")
            
        except discord.errors.Forbidden:
            logger.warning(f"Failed to send sound or react in {message.channel.id}. Check permissions.")
        except Exception as e:
            logger.error(f"Error processing sound command: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
