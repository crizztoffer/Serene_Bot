import discord
from discord.ext import commands
import json
import aiomysql
import re

async def start(interaction: discord.Interaction, bot: commands.Bot):
    await interaction.response.defer(ephemeral=True)

    try:
        # Parse the command options
        options = {opt.name: opt.value for opt in interaction.data.get("options", [])}
        reason = options.get("reason")
        mentions_input = options.get("users")

        if not reason or not mentions_input:
            await interaction.followup.send("You must provide a reason and at least one user mention.", ephemeral=True)
            return

        # Extract user IDs from the mentions
        user_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", mentions_input)]

        if not user_ids:
            await interaction.followup.send("No valid user mentions found.", ephemeral=True)
            return

        # Connect to MySQL
        conn = await aiomysql.connect(
            host=bot.db_host,
            user=bot.db_user,
            password=bot.db_pass,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            # Check the reason in the rule_flagging table
            await cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s", (reason,))
            rule = await cursor.fetchone()

            if not rule:
                await interaction.followup.send(f"The reason '{reason}' is not a valid rule violation.", ephemeral=True)
                return

            action_type = rule["action"].lower()

            updates = []
            for uid in user_ids:
                await cursor.execute("SELECT flags FROM user_data WHERE discord_id = %s", (uid,))
                row = await cursor.fetchone()

                user_flags = {
                    "warnings": {
                        "flags": [],
                        "strikes": []
                    }
                }

                if row and row.get("flags"):
                    try:
                        user_flags = json.loads(row["flags"])
                    except json.JSONDecodeError:
                        pass  # fallback to empty structure if bad JSON

                flags = user_flags["warnings"]["flags"]
                strikes = user_flags["warnings"]["strikes"]

                already_flagged = any(f["reason"] == reason for f in flags)
                new_strike_count = sum(1 for s in strikes if s["reason"] == reason) + 1

                if action_type == "ban":
                    # Insert flag and 3 strikes
                    flags.append({"reason": reason, "seen": False})
                    for i in range(1, 4):
                        strikes.append({"reason": reason, "strike_number": i})
                else:
                    if already_flagged:
                        strikes.append({
                            "reason": reason,
                            "strike_number": new_strike_count
                        })
                    else:
                        flags.append({
                            "reason": reason,
                            "seen": False
                        })

                updated_json = json.dumps(user_flags)

                await cursor.execute(
                    "UPDATE user_data SET flags = %s WHERE discord_id = %s",
                    (updated_json, uid)
                )

                user_obj = await bot.fetch_user(uid)
                updates.append(f"{user_obj.mention} — {'Flagged and Banned' if action_type == 'ban' else ('Strike Added' if already_flagged else 'Flag Added')}")

        await interaction.followup.send(
            f"✅ Processed {len(user_ids)} user(s) for reason: **{reason}**\n\n" + "\n".join(updates),
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
