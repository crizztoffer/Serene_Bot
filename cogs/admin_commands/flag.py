# --- cogs/admin_commands/flag.py ---

from __future__ import annotations

import discord
from discord import app_commands, User
import json
import aiomysql
import logging

logger = logging.getLogger(__name__)

# Autocomplete for the 'reason' option
async def autocomplete_flag_reasons(interaction: discord.Interaction, current: str):
    reasons = getattr(interaction.client, "flag_reasons", [])
    return [
        app_commands.Choice(name=reason, value=reason)
        for reason in reasons
        if current.lower() in reason.lower()
    ]

# Define the command as a regular function, no decorators
async def flag_command(
    interaction: discord.Interaction,
    reason: str,
    user: User
):
    await interaction.response.defer(ephemeral=True)

    db_user = interaction.client.db_user
    db_password = interaction.client.db_password
    db_host = interaction.client.db_host

    if not all([db_user, db_password, db_host]):
        await interaction.followup.send("Database credentials are not configured. Cannot flag user.", ephemeral=True)
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
            await cursor.execute(
                "SELECT json_data FROM discord_users WHERE discord_id = %s",
                (str(user.id),)
            )
            row = await cursor.fetchone()

            if not row:
                logger.info(f"User {user.display_name} ({user.id}) not found in DB. Adding now.")
                await interaction.client.add_user_to_db_if_not_exists(
                    interaction.guild_id, user.display_name, user.id
                )
                await cursor.execute(
                    "SELECT json_data FROM discord_users WHERE discord_id = %s",
                    (str(user.id),)
                )
                row = await cursor.fetchone()
                if not row:
                    await interaction.followup.send(f"Could not find or add user {user.display_name} to database.", ephemeral=True)
                    logger.error(f"Failed to add user {user.display_name} to DB.")
                    return

            json_data = json.loads(row[0])
            warnings = json_data.get("warnings", {})
            flags = warnings.setdefault("flags", [])
            strikes = warnings.setdefault("strikes", [])

            flag_exists = any(flag.get("reason") == reason for flag in flags)
            if flag_exists:
                strike_count = sum(1 for s in strikes if s.get("reason") == reason)
                strikes.append({
                    "reason": reason,
                    "strike_number": strike_count + 1,
                    "timestamp": discord.utils.utcnow().isoformat()
                })
                logger.info(f"Added strike for {user.display_name} for reason: {reason}. Total strikes: {strike_count + 1}")
            else:
                flags.append({
                    "reason": reason,
                    "seen": False,
                    "timestamp": discord.utils.utcnow().isoformat()
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

        await interaction.followup.send(
            f"✅ **{user.display_name}** has been flagged for: **{reason}**",
            ephemeral=True
        )

    except Exception as e:
        logger.error(f"Database error in flag_command: {e}", exc_info=True)
        await interaction.followup.send(f"An error occurred while flagging: {e}", ephemeral=True)
    finally:
        if conn:
            await conn.ensure_closed()

# Setup function that adds the command to the admin group
async def start(admin_group: app_commands.Group, bot):
    command = app_commands.Command(
        name="flag",
        description="Flag a user for a rule violation.",
        callback=flag_command,
        parameters=[
            app_commands.Parameter(
                name="reason",
                description="Choose a reason from the rules",
                type=str,
                autocomplete=autocomplete_flag_reasons,
            ),
            app_commands.Parameter(
                name="user",
                description="User to flag",
                type=discord.User,
            ),
        ],
    )

    admin_group.add_command(command)
    logger.info("Flag command added to '/serene admin' group.")
