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

# Regex to find ![name] or ![name] 150
# We will capture the rate (group 2) but ignore it,
# as Discord's embed player cannot change playback speed.
DISCORD_SOUND_RE = re.compile(r'!\[\s*([A-Za-z0-9_-]{1,64})\s*\](?:\s+(\d{2,3}))?')
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

    def _sound_url(self, name: str) -> str:
        """Gets the full URL for a sound name."""
        return f"{SOUND_BASE_URL}/{name}.ogg"

    async def _sound_exists(self, url: str) -> bool:
        """Checks if a sound file URL is accessible."""
        try:
            # We use HEAD request for speed, as we only need status
            async with self.http_session.head(url, allow_redirects=True, timeout=5) as resp:
                if resp.status == 200:
                    return True
                # Fallback for servers that don't like HEAD
                if resp.status in (403, 405):
                    headers = {"Range": "bytes=0-0"}
                    async with self.http_session.get(url, headers=headers, allow_redirects=True, timeout=5) as get_resp:
                        return get_resp.status in (200, 206)
                return False
        except Exception as e:
            logger.warning(f"Failed to check sound URL {url}: {e}")
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots, DMs, and messages without content
        if message.author.bot or not message.guild or not message.content:
            return

        # Check if the message matches the ![sound] format
        match = DISCORD_SOUND_RE.search(message.content)
        if not match:
            return

        # --- We have a match, process the sound ---
        sound_name = match.group(1)
        # We parse the rate (match.group(2)) but deliberately ignore it.
        
        url = self._sound_url(sound_name)

        # 1. Check if sound exists
        if not await self._sound_exists(url):
            try:
                # Add a 'not found' reaction
                await message.add_reaction("❓") 
            except discord.HTTPException:
                pass # Ignore if we can't add reaction
            return

        # 2. Post the URL
        try:
            # Send the URL. Discord will auto-embed the player.
            await message.channel.send(url)
            
            # 3. (Optional) Delete the user's triggering message to keep chat clean
            await message.delete()

        except discord.errors.Forbidden:
            logger.warning(f"Failed to send sound or delete message in {message.channel.id}. Check permissions.")
        except Exception as e:
            logger.error(f"Error sending sound URL: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
