# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
from discord.ext import commands

async def start(admin_group: app_commands.Group, bot: commands.Bot):
    # Avoid duplicate registration
    if any(cmd.name == "flag" for cmd in admin_group.commands):
        return

    @app_commands.command(name="flag", description="Flag a user for violating a rule")
    @app_commands.describe(reason="The rule violated", user="The user to flag")
    async def flag_command(interaction: discord.Interaction, reason: str, user: discord.Member):
        try:
            guild_id = interaction.guild.id
            user_id = user.id

            # Fetch reasons from bot (loaded at startup)
            valid_reasons = getattr(bot, "flag_reasons", [])
            if reason not in valid_reasons:
                await interaction.response.send_message(
                    f"'{reason}' is not a valid reason.", ephemeral=True
                )
                return

            # Fetch user JSON from DB
            conn = await bot.get_db_connection()
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT json_data FROM discord_users WHERE channel_id=%s AND discord_id=%s",
                    (str(guild_id), str(user_id))
                )
                result = await cursor.fetchone()

                if not result:
                    await interaction.response.send_message("User not found in database.", ephemeral=True)
                    return

                json_data = result[0]
                data = json.loads(json_data)

                if "warnings" not in data:
                    data["warnings"] = {}

                data["warnings"][reason] = data["warnings"].get(reason, 0) + 1

                # Update user in DB
                await cursor.execute(
                    "UPDATE discord_users SET json_data=%s WHERE channel_id=%s AND discord_id=%s",
                    (json.dumps(data), str(guild_id), str(user_id))
                )

            await interaction.response.send_message(
                f"User {user.mention} has been flagged for '{reason}'.", ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @flag_command.autocomplete("reason")
    async def autocomplete_reason(interaction: discord.Interaction, current: str):
        reasons = getattr(bot, "flag_reasons", [])
        return [
            app_commands.Choice(name=r, value=r)
            for r in reasons if current.lower() in r.lower()
        ]

    # Add the command to the admin group
    admin_group.add_command(flag_command)
