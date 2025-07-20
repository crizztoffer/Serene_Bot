# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands, Interaction
import aiomysql
import json
from datetime import datetime


def start(admin_group: app_commands.Group, bot):
    """
    Register the /serene admin flag command under the given admin_group.
    """

    class FlagReasonTransformer(app_commands.Transformer):
        async def transform(self, interaction: Interaction, value: str) -> str:
            return value

        async def autocomplete(self, interaction: Interaction, current: str):
            reasons = getattr(bot, "flag_reasons", [])
            return [
                app_commands.Choice(name=r, value=r)
                for r in reasons
                if current.lower() in r.lower()
            ][:25]

    @admin_group.command(
        name="flag",
        description="Flag a user for breaking server rules."
    )
    @app_commands.describe(
        user="The user to flag",
        reason="Reason for flagging"
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.rename(reason="reason")
    async def flag(
        interaction: Interaction,
        user: discord.Member,
        reason: app_commands.Transform[str, FlagReasonTransformer]
    ):
        # Connect to DB and update JSON for the user
        conn = None
        try:
            conn = await aiomysql.connect(
                host=bot.db_host,
                user=bot.db_user,
                password=bot.db_password,
                db="serene_users",
                charset='utf8mb4',
                autocommit=True
            )
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT json_data FROM discord_users WHERE channel_id = %s AND discord_id = %s",
                    (str(interaction.guild.id), str(user.id))
                )
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(
                        f"User {user.mention} is not in the database.", ephemeral=True
                    )
                    return

                # Parse and modify JSON
                json_data = json.loads(row[0])
                warnings = json_data.get("warnings", {})

                now = datetime.utcnow().isoformat()
                flag_entry = {
                    "reason": reason,
                    "flagged_by": interaction.user.id,
                    "timestamp": now
                }

                if reason not in warnings:
                    warnings[reason] = []

                warnings[reason].append(flag_entry)
                json_data["warnings"] = warnings

                await cursor.execute(
                    "UPDATE discord_users SET json_data = %s WHERE channel_id = %s AND discord_id = %s",
                    (json.dumps(json_data), str(interaction.guild.id), str(user.id))
                )

                await interaction.response.send_message(
                    f"{user.mention} has been flagged for: **{reason}**", ephemeral=True
                )

        except Exception as e:
            await interaction.response.send_message(
                f"Failed to flag user: {e}", ephemeral=True
            )
        finally:
            if conn:
                await conn.ensure_closed()
