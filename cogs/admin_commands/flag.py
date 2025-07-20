import discord
from discord.ext import commands
from discord import app_commands
import aiomysql
import json
import re

async def fetch_flag_reasons(bot, current: str):
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
            # Filter reasons matching current text input
            await cursor.execute(
                "SELECT reason FROM rule_flagging WHERE reason LIKE %s LIMIT 25",
                (f"%{current}%",)
            )
            rows = await cursor.fetchall()
            return [app_commands.Choice(name=row["reason"], value=row["reason"]) for row in rows]
    except Exception:
        return []

async def start(interaction: discord.Interaction, bot: commands.Bot):
    @app_commands.command(name="flag", description="Flag users for a rule violation")
    @app_commands.describe(reason="Reason for flagging", users="Mention one or more users")
    @app_commands.autocomplete(reason=lambda interaction, current: fetch_flag_reasons(bot, current))
    async def flag(
        interaction: discord.Interaction,
        reason: str,
        users: str
    ):
        await interaction.response.defer(ephemeral=True)

        # Extract user IDs from mentions string
        user_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", users)]
        if not user_ids:
            await interaction.followup.send("Please mention at least one user to flag.", ephemeral=True)
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
                await cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s", (reason,))
                rule = await cursor.fetchone()
                if not rule:
                    await interaction.followup.send("Invalid flag reason.", ephemeral=True)
                    return

                action = rule["action"]
                updates = []

                for uid in user_ids:
                    await cursor.execute("SELECT flags FROM user_data WHERE discord_id = %s", (uid,))
                    row = await cursor.fetchone()
                    user_flags = {"warnings": {"flags": [], "strikes": []}}
                    if row and row.get("flags"):
                        try:
                            user_flags = json.loads(row["flags"])
                        except Exception:
                            pass

                    flags = user_flags["warnings"]["flags"]
                    strikes = user_flags["warnings"]["strikes"]
                    already_flagged = any(f["reason"] == reason for f in flags)
                    new_strike_num = sum(1 for s in strikes if s["reason"] == reason) + 1

                    if action == "ban":
                        # Add flag and 3 strikes forcibly
                        flags.append({"reason": reason, "seen": False})
                        for i in range(1, 4):
                            strikes.append({"reason": reason, "strike_number": i})
                    else:
                        if already_flagged:
                            strikes.append({"reason": reason, "strike_number": new_strike_num})
                        else:
                            flags.append({"reason": reason, "seen": False})

                    updated_json = json.dumps(user_flags)
                    await cursor.execute(
                        "UPDATE user_data SET flags = %s WHERE discord_id = %s",
                        (updated_json, uid)
                    )

                    user = await bot.fetch_user(uid)
                    updates.append(f"{user.mention}: {'Strike' if already_flagged else 'Flagged'}")

                await interaction.followup.send(
                    f"✅ Reason: **{reason}**\n" + "\n".join(updates),
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Database or processing error: {e}", ephemeral=True)

    serene_group = bot.tree.get_command("serene")
    if serene_group:
        serene_group.add_command(flag)
        await bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send("✅ Flag command ready.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Could not find '/serene' group.", ephemeral=True)
