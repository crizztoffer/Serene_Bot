# --- cogs/audio_main.py ---

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import os
import logging
import urllib.parse # Import for URL encoding
from typing import Optional

logger = logging.getLogger(__name__)

# --- Constants ---
# Regex to match "!soundname" and optional rate "!soundname 150"
# We capture the name (group 1) and the rate (group 2)
SOUND_COMMAND_RE = re.compile(r'^!([A-Za-z0-9_-]{1,64})(?:\s+(\d{2,3}))?$')
SOUND_BASE_URL = "https://serenekeks.com/serene_sounds"
# NEW: The URL to your embedder page
PLAYER_BASE_URL = "https://serenekeks.com/sound_player.php"

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
            async with self.http_session.head(ogg_url, allow_redirects=True, timeout=5) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (403, 405): # Fallback for servers that block HEAD
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
        This listener now replaces the on_command_error logic.
        It directly checks if a message *is* a sound command.
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
        
        # This will be run *before* the bot's command processor,
        # so we must "consume" the message to prevent a "Command not found"
        
        sound_name = match.group(1)
        # We capture the rate (match.group(2)) but ignore it, as the embed can't use it.
        
        # 1. Check if the *actual .ogg file* exists
        ogg_url = self._get_ogg_url(sound_name)
        if not await self._sound_exists(ogg_url):
            # It looked like a sound, but the file doesn't exist.
            # We can optionally react, but the important part is we DON'T run other commands.
            try:
                await message.add_reaction("❓")
            except discord.HTTPException:
                pass
            return # We are done.

        # 2. Build the URL to your *PHP player*
        safe_name = urllib.parse.quote_plus(sound_name)
        player_url = f"{PLAYER_BASE_URL}?s={safe_name}"

        # 3. Post the player URL
        try:
            await message.channel.send(player_url)
            # (Optional) Delete the user's triggering message
            # await message.delete()

        except discord.errors.Forbidden:
            logger.warning(f"Failed to send sound in {message.channel.id}. Check permissions.")
        except Exception as e:
            logger.error(f"Error sending sound URL: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        This is now just a standard error handler, since the sound logic
        is in on_message.
        """
        
        # This cog "consumes" CommandNotFound to prevent bot.py from firing
        if isinstance(error, commands.CommandNotFound):
            # We check if it was a sound command that *failed* the regex.
            # If on_message didn't catch it, it's a real "Command not found".
            if not SOUND_COMMAND_RE.match(ctx.message.content):
                 await ctx.send("Command not found.")
            # If it *did* match the regex but failed (e.g., file not found),
            # on_message handled it, so we just stay silent here.
            return

        # --- Handle other, non-sound-related errors ---
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: {error.param.name}.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("You lack permissions.")
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"Command invoke error for '{ctx.command}': {error.original}")
            await ctx.send(f"An unexpected error occurred: {error.original}")
        else:
            logger.error(f"Unhandled command error: {error}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
