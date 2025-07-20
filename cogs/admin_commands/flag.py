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
        # Enable confirm button if both reason and users are selected
        self.view.confirm_button.disabled = not (self.view.selected_reason and self.view.selected_users)
        await interaction.response.edit_message(view=self.view)


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
        # Enable confirm button if both reason and users are selected
        self.view.confirm_button.disabled = not (self.view.selected_reason and self.view.selected_users)
        await interaction.response.edit_message(view=self.view)


class FlagConfirmButton(Button):
    def __init__(self):
        super().__init__(
            label="Confirm Flag",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_flag",
            disabled=True
        )

    async def callback(self, interaction: discord.Interaction):
        view: FlagView = self.view

        if not view.selected_reason or not view.selected_users:
            # This should ideally not be reached if button is properly disabled
            await interaction.response.send_message(
                "⚠️ Please select both a reason and at least one user before confirming.",
                ephemeral=True
            )
            return

        bot = interaction.client  # Get your bot instance

        # DB logic: For each user, add flag and handle strikes/banning
        flagged_mentions = []
        for user in view.selected_users:
            try:
                # Example async DB call - replace with your actual method
                # await bot.db.add_flag(user.id, view.selected_reason) # Uncomment and replace
                flagged_mentions.append(user.mention)
            except Exception as e:
                logger.error(f"Failed to flag user {user} ({user.id}): {e}")

        if flagged_mentions:
            mentions_str = ", ".join(flagged_mentions)
            # Disable all components after confirmation
            for item in view.children:
                item.disabled = True
            await interaction.response.edit_message(
                content=f"🚩 Flagged {mentions_str} for **{view.selected_reason}**.",
                view=view,
                embed=None # Remove the embed as it's no longer needed
            )
            # Optionally, add logging or webhook notification here
        else:
            await interaction.response.send_message(
                "⚠️ Failed to flag any users due to an internal error.",
                ephemeral=True
            )


class FlagCancelButton(Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        # Disable all controls in the view to prevent further interaction
        for item in self.view.children:
            item.disabled = True

        # Edit the original ephemeral message to reflect disabled controls and update message
        await interaction.response.edit_message(
            content="🗑️ Flag operation cancelled.",
            view=self.view,
            embed=None # Remove the embed
        )


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

    # Remove interaction_check as its purpose is now handled in select callbacks
    # async def interaction_check(self, interaction: discord.Interaction) -> bool:
    #     self.confirm_button.disabled = not (self.selected_reason and self.selected_users)
    #     await interaction.response.edit_message(view=self)
    #     return True


async def start(serene_group, bot, interaction: discord.Interaction):
    reasons = getattr(bot, "flag_reasons", [])
    if not reasons:
        await interaction.response.send_message("❌ No flag reasons configured.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚩 Flag Users",
        description="Serene Bot will handle the hassle of administering disciplinary actions towards a user or group of users. It does this **__by checking each discord user's flag status for the given reason__** you specify below, and **__if a flag for it already exists, Serene Bot automatically administers the first strike__**. If a strike or strikes have already been administered, **__Serene Bot will automatically increase the number of strikes__** until the third. **__After the third strike, the user(s) will be banned from the server__**.",
        color=discord.Color.orange()
    )
    embed.set_footer(text="Admins only — all actions are logged.")

    view = FlagView(reasons)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
