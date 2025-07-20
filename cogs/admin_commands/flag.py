# --- cogs/admin_commands/flag.py ---

import discord
from discord.ui import View, Select, UserSelect
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
        selected_names = ", ".join(user.name for user in self.values)
        await interaction.response.send_message(
            f"👤 Selected users: **{selected_names}**", ephemeral=True
        )


class FlagView(View):
    def __init__(self, reasons: list[str]):
        super().__init__(timeout=300)
        self.selected_reason = None
        self.selected_users = None

        self.add_item(FlagReasonSelect(reasons))
        self.add_item(FlagUserSelect())


async def start(serene_group, bot):
    # This is called directly by /serene admin flag
    async def run_flag_ui(interaction: discord.Interaction):
        reasons = getattr(bot, "flag_reasons", [])
        if not reasons:
            await interaction.response.send_message("❌ No flag reasons configured.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🚩 Flag Users",
            description="Select a **reason** and one or more **users** to flag.\n\n📝 A comment field will be added later.",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Admins only — all actions are logged.")

        view = FlagView(reasons)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # Call the UI directly from the interaction
    ctx = await bot.get_context_from_interaction()
    await run_flag_ui(ctx.interaction)
