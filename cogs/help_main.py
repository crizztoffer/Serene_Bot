# --- cogs/help_main.py ---

import discord
from discord.ext import commands
from discord import app_commands, Interaction
import aiohttp
import re
import logging
from typing import List, Set

logger = logging.getLogger(__name__)

# The URL to your public sounds directory
SOUNDS_DIRECTORY_URL = "https://serenekeks.com/serene_sounds/"

# Regex to find .ogg files in an HTML <a> tag
# This will match href="ha7.ogg" or href="/path/to/ha7.ogg"
# We only care about the filename itself.
HREF_RE = re.compile(r'href=["\']?([A-Za-z0-9_-]+\.ogg)["\']?', re.IGNORECASE)

class HelpModal(discord.ui.Modal, title='Serene Bot Commands'):
    """
    A simple modal that just displays text.
    The text input is used as a read-only, scrollable box.
    """
    def __init__(self, command_list: str):
        super().__init__(timeout=None)
        
        # Add a text input item, pre-filled with the command list.
        # It's set to 'paragraph' to allow for a large list and scrolling.
        self.command_field = discord.ui.TextInput(
            label='Available !sound commands',
            style=discord.TextStyle.paragraph,
            default=command_list,
            required=False,  # Makes it optional; users can still type but we ignore it.
            placeholder="No sound commands found."
        )
        self.add_item(self.command_field)

    async def on_submit(self, interaction: Interaction):
        # This modal is read-only, so we just acknowledge the "submission".
        await interaction.response.send_message("Closing help menu.", ephemeral=True, delete_after=5)

async def fetch_sound_commands() -> str:
    """
    Fetches the list of sound commands by scraping the directory listing.
    Returns a formatted string for the modal.
    """
    # Using a Set to avoid duplicates if a file is linked twice
    sound_names: Set[str] = set()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SOUNDS_DIRECTORY_URL) as resp:
                if resp.status == 200:
                    html_content = await resp.text()
                    
                    # Find all matches for .ogg files
                    matches = HREF_RE.findall(html_content)
                    
                    if not matches:
                        logger.warning("Failed to parse sounds: No .ogg files found in directory listing.")
                        return "Error: Could not parse the sound directory. Is directory listing enabled?"
                        
                    for ogg_filename in matches:
                        # Strip the ".ogg" extension to get the command name
                        sound_names.add(f"!{ogg_filename[:-4]}")
                        
                else:
                    logger.warning(f"Failed to fetch sound list. Status: {resp.status}")
                    return f"Error: Could not fetch command list from server (Status: {resp.status})."
                    
    except Exception as e:
        logger.error(f"Error fetching sound list: {e}")
        return "Error: Could not connect to command list server."

    if not sound_names:
        return "No sound commands found."
        
    # Sort the list alphabetically
    sorted_commands = sorted(list(sound_names))
    
    # Format the list with newlines for the modal
    return "\n".join(sorted_commands)

class HelpCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Get the /serene group from the bot
        serene_group = self.bot.tree.get_command("serene")
        if serene_group is None:
            raise commands.ExtensionFailed(self.qualified_name, "/serene group not found")

        # Define the /serene help command
        @app_commands.command(name="help", description="Shows a list of available bot commands.")
        async def serene_help(interaction: Interaction):
            # IMPORTANT: Do not defer; send the modal via interaction.response
            command_list_str = await fetch_sound_commands()
            modal = HelpModal(command_list=command_list_str)
            await interaction.response.send_modal(modal)

        # Add the 'help' command under the '/serene' group
        serene_group.add_command(serene_help)
        logger.info("✅ Registered /serene help command.")

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCommands(bot))
