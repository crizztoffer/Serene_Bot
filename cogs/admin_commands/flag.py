import discord
from discord.ext import commands
from discord import app_commands
import aiomysql
import json
import re

async def start(interaction: discord.Interaction, bot: commands.Bot):
    # Fetch reasons from DB for dropdown choices
    try:
        conn = await aiomysql.connect(
            host=bot.db_host,
            user=bot.db_user,
            password=bot.db_pass,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT reason FROM rule_flagging WHERE action IN ('flag', 'ban')")
            rows = await cursor.fetchall()
            reason_choices = [
                app_commands.Choice(name=row["reason"], value=row["reason"])
                for row in rows
            ]
    except Exception as e:
        await interaction.response.send_message(f"Database error fetching flag reasons: {e}", ephemeral=True)
        return

    # Define command dynamically
    @app_commands.command(name="flag", description="Flag users for a rule violation")
    @app_commands.choices(reason=reason_choices)
    async def flag(
        interaction: discord.Interaction,
        reason: app_commands.Choice[str],
        users: str
    ):
        await interaction.response.defer(ephemeral=True)

        reason_text = reason.value
        user_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", users)]

        if not user_ids:
            await interaction.followup.send("No valid user mentions found.", ephemeral=True)
            return

        try:
            conn = await aiomysql.connect(
                host=bot.db_host,
                user=bot.db_user,
                password=bot.db_pass,
                db="serene_users",
                charset='utf8mb4',
                autocommit=True
            )
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s", (reason_text,))
                rule = await cursor.fetchone()

                if not rule:
                    await interaction.followup.send("Invalid rule reason.", ephemeral=True)
                    return

                action = rule["action"]
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
                            pass

                    flags = user_flags["warnings"]["flags"]
                    strikes = user_flags["warnings"]["strikes"]

                    already_flagged = any(f["reason"] == reason_text for f in flags)
                    new_strike_num = sum(1 for s in strikes if s["reason"] == reason_text) + 1

                    if action == "ban":
                        flags.append({"reason": reason_text, "seen": False})
                        for i in range(1, 4):
                            strikes.append({"reason": reason_text, "strike_number": i})
                    else:
                        if already_flagged:
                            strikes.append({"reason": reason_text, "strike_number": new_strike_num})
                        else:
                            flags.append({"reason": reason_text, "seen": False})

                    updated_json = json.dumps(user_flags)
                    await cursor.execute(
                        "UPDATE user_data SET flags = %s WHERE discord_id = %s",
                        (updated_json, uid)
                    )

                    user = await bot.fetch_user(uid)
                    updates.append(f"{user.mention}: {'Strike' if already_flagged else 'Flagged'}")

                await interaction.followup.send(
                    f"✅ Reason: **{reason_text}**\n" + "\n".join(updates),
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ DB or logic error: {e}", ephemeral=True)

    # Add to /serene group
    serene_group = bot.tree.get_command("serene")
    if serene_group:
        serene_group.add_command(flag)
        await bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send("✅ Flag command registered.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Could not find '/serene' group.", ephemeral=True)
