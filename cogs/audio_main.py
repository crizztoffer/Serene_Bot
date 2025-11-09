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
# Regex to match a valid sound name.
# Does NOT include the '!' prefix.
SOUND_NAME_RE = re.compile(r'^([A-Za-z0-9_-]{1,64})$')
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
                # Any other status (like 404)
                return False
        except Exception as e:
            logger.warning(f"Failed to check sound URL {url}: {e}")
            return False

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        Listens for command errors. If a command is "not found,"
        this will check if it's a valid sound name and play it.
        """
        
        if isinstance(error, commands.CommandNotFound):
            # 'ctx.invoked_with' is the command name the user *tried* to use, e.g., "ha7"
            command_name = ctx.invoked_with
            
            # Check if this "command name" looks like a sound
            if command_name and SOUND_NAME_RE.match(command_name):
                url = self._sound_url(command_name)
                
                if await self._sound_exists(url):
                    # --- SUCCESS ---
                    # It's a valid sound. Post the URL and stop.
                    try:
                        await ctx.send(url)
                    except Exception as e:
                        logger.error(f"Error sending sound URL: {e}")
                    finally:
                        return # Stop all further error processing
                else:
                    # --- FAILED SOUND ---
                    # It *looked* like a sound, but the file doesn't exist.
                    # Be silent and stop processing.
                    return 

        # --- FALLTHROUGH FOR OTHER ERRORS ---
        # If the error was *not* a CommandNotFound, or if it was
        # CommandNotFound but *didn't* match the sound regex,
        # handle it normally (replicating your bot.py logic).
        
        if isinstance(error, commands.CommandNotFound):
            # This will only be reached if the command was NOT a sound.
            await ctx.send("Command not found.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: {error.param.name}.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("You lack permissions.")
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"Command invoke error for '{ctx.command}': {error.original}")
            await ctx.send(f"An unexpected error occurred: {error.original}")
        else:
            logger.error(f"Unhandled command error: {error}")
            # You may or may not want to send this to the channel
            # await ctx.send(f"Unexpected error: {error}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioMain(bot))
