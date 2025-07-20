# --- cogs/admin_commands/view.py ---

from __future__ import annotations

import discord
from discord import app_commands
from discord.ui import Button, View
import logging

logger = logging.getLogger(__name__)

# View command function
@app_commands.checks.has_permissions(administrator=True)
async def view_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    view_url = "https://example.com/admin/flagged_users"

    embed = discord.Embed(
        title="Flagged Users Dashboard",
        description="Click the button below to view flagged users.",
        color=discord.Color.red()
    )
    embed.add_field(name="Access", value="Admins only.", inline=False)
    embed.set_footer(text="Ensure proper login.")

    button = Button(label="View Flagged Users", style=discord.ButtonStyle.url, url=view_url, emoji="📊")
    view = View()
    view.add_item(button)

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# Hook
async def start(admin_group: app_commands.Group, bot):
    command = app_commands.Command(
        name="view",
        description="View flagged users.",
        callback=view_command
    )
    admin_group.add_command(command)
