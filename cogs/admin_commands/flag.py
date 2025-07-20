# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
from discord.ui import View, Select, UserSelect, Button
import logging
import json
import aiomysql

logger = logging.getLogger(__name__)


class FlagReasonSelect(Select):
    def __init__(self, reasons: list[str]):
        options = [discord.SelectOption(label=reason, value=reason) for reason in reasons]
        super().__init__(
            placeholder="Select a reason to flag",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="flag_reason"
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_reason = self.values[0]
        await self.view.update_buttons(interaction)


class FlagUserSelect(UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select user(s) to flag (bots will be ignored)",
            min_values=1,
            max_values=5,
            custom_id="flag_users"
        )

    async def callback(self, interaction: discord.Interaction):
        filtered_users = [user for user in self.values if not user.bot]

        if not filtered_users:
            await interaction.response.send_message(
                "⚠️ You cannot flag bots. Please select human users only.",
                ephemeral=True
            )
            return

        self.view.selected_users = filtered_users
        await self.view.update_buttons(interaction)


class FlagConfirmButton(Button):
    def __init__(self):
        super().__init__(
            label="Confirm Flag",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_flag",
            disabled=True
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.flag_users(interaction)


class FlagCancelButton(Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.message.delete()


class FlagView(View):
    def __init__(self, reasons: list[str]):
        super().__init__(timeout=300)
        self.selected_reason = None
        self.selected_users = None

        self.reason_select = FlagReasonSelect(reasons)
        self.user_select = FlagUserSelect()
        self.confirm_button = FlagConfirmButton()
        self.cancel_button = FlagCancelButton()

        self.add_item(self.reason_select)
        self.add_item(self.user_select)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def update_buttons(self, interaction: discord.Interaction):
        self.confirm_button.disabled = not (self.selected_reason and self.selected_users)
        await interaction.response.edit_message(view=self)

    async def flag_users(self, interaction: discord.Interaction):
        if not self.selected_reason or not self.selected_users:
            await interaction.response.send_message(
                "⚠️ Please select both a reason and at least one user before confirming.",
                ephemeral=True
            )
            return

        reason = self.selected_reason
        results = []

        db_user = interaction.client.db_user
        db_password = interaction.client.db_password
        db_host = interaction.client.db_host

        if not all([db_user, db_password, db_host]):
            await interaction.response.send_message("❌ Database credentials are not configured.", ephemeral=True)
            logger.error("Missing DB credentials.")
            return

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
                for user in self.selected_users:
                    await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                    row = await cursor.fetchone()

                    if not row:
                        logger.info(f"{user.display_name} not in DB. Attempting to add.")
                        await interaction.client.add_user_to_db_if_not_exists(interaction.guild_id, user.display_name, user.id)
                        await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                        row = await cursor.fetchone()
                        if not row:
                            results.append(f"❌ Could not add {user.display_name} to DB.")
                            continue

                    json_data = json.loads(row[0])
                    warnings = json_data.get("warnings", {})
                    flags = warnings.setdefault("flags", [])
                    strikes = warnings.setdefault("strikes", [])

                    if any(f.get("reason") == reason for f in flags):
                        strike_count = sum(1 for s in strikes if s.get("reason") == reason)
                        strikes.append({
                            "reason": reason,
                            "strike_number": strike_count + 1,
                            "timestamp": discord.utils.utcnow().isoformat()
                        })
                    else:
                        flags.append({
                            "reason": reason,
                            "seen": False,
                            "timestamp": discord.utils.utcnow().isoformat()
                        })

                    json_data["warnings"] = {"flags": flags, "strikes": strikes}
                    await cursor.execute(
                        "UPDATE discord_users SET json_data = %s WHERE discord_id = %s",
                        (json.dumps(json_data), str(user.id))
                    )

                    results.append(f"🚩 Flagged {user.mention} for **{reason}**")

            await conn.ensure_closed()

        except Exception as e:
            logger.error(f"Error flagging users: {e}")
            await interaction.response.send_message("⚠️ An error occurred while flagging users.", ephemeral=True)
            return

        result_msg = "\n".join(results)
        await interaction.response.send_message(result_msg, ephemeral=True)


async def start(serene_group, bot, interaction: discord.Interaction):
    reasons = getattr(bot, "flag_reasons", [])
    if not reasons:
        await interaction.response.send_message("❌ No flag reasons configured.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚩 Flag Users",
        description="Serene Bot will handle the hassle of administering disciplinary actions towards a user or group of users. It does this **by checking each discord user's flag status for the given reason** you specify below, and **if a flag for it already exists, Serene Bot automatically administers the first strike**. If a strike or strikes have already been administered, **Serene Bot will automatically increase the number of strikes** until the third. **After the third strike, the user(s) will be banned from the server**.",
        color=discord.Color.orange()
    )
    embed.set_footer(text="Admins only — all actions are logged.")

    view = FlagView(reasons)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
