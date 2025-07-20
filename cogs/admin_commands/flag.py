# --- cogs/admin_commands/flag.py ---

import discord
from discord import app_commands
from discord.ui import View, Select, UserSelect, Button
import logging

logger = logging.getLogger(__name__)


class FlagReasonSelect(Select):
    def __init__(self, reasons: list[str]):
        options = [
            discord.SelectOption(label=reason, value=reason) for reason in reasons
        ]
        super().__init__(
            placeholder="Select a reason to flag",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="flag_reason"
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_reason = self.values[0]
        await interaction.response.send_message(
            f"✅ Selected reason: **{self.values[0]}**", ephemeral=True
        )


class FlagUserSelect(UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select user(s) to flag",
            min_values=1,
            max_values=5,
            custom_id="flag_users"
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_users = self.values
        selected_names = ", ".join(user.mention for user in self.values)
        await interaction.response.send_message(
            f"👤 Selected users: {selected_names}", ephemeral=True
        )


class FlagView(View):
    def __init__(self, reasons: list[str]):
        super().__init__(timeout=300)
        self.selected_reason = None
        self.selected_users = None

        self.add_item(FlagReasonSelect(reasons))
        self.add_item(FlagUserSelect())
        self.add_item(FlagConfirmButton())

    async def flag_users(self, interaction: discord.Interaction):
        if not self.selected_reason or not self.selected_users:
            await interaction.response.send_message(
                "⚠️ Please select both a reason and at least one user before confirming.",
                ephemeral=True
            )
            return

        flagged_mentions = ", ".join(user.mention for user in self.selected_users)
        await interaction.response.send_message(
            f"🚩 Flagged {flagged_mentions} for **{self.selected_reason}**.",
            ephemeral=True
        )
        # Here you could also log the flag somewhere (e.g., webhook, database, etc.)


class FlagConfirmButton(Button):
    def __init__(self):
        super().__init__(
            label="Confirm Flag",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.flag_users(interaction)


async def start(serene_group, bot, interaction: discord.Interaction):
    reasons = getattr(bot, "flag_reasons", [])
    if not reasons:
        await interaction.response.send_message("❌ No flag reasons configured.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚩 Flag Users",
        description=(
            "Serene Bot will handle the hassle of administrating disciplinary actions towards a user or group of users.\n"
            "It does this by checking each discord user's flag status for the given reason you specify below, and if \n"
            "flag for it already exists, Serene Bot automatically administers the first strike. If a strike or strikes \n"
            "have already been administered, Serene Bot will automatically increase the strike until the third, in \n"
            "case, the user(s) will be banned from the server."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text="Admins only — all actions are logged.")

    view = FlagView(reasons)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
