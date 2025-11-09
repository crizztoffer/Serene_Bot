# --- cogs/audio_main.py ---

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import logging
import io
from typing import Optional

try:
    import pydub
except ImportError:
    pydub = None

logger = logging.getLogger(__name__)

# --- Constants ---
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
SOUND_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"


class AudioMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        if pydub is None:
            logger.critical("!!! pydub is not installed! Audio conversion/pitching will fail. !!!")

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

    def _segment_from_bytes(self, data: bytes, fmt: str) -> Optional["pydub.AudioSegment"]:
        """Decode bytes into a pydub.AudioSegment of given format ('mp3' or 'ogg')."""
        if pydub is None:
            logger.error("Cannot load audio: pydub not installed.")
            return None
        try:
            return pydub.AudioSegment.from_file(io.BytesIO(data), format=fmt)
        except Exception as e:
            logger.error(f"Failed to decode {fmt}: {e}")
            return None

    def _apply_pitch_percent(self, segment: "pydub.AudioSegment", percent: int) -> "pydub.AudioSegment":
        """
        Pitch shift via resampling.
        - 100 = original pitch
        - 200 ≈ +12 semitones (1 octave up)
        - 50  ≈ -12 semitones (1 octave down)
        Keeps duration approximately the same by resetting frame_rate.
        """
        factor = percent / 100.0
        new_rate = max(1000, int(segment.frame_rate * factor))  # sanity guard
        pitched = segment._spawn(segment.raw_data, overrides={"frame_rate": new_rate})
        return pitched.set_frame_rate(segment.frame_rate)

    def _export_mp3(self, segment: "pydub.AudioSegment") -> Optional[io.BytesIO]:
        """Export a pydub segment to MP3 in-memory (no disk writes)."""
        try:
            out_io = io.BytesIO()
            segment.export(out_io, format="mp3", bitrate="192k")
            out_io.seek(0)
            return out_io
        except Exception as e:
            logger.error(f"Export to MP3 failed: {e}")
            return None

    async def _reply_mp3_segment(self, ctx: commands.Context, segment: "pydub.AudioSegment", out_name: str):
        """
        Export 'segment' to MP3 in-memory and reply with it as an attachment named out_name.
        No files are saved to disk or persisted elsewhere.
        """
        out_io = self._export_mp3(segment)
        if out_io is None:
            await ctx.reply("Couldn't encode MP3 (FFmpeg/pydub issue).", mention_author=False)
            return
        await ctx.reply(file=discord.File(out_io, filename=out_name), mention_author=False)

    async def _reply_raw_bytes(self, ctx: commands.Context, data: bytes, out_name: str):
        """Reply with already-encoded bytes as an attachment (no text)."""
        bio = io.BytesIO(data)
        bio.seek(0)
        await ctx.reply(file=discord.File(bio, filename=out_name), mention_author=False)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        GLOBAL error handler.
        Intercepts CommandNotFound to treat '!<sound> [pitch]' as a sound trigger.
        Replies with the attachment (threaded under the user's message). No hourglass reactions.
        Shows the standard typing indicator during longer operations.
        """
        if isinstance(error, commands.CommandNotFound):
            sound_name = ctx.invoked_with
            full_message_match = SOUND_COMMAND_RE.match(ctx.message.content)

            # If it looks like a sound trigger: !<name> [pitch]
            if sound_name and SOUND_NAME_RE.match(sound_name) and full_message_match:
                # Parse optional pitch (group 2)
                pitch_percent: Optional[int] = None
                if full_message_match.group(2) is not None:
                    try:
                        pitch_percent = int(full_message_match.group(2))
                    except ValueError:
                        pitch_percent = None

                    # Validate 50..200 if provided
                    if pitch_percent is not None and not (50 <= pitch_percent <= 200):
                        await ctx.reply("Pitch must be an integer between **50** and **200**.", mention_author=False)
                        return

                mp3_url = self._get_file_url(sound_name, "mp3")
                ogg_url = self._get_file_url(sound_name, "ogg")

                try:
                    # Show "typing..." while we potentially download/convert
                    async with ctx.typing():
                        # 1) Try MP3 source
                        mp3_data = await self._download_file_data(mp3_url)
                        if mp3_data:
                            # If no pitch requested (or 100) or no pydub available, reply original bytes
                            if pitch_percent is None or pitch_percent == 100 or pydub is None:
                                await self._reply_raw_bytes(ctx, mp3_data, f"{sound_name}.mp3")
                                return

                            # Pitch in-memory, then export+reply as mp3 with original name
                            segment = self._segment_from_bytes(mp3_data, "mp3")
                            if segment is not None:
                                segment = self._apply_pitch_percent(segment, pitch_percent)
                                await self._reply_mp3_segment(ctx, segment, f"{sound_name}.mp3")
                                return

                            # Decode failed; reply original
                            await self._reply_raw_bytes(ctx, mp3_data, f"{sound_name}.mp3")
                            return

                        # 2) Try OGG source (decode → (pitch) → export MP3 → reply)
                        ogg_data = await self._download_file_data(ogg_url)
                        if ogg_data:
                            if pydub is None:
                                # No pydub: reply OGG as-is; still just an in-memory relay
                                await self._reply_raw_bytes(ctx, ogg_data, f"{sound_name}.ogg")
                                return

                            segment = self._segment_from_bytes(ogg_data, "ogg")
                            if segment is None:
                                await self._reply_raw_bytes(ctx, ogg_data, f"{sound_name}.ogg")
                                return

                            if pitch_percent is not None and pitch_percent != 100:
                                segment = self._apply_pitch_percent(segment, pitch_percent)

                            # Always export to MP3 for the reply (consistent container for attachments)
                            await self._reply_mp3_segment(ctx, segment, f"{sound_name}.mp3")
                            return

                    # 3) Neither file found
                    await ctx.reply("❓ Couldn’t find that sound.", mention_author=False)
                    return

                except discord.errors.Forbidden:
                    logger.warning(f"Failed to reply with sound in {ctx.channel.id}.")
                    return
                except Exception as e:
                    logger.error(f"Error processing sound command: {e}")
                    return

            # Not a sound trigger; fall back to the default message
            await ctx.send("Command not found.")
            return

        # Handle other error types (copied/kept from original)
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
