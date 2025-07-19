import discord
from discord import app_commands

@app_commands.command(name="kick", description="Kick a user from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def command(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"✅ {member.mention} has been kicked. Reason: {reason}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to kick: {e}", ephemeral=True)
