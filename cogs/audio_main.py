# --- cogs/audio_main.py ---

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# --- Constants ---
# Regex to match "!soundname" and optional rate "!soundname 150"
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"

class AudioMain(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http_session = aiohttp.ClientSession()

    def cog_unload(self):
        """Cog shutdown cleanup."""
        try:
            if not self.http_session.closed:
                asyncio.create_task(self.http_session.close())
        except Exception:
            pass

    def _get_ogg_url(self, name: str) -> str:
        """Gets the full, direct URL to the .ogg file for checking."""
        return f"{SOUND_BASE_URL}/{name}.ogg"

    async def _sound_exists(self, ogg_url: str) -> bool:
        """Checks if the actual .ogg file URL is accessible."""
        try:
            # We use HEAD request for speed, as we only need status
            async with self.http_session.head(ogg_url, allow_redirects=True, timeout=5) as resp:
                if resp.status == 200:
                    return True
                # Fallback for servers that block HEAD
                if resp.status in (403, 405):
                    headers = {"Range": "bytes=0-0"}
                    async with self.http_session.get(ogg_url, headers=headers, allow_redirects=True, timeout=5) as get_resp:
                        return get_resp.status in (200, 206)
                return False
        except Exception as e:
            logger.warning(f"Failed to check sound URL {ogg_url}: {e}")
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Listens for messages that look like a sound command.
        This runs *before* the bot's command processor.
        """
        # Ignore bots, DMs, and messages without the '!' prefix
        if message.author.bot or not message.guild or not message.content.startswith('!'):
            return

        # Check if the message content matches the sound command format
        match = SOUND_COMMAND_RE.match(message.content)
        if not match:
            # This isn't a sound command, so let the regular command processor handle it
            return

        # --- It *is* a sound command, so we handle it ---
        sound_name = match.group(1)
        # We capture the rate (match.group(2)) but ignore it.
        
        # 1. Check if the *actual .ogg file* exists
        ogg_url = self._get_ogg_url(sound_name)
        if not await self._sound_exists(ogg_url):
            # It looked like a sound, but the file doesn't exist.
            try:
                await message.add_reaction("❓")
            except discord.HTTPException:
                pass
            return # We are done.

        # 2. Build the direct .ogg URL
        # (ogg_url is already built)

        # 3. Post the direct .ogg link
        try:
            await message.channel.send(ogg_url)
            # (Optional) Delete the user's triggering message
            # await message.delete()

        except discord.errors.Forbidden:
            logger.warning(f"Failed to send sound in {message.channel.id}. Check permissions.")
        except Exception as e:
            logger.error(f"Error sending sound URL: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
