# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
from typing import List
import json
import aiomysql
import logging

logger = logging.getLogger(__name__)

# Autocomplete for the 'reason' option
async def autocomplete_flag_reasons(interaction: discord.Interaction, current: str):
    """Provides autocomplete suggestions for flag reasons from the bot's loaded reasons."""
    reasons = getattr(interaction.client, "flag_reasons", [])
    return [
        app_commands.Choice(name=reason, value=reason)
        for reason in reasons
        if current.lower() in reason.lower()
    ]

# The command handler for /serene admin flag
@app_commands.command(name="flag", description="Flag one or more users for a rule violation.")
@app_commands.describe(reason="Choose a reason from the rules")
@app_commands.autocomplete(reason=autocomplete_flag_reasons)
async def flag_command(
    interaction: discord.Interaction,
    reason: str,
    users: List[discord.User]
):
    """
    Flags specified users for a given reason.
    If a user is already flagged for that reason, it adds a strike instead.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction immediately

    db_user = interaction.client.db_user
    db_password = interaction.client.db_password
    db_host = interaction.client.db_host

    if not all([db_user, db_password, db_host]):
        await interaction.followup.send("Database credentials are not configured. Cannot flag users.", ephemeral=True)
        logger.error("Missing DB credentials in flag_command.")
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=db_host,
            user=db_user,
            password=db_password,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            for user in users:
                await cursor.execute(
                    "SELECT json_data FROM discord_users WHERE discord_id = %s",
                    (str(user.id),)
                )
                row = await cursor.fetchone()

                if not row:
                    # If user not found in DB, add them before flagging
                    logger.info(f"User {user.display_name} ({user.id}) not found in DB. Adding now.")
                    # Use the add_user_to_db_if_not_exists function attached to the bot
                    await interaction.client.add_user_to_db_if_not_exists(
                        interaction.guild_id, user.display_name, user.id
                    )
                    # Re-fetch after adding
                    await cursor.execute(
                        "SELECT json_data FROM discord_users WHERE discord_id = %s",
                        (str(user.id),)
                    )
                    row = await cursor.fetchone()
                    if not row: # Still not found, something is wrong
                        await interaction.followup.send(f"Could not find or add user {user.display_name} to database. Skipping.", ephemeral=True)
                        logger.error(f"Failed to add user {user.display_name} to DB for flagging.")
                        continue

                json_data = json.loads(row[0])
                warnings = json_data.get("warnings", {})
                flags = warnings.setdefault("flags", [])
                strikes = warnings.setdefault("strikes", [])

                flag_exists = any(flag.get("reason") == reason for flag in flags)
                if flag_exists:
                    # User has already been flagged for this reason, so add a strike
                    strike_count = sum(1 for s in strikes if s.get("reason") == reason)
                    strikes.append({
                        "reason": reason,
                        "strike_number": strike_count + 1,
                        "timestamp": discord.utils.utcnow().isoformat() # Add timestamp
                    })
                    logger.info(f"Added strike for {user.display_name} for reason: {reason}. Total strikes: {strike_count + 1}")
                else:
                    # First time being flagged for this reason
                    flags.append({
                        "reason": reason,
                        "seen": False,
                        "timestamp": discord.utils.utcnow().isoformat() # Add timestamp
                    })
                    logger.info(f"Flagged {user.display_name} for reason: {reason}.")

                json_data["warnings"] = {
                    "flags": flags,
                    "strikes": strikes
                }

                updated_json = json.dumps(json_data)
                await cursor.execute(
                    "UPDATE discord_users SET json_data = %s WHERE discord_id = %s",
                    (updated_json, str(user.id))
                )
        
        # Send a single followup message after processing all users
        user_names = ", ".join([u.display_name for u in users])
        await interaction.followup.send(f"Flag applied for reason: **{reason}** to user(s): **{user_names}**", ephemeral=True)

    except Exception as e:
        logger.error(f"Database error in flag_command: {e}", exc_info=True) # Log full traceback
        await interaction.followup.send(f"An error occurred while flagging: {e}", ephemeral=True)
    finally:
        if conn:
            await conn.ensure_closed()

# This 'start' function is called by admin_main.py's cog_load method.
# It receives the admin_group (which is /serene admin) and the bot instance.
async def start(admin_group: app_commands.Group, bot):
    """
    Registers the flag_command as a subcommand of the /serene admin group.
    """
    admin_group.add_command(flag_command)
    logger.info("Flag command added to '/serene admin' group.")
    # No need to sync bot.tree here, as admin_main.py's setup will sync it after all cogs are loaded.
