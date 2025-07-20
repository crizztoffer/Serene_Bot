# --- cogs/admin_commands/view.py ---

from __future__ import annotations # Defer evaluation of type hints

import discord
from discord import app_commands
from discord.ui import Button, View
import logging

logger = logging.getLogger(__name__)

# The command handler for /serene admin view
@app_commands.command(name="view", description="View flagged users.")
@app_commands.checks.has_permissions(administrator=True) # Restrict to admins only
async def view_command(
    interaction: discord.Interaction
):
    """
    Creates an embed with a link to view flagged users.
    The link is presented as a button that only admins can interact with.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction immediately

    # Placeholder URL for viewing flagged users.
    # You will need to replace this with the actual URL to your dashboard or system
    # where flagged users can be viewed.
    view_url = "https://example.com/admin/flagged_users"

    # Create the embed message
    embed = discord.Embed(
        title="Flagged Users Dashboard",
        description="Click the button below to view the list of flagged users and their details.",
        color=discord.Color.red()
    )
    embed.add_field(name="Access", value="This link is only accessible to administrators.", inline=False)
    embed.set_footer(text="Ensure you are logged in with appropriate permissions.")

    # Create a button that opens the URL
    # The URL button itself handles the permission, as Discord will only open it for the user clicking.
    # The command itself is already restricted by @app_commands.checks.has_permissions(administrator=True)
    button = Button(
        label="View Flagged Users",
        style=discord.ButtonStyle.url,
        url=view_url,
        emoji="📊" # A suitable emoji for a dashboard/report
    )

    # Create a View and add the button to it
    view = View()
    view.add_item(button)

    # Send the embed with the button
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    logger.info(f"Admin {interaction.user.display_name} requested to view flagged users.")

# This 'start' function is called by admin_main.py's cog_load method.
# It receives the admin_group (which is /serene admin) and the bot instance.
async def start(admin_group: app_commands.Group, bot):
    """
    Registers the view_command as a subcommand of the /serene admin group.
    """
    admin_group.add_command(view_command)
    logger.info("View command added to '/serene admin' group.")
    # No need to sync bot.tree here, as admin_main.py's setup will sync it after all cogs are loaded.
