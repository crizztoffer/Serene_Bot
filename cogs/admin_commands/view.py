# --- cogs/admin_commands/view.py ---

from __future__ import annotations  # Defer evaluation of type hints

import discord
from discord import app_commands
from discord.ui import Button, View
import logging

logger = logging.getLogger(__name__)

@app_commands.command(
    name="view",
    description="View flagged users."
)
@app_commands.checks.has_permissions(administrator=True)
async def view_command(interaction: discord.Interaction):
    """
    Creates an embed with a link to view flagged users.
    The link is presented as a button that only admins can interact with.
    """
    await interaction.response.defer(ephemeral=True)

    # Replace this with your actual admin dashboard URL
    view_url = "https://example.com/admin/flagged_users"

    embed = discord.Embed(
        title="Flagged Users Dashboard",
        description="Click the button below to view the list of flagged users and their details.",
        color=discord.Color.red()
    )
    embed.add_field(name="Access", value="This link is only accessible to administrators.", inline=False)
    embed.set_footer(text="Ensure you are logged in with appropriate permissions.")

    button = Button(
        label="View Flagged Users",
        style=discord.ButtonStyle.url,
        url=view_url,
        emoji="📊"
    )

    view = View()
    view.add_item(button)

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    logger.info(f"Admin {interaction.user.display_name} requested to view flagged users.")

# ✅ Expose for dynamic import
command = view_command
