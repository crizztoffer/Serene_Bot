import discord
from discord import app_commands
from discord.ext import commands
import json
import pymysql
import os

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "cursorclass": pymysql.cursors.DictCursor
}

# Store reasons in memory after DB fetch
reasons_cache = {}

class ReasonTransformer(app_commands.Transformer):
    async def autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            connection = pymysql.connect(**DB_CONFIG)
            with connection.cursor() as cursor:
                cursor.execute("SELECT reason FROM rule_flagging")
                results = cursor.fetchall()
                matches = [r["reason"] for r in results if current.lower() in r["reason"].lower()]
                return [app_commands.Choice(name=r, value=r) for r in matches[:25]]
        except Exception as e:
            print(f"[Autocomplete Error]: {e}")
            return []

@app_commands.command(name="flag", description="Flag one or more users for a rule violation.")
@app_commands.describe(reason="The rule violation reason", users="Users to flag")
async def command(
    interaction: discord.Interaction,
    reason: app_commands.Transform[str, ReasonTransformer],
    users: app_commands.Transform[list[discord.User], app_commands.Greedy[discord.User]]
):
    await interaction.response.defer(ephemeral=True)

    if not users:
        await interaction.followup.send("No users specified.")
        return

    flagged = []
    struck = []

    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            # --- Step 1: Get action from rule_flagging table ---
            cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s", (reason,))
            result = cursor.fetchone()
            if not result:
                await interaction.followup.send(f"Reason '{reason}' not found in database.")
                return

            action_type = result["action"]

            for user in users:
                cursor.execute("SELECT moderation_data FROM users WHERE discord_id = %s", (str(user.id),))
                row = cursor.fetchone()

                if not row:
                    continue

                moderation_data = json.loads(row.get("moderation_data", "{}"))
                warnings = moderation_data.get("warnings", {})
                flags = warnings.get("flags", [])
                strikes = warnings.get("strikes", [])

                # --- Flags and Strikes Logic ---
                already_flagged = any(f["reason"] == reason for f in flags)
                current_strikes = [s for s in strikes if s["reason"] == reason]
                next_strike = 1 if not current_strikes else max(s["strike_number"] for s in current_strikes) + 1

                if action_type == "ban":
                    if not already_flagged:
                        flags.append({ "reason": reason, "seen": False })
                    for i in range(1, 4):  # Insert 3 strikes
                        strikes.append({ "reason": reason, "strike_number": i })
                    struck.append(user.mention + " (auto-ban level)")
                else:
                    if not already_flagged:
                        flags.append({ "reason": reason, "seen": False })
                        flagged.append(user.mention)
                    else:
                        strikes.append({ "reason": reason, "strike_number": next_strike })
                        struck.append(user.mention)

                new_data = {
                    "warnings": {
                        "flags": flags,
                        "strikes": strikes
                    }
                }

                cursor.execute(
                    "UPDATE users SET moderation_data = %s WHERE discord_id = %s",
                    (json.dumps(new_data), str(user.id))
                )

            connection.commit()

        msg = ""
        if flagged:
            msg += f"🚩 **Flagged**: {', '.join(flagged)} for `{reason}`\n"
        if struck:
            msg += f"⚠️ **Struck**: {', '.join(struck)} for `{reason}`"

        await interaction.followup.send(msg or "No action taken.")

    except Exception as e:
        await interaction.followup.send(f"Error during flagging: {e}")
