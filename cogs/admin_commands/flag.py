# --- cogs/admin_commands/flag.py ---

from __future__ import annotations

import discord
from discord import app_commands, User
import json
import aiomysql
import logging

logger = logging.getLogger(__name__)

# Dynamically build the flag command with static choices from bot.flag_reasons
def build_flag_command(bot) -> app_commands.Command:
    reasons = getattr(bot, "flag_reasons", [])
    reason_choices = [
        app_commands.Choice(name=reason.title(), value=reason)
        for reason in reasons[:25]  # max 25 choices
    ]

    @app_commands.describe(reason="Choose a reason", user="User to flag")
    async def flag(
        interaction: discord.Interaction,
        reason: app_commands.Choice[str],
        user: User,
    ):
        await interaction.response.defer(ephemeral=True)

        db_user = interaction.client.db_user
        db_password = interaction.client.db_password
        db_host = interaction.client.db_host

        if not all([db_user, db_password, db_host]):
            await interaction.followup.send("Database credentials are not configured.", ephemeral=True)
            logger.error("Missing DB credentials.")
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
                await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                row = await cursor.fetchone()

                if not row:
                    logger.info(f"{user.display_name} not in DB. Attempting to add.")
                    await interaction.client.add_user_to_db_if_not_exists(interaction.guild_id, user.display_name, user.id)
                    await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                    row = await cursor.fetchone()
                    if not row:
                        await interaction.followup.send("Could not add user to DB.", ephemeral=True)
                        return

                json_data = json.loads(row[0])
                warnings = json_data.get("warnings", {})
                flags = warnings.setdefault("flags", [])
                strikes = warnings.setdefault("strikes", [])

                reason_str = reason.value

                if any(f.get("reason") == reason_str for f in flags):
                    strike_count = sum(1 for s in strikes if s.get("reason") == reason_str)
                    strikes.append({
                        "reason": reason_str,
                        "strike_number": strike_count + 1,
                        "timestamp": discord.utils.utcnow().isoformat()
                    })
                else:
                    flags.append({
                        "reason": reason_str,
                        "seen": False,
                        "timestamp": discord.utils.utcnow().isoformat()
                    })

                json_data["warnings"] = {"flags": flags, "strikes": strikes}
                await cursor.execute("UPDATE discord_users SET json_data = %s WHERE discord_id = %s", (json.dumps(json_data), str(user.id)))

            await interaction.followup.send(f"✅ **{user.display_name}** flagged for: **{reason_str}**", ephemeral=True)

        except Exception as e:
            logger.error(f"DB error: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while flagging.", ephemeral=True)
        finally:
            if conn:
                await conn.ensure_closed()

    # Attach choices explicitly
    flag = app_commands.Command(
        name="flag",
        description="Flag a user for a rule violation.",
        callback=flag
    )
    flag.parameters[0].choices = reason_choices  # Manually attach choices
    return flag

def start(admin_group: app_commands.Group, bot):
    admin_group.add_command(build_flag_command(bot))
