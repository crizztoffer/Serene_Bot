# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
from typing import List
import json
import aiomysql

# Autocomplete for the 'reason' option
async def autocomplete_flag_reasons(interaction: discord.Interaction, current: str):
    reasons = getattr(interaction.client, "flag_reasons", [])
    return [
        app_commands.Choice(name=reason, value=reason)
        for reason in reasons
        if current.lower() in reason.lower()
    ]

# The command handler
@app_commands.command(name="flag", description="Flag one or more users for a rule violation.")
@app_commands.describe(reason="Choose a reason from the rules")
@app_commands.autocomplete(reason=autocomplete_flag_reasons)
async def flag_command(
    interaction: discord.Interaction,
    reason: str,
    users: List[discord.User]
):
    await interaction.response.defer(ephemeral=True)

    db_user = interaction.client.db_user
    db_password = interaction.client.db_password
    db_host = interaction.client.db_host

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
                    continue

                json_data = json.loads(row[0])
                warnings = json_data.get("warnings", {})
                flags = warnings.setdefault("flags", [])
                strikes = warnings.setdefault("strikes", [])

                flag_exists = any(flag.get("reason") == reason for flag in flags)
                if flag_exists:
                    # Already flagged → add a strike
                    strike_count = sum(1 for s in strikes if s.get("reason") == reason)
                    strikes.append({
                        "reason": reason,
                        "strike_number": strike_count + 1
                    })
                else:
                    # First time being flagged
                    flags.append({
                        "reason": reason,
                        "seen": False
                    })

                json_data["warnings"] = {
                    "flags": flags,
                    "strikes": strikes
                }

                updated_json = json.dumps(json_data)
                await cursor.execute(
                    "UPDATE discord_users SET json_data = %s WHERE discord_id = %s",
                    (updated_json, str(user.id))
                )

        await interaction.followup.send(f"Flag applied for reason: **{reason}**", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Database error: {e}", ephemeral=True)
    finally:
        if conn:
            await conn.ensure_closed()

# This is called from admin_main.py after `/serene admin flag` is triggered
async def start(interaction: discord.Interaction, bot):
    serene_group = bot.tree.get_command("serene")
    if serene_group is None:
        await interaction.response.send_message("Serene group not found.", ephemeral=True)
        return

    serene_group.add_command(flag_command)
    await bot.tree.sync()  # Ensure the new command is known to Discord

    await interaction.response.send_message("`/serene flag` command loaded. Now you can use it.", ephemeral=True)
