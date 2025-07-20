import discord
from discord.ext import commands
from discord import app_commands
import json
import aiomysql
import re

async def start(interaction: discord.Interaction, bot: commands.Bot):
    # Define dynamic choices function
    async def get_reasons(interaction: discord.Interaction, current: str):
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
                await cursor.execute("SELECT reason FROM rule_flagging")
                rows = await cursor.fetchall()
                return [
                    app_commands.Choice(name=row['reason'], value=row['reason'])
                    for row in rows if current.lower() in row['reason'].lower()
                ][:25]  # Discord max choices is 25
        except Exception as e:
            print(f"[Flag Reason Fetch Error]: {e}")
            return []

    class FlagCommand(app_commands.Group):
        @app_commands.command(name="flag", description="Flag one or more users for a rule violation")
        @app_commands.autocomplete(reason=get_reasons)
        async def flag(
            self,
            interaction: discord.Interaction,
            reason: str,
            users: str
        ):
            await interaction.response.defer(ephemeral=True)

            try:
                user_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", users)]

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
                    # Validate reason
                    await cursor.execute("SELECT action FROM rule_flagging WHERE reason = %s", (reason,))
                    rule = await cursor.fetchone()

                    if not rule:
                        await interaction.followup.send(f"Invalid reason: '{reason}'", ephemeral=True)
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
                                pass  # ignore bad json

                        flags = user_flags["warnings"]["flags"]
                        strikes = user_flags["warnings"]["strikes"]

                        already_flagged = any(f["reason"] == reason for f in flags)
                        new_strike_count = sum(1 for s in strikes if s["reason"] == reason) + 1

                        if action_type == "ban":
                            flags.append({"reason": reason, "seen": False})
                            for i in range(1, 4):
                                strikes.append({"reason": reason, "strike_number": i})
                        else:
                            if already_flagged:
                                strikes.append({"reason": reason, "strike_number": new_strike_count})
                            else:
                                flags.append({"reason": reason, "seen": False})

                        updated_json = json.dumps(user_flags)
                        await cursor.execute(
                            "UPDATE user_data SET flags = %s WHERE discord_id = %s",
                            (updated_json, uid)
                        )

                        user_obj = await bot.fetch_user(uid)
                        updates.append(f"{user_obj.mention} — {'Banned' if action_type == 'ban' else ('Strike Added' if already_flagged else 'Flagged')}")

                await interaction.followup.send(
                    f"✅ Processed {len(user_ids)} user(s) for reason: **{reason}**\n\n" + "\n".join(updates),
                    ephemeral=True
                )

            except Exception as e:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # Register command inside a temporary group
    serene_group = bot.tree.get_command("serene")
    if serene_group is None:
        await interaction.followup.send("Command group '/serene' not found.", ephemeral=True)
        return

    serene_group.add_command(FlagCommand(name="admin", description="Admin tools"))

    # Sync it dynamically for this interaction only
    await bot.tree.sync(guild=interaction.guild)
    await interaction.followup.send("✅ Flag command loaded. Use `/serene admin flag`.", ephemeral=True)
