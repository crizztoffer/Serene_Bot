# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
import json
import aiomysql

# Autocomplete for flag reasons from bot.flag_reasons
async def autocomplete_flag_reasons(interaction: discord.Interaction, current: str):
    reasons = getattr(interaction.client, "flag_reasons", [])
    return [
        app_commands.Choice(name=reason, value=reason)
        for reason in reasons if current.lower() in reason.lower()
    ][:25]

# The start() function called by admin_main.py
async def start(interaction: discord.Interaction, bot):
    # Dynamically create a subcommand under /serene for flagging
    serene_group = bot.tree.get_command("serene")
    if serene_group is None:
        await interaction.response.send_message("Command group '/serene' not found.", ephemeral=True)
        return

    # Check if flag already exists to avoid duplication
    if serene_group.get_command("flag"):
        await interaction.response.send_message("The '/serene flag' command is already registered.", ephemeral=True)
        return

    @app_commands.command(
        name="flag",
        description="Flag user(s) for a rule violation"
    )
    @app_commands.describe(
        reason="Reason for the flag",
        users="User(s) to flag"
    )
    @app_commands.autocomplete(reason=autocomplete_flag_reasons)
    async def flag_command(
        interaction: discord.Interaction,
        reason: str,
        users: list[discord.Member]
    ):
        await interaction.response.defer(ephemeral=True)

        if not users:
            await interaction.followup.send("You must mention at least one user to flag.", ephemeral=True)
            return

        DB_USER = getattr(bot, "DB_USER", None)
        DB_PASSWORD = getattr(bot, "DB_PASSWORD", None)
        DB_HOST = getattr(bot, "DB_HOST", None)

        if not all([DB_USER, DB_PASSWORD, DB_HOST]):
            await interaction.followup.send("Database credentials are missing.", ephemeral=True)
            return

        conn = None
        try:
            conn = await aiomysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
                db="serene_users", charset='utf8mb4', autocommit=True
            )
            async with conn.cursor() as cursor:
                # Get the rule action (flag or ban)
                await cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s LIMIT 1", (reason,))
                result = await cursor.fetchone()
                if not result:
                    await interaction.followup.send(f"No rule found for reason '{reason}'.", ephemeral=True)
                    return

                action = result[0]  # "flag" or "ban"

                for user in users:
                    await cursor.execute(
                        "SELECT json_data FROM discord_users WHERE discord_id = %s",
                        (str(user.id),)
                    )
                    row = await cursor.fetchone()
                    if not row:
                        continue

                    try:
                        data = json.loads(row[0]) if row[0] else {}
                    except json.JSONDecodeError:
                        data = {}

                    warnings = data.setdefault("warnings", {})
                    flags = warnings.setdefault("flags", [])
                    strikes = warnings.setdefault("strikes", [])

                    existing_flag = next((f for f in flags if f.get("reason") == reason), None)

                    if action == "flag":
                        if existing_flag:
                            # Already flagged before → Strike
                            existing_strike = next((s for s in strikes if s.get("reason") == reason), None)
                            if existing_strike:
                                existing_strike["strike_number"] = existing_strike.get("strike_number", 1) + 1
                            else:
                                strikes.append({"reason": reason, "strike_number": 1})
                        else:
                            flags.append({"reason": reason, "seen": False})

                    elif action == "ban":
                        if not existing_flag:
                            flags.append({"reason": reason, "seen": False})
                        existing_numbers = {s.get("strike_number") for s in strikes if s.get("reason") == reason}
                        for i in range(1, 4):
                            if i not in existing_numbers:
                                strikes.append({"reason": reason, "strike_number": i})

                    updated_json = json.dumps(data)
                    await cursor.execute(
                        "UPDATE discord_users SET json_data = %s WHERE discord_id = %s",
                        (updated_json, str(user.id))
                    )

            await interaction.followup.send(
                f"Users flagged for '{reason}' with action '{action}'.", ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"Flagging error: {e}", ephemeral=True)
        finally:
            if conn:
                await conn.ensure_closed()

    # Register the command dynamically under the serene group
    serene_group.add_command(flag_command)
    await bot.tree.sync()

    await interaction.response.send_message("Flag command loaded and ready to use.", ephemeral=True)
