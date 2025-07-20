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
        # Do NOT send a message here as per your request


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
        # Do NOT send a message here as per your request


class FlagConfirmButton(Button):
    def __init__(self):
        super().__init__(
            label="Confirm Flag",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_flag",
            disabled=True  # Disabled initially
        )

    async def callback(self, interaction: discord.Interaction):
        view: FlagView = self.view

        if not view.selected_reason or not view.selected_users:
            await interaction.response.send_message(
                "⚠️ Please select both a reason and at least one user before confirming.",
                ephemeral=True
            )
            return

        # Here you can add your database flagging logic or call a method
        flagged_mentions = ", ".join(user.mention for user in view.selected_users)
        await interaction.response.send_message(
            f"🚩 Flagged {flagged_mentions} for **{view.selected_reason}**.",
            ephemeral=True
        )
        # Optionally log the action here


class FlagCancelButton(Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        # Defer to acknowledge interaction and prevent "This interaction failed"
        await interaction.response.defer()

        # Delete the ephemeral message containing the embed & buttons
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Enable confirm button only if both reason and users are selected
        self.confirm_button.disabled = not (self.selected_reason and self.selected_users)
        await interaction.response.edit_message(view=self)
        return True


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
