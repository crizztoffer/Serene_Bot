# --- cogs/audio_main.py ---

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import logging
import io
from typing import Optional  # <-- was missing

try:
    import pydub
except ImportError:
    pydub = None

logger = logging.getLogger(__name__)

# --- Constants ---
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
SOUND_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')  # <-- you referenced this but hadn’t defined it
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

class AudioMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        if pydub is None:
            logger.critical("!!! pydub is not installed! Audio conversion will fail. !!!")

    def cog_unload(self):
        try:
            if not self.http_session.closed:
                asyncio.create_task(self.http_session.close())
        except Exception:
            pass

    def _get_file_url(self, name: str, extension: str) -> str:
        return f"{SOUND_BASE_URL}/{name}.{extension}"

    async def _download_file_data(self, url: str) -> Optional[bytes]:
        try:
            async with self.http_session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"Failed to download {url} (status: {resp.status})")
                return None
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
            return None

    def _convert_ogg_to_mp3(self, ogg_data: bytes) -> Optional[io.BytesIO]:
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
            logger.critical("!!! FFmpeg (or libav) NOT FOUND !!!")
            return None
        except Exception as e:
            logger.error(f"In-memory conversion failed: {e}")
            return None

    async def _send_attachment(self, ctx: commands.Context, data: bytes | io.BytesIO, filename: str):
        """
        Sends an attachment only (no URL text), ensuring Discord shows the inline player.
        """
        # Ensure we have a BytesIO ready
        file_obj = data if isinstance(data, io.BytesIO) else io.BytesIO(data)
        file_obj.seek(0)
        file = discord.File(file_obj, filename=filename)

        # Important: do NOT include a URL in 'content'; just the file.
        await ctx.send(file=file)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            sound_name = ctx.invoked_with
            full_message_match = SOUND_COMMAND_RE.match(ctx.message.content)

            if sound_name and SOUND_NAME_RE.match(sound_name) and full_message_match:
                mp3_url = self._get_file_url(sound_name, "mp3")
                ogg_url = self._get_file_url(sound_name, "ogg")

                try:
                    # 1) Try MP3 direct
                    mp3_data = await self._download_file_data(mp3_url)
                    if mp3_data:
                        await self._send_attachment(ctx, mp3_data, f"{sound_name}.mp3")
                        return

                    # 2) Try OGG, then convert to MP3
                    ogg_data = await self._download_file_data(ogg_url)
                    if ogg_data:
                        try:
                            await ctx.message.add_reaction("⏳")
                        except discord.Forbidden:
                            pass

                        mp3_file_data = self._convert_ogg_to_mp3(ogg_data)

                        try:
                            await ctx.message.remove_reaction("⏳", self.bot.user)
                        except discord.Forbidden:
                            pass

                        if mp3_file_data:
                            await self._send_attachment(ctx, mp3_file_data, f"{sound_name}.mp3")
                        else:
                            # Conversion failed: attach original OGG so at least it plays inline
                            await self._send_attachment(ctx, ogg_data, f"{sound_name}.ogg")
                        return

                    # 3) Neither exists
                    try:
                        await ctx.message.add_reaction("❓")
                    except discord.Forbidden:
                        pass
                    return

                except discord.errors.Forbidden:
                    logger.warning(f"Failed to send sound or react in {ctx.channel.id}.")
                    return
                except Exception as e:
                    logger.error(f"Error processing sound command: {e}")
                    return

            # Not a sound trigger; fallback message
            await ctx.send("Command not found.")
            return

        # Other error types
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
