import discord
from discord import app_commands
from discord.ui import View, Select, UserSelect, Button
import logging
import json
import aiomysql

logger = logging.getLogger(__name__)

class FlagReasonSelect(Select):
    def __init__(self, reasons: list[str], current_selection: str = None):
        self.all_reasons = reasons # Store all reasons to re-create options if needed
        options = []
        for reason in reasons:
            option = discord.SelectOption(label=reason, value=reason)
            if reason == current_selection:
                option.default = True # Mark this option as selected
            options.append(option)

        super().__init__(
            placeholder="Select a reason to flag",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="flag_reason"
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_reason = self.values[0]
        # Re-create the select with the new default
        self.view.reason_select = FlagReasonSelect(self.all_reasons, self.view.selected_reason)
        # Remove and re-add the item to update its position in the view
        self.view.remove_item(self) # Remove the old instance
        self.view.add_item(self.view.reason_select) # Add the new instance at the same logical position

        # Enable confirm button if both reason and users are selected
        self.view.confirm_button.disabled = not (self.view.selected_reason and self.view.selected_users)
        await interaction.response.edit_message(view=self.view)


class FlagUserSelect(UserSelect):
    def __init__(self, current_selections: list[discord.User] = None):
        # UserSelect handles defaults differently, you can't set `default=True` on options like Select
        # The selected users are automatically managed by Discord if the view isn't recreated.
        # However, since we are re-editing the message, we need to consider how to visually represent this.
        # For UserSelect, the default behavior is often good enough if you're managing `selected_users`
        # on the view. The issue is more pronounced with standard Select options.
        super().__init__(
            placeholder="Select user(s) to flag",
            min_values=1,
            max_values=5,
            custom_id="flag_users"
        )
        # Store current selections if needed, though UserSelect often handles this visually on re-render
        # if the interaction is with itself. The problem arises when a *different* component causes the re-render.
        self.current_selected_users = current_selections if current_selections is not None else []


    async def callback(self, interaction: discord.Interaction):
        self.view.selected_users = self.values
        # For UserSelect, Discord typically handles the display of selected users internally
        # when the message is edited with the same view instance.
        # However, if you need to "force" the re-selection visual when *another* component
        # triggers the edit, you'd need to recreate the UserSelect instance similarly to FlagReasonSelect
        # For simplicity, let's just make sure the confirm button is updated.

        # Re-create the select with the new default users (this is more for visual consistency)
        self.view.user_select = FlagUserSelect(self.view.selected_users)
        self.view.remove_item(self)
        self.view.add_item(self.view.user_select)

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
            await interaction.response.send_message(
                "⚠️ Please select both a reason and at least one user before confirming.",
                ephemeral=True
            )
            return

        bot = interaction.client

        db_user = getattr(bot, "db_user", None)
        db_password = getattr(bot, "db_password", None)
        db_host = getattr(bot, "db_host", None)

        if not all([db_user, db_password, db_host]):
            await interaction.response.send_message(
                "⚠️ Database credentials are not configured.", ephemeral=True
            )
            logger.error("Missing DB credentials.")
            return

        flagged_mentions = []
        conn = None
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
                for user in view.selected_users:
                    try:
                        await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                        row = await cursor.fetchone()

                        if not row:
                            logger.info(f"{user.display_name} not in DB. Attempting to add.")
                            if hasattr(bot, 'add_user_to_db_if_not_exists'):
                                await bot.add_user_to_db_if_not_exists(interaction.guild_id, user.display_name, user.id)
                            else:
                                logger.warning("bot.add_user_to_db_if_not_exists method not found. User might not be added.")
                                continue

                            await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                            row = await cursor.fetchone()
                            if not row:
                                logger.error(f"Could not add user {user.display_name} to DB after initial attempt.")
                                continue

                        json_data = json.loads(row[0])
                        warnings = json_data.setdefault("warnings", {})
                        flags = warnings.setdefault("flags", [])
                        strikes = warnings.setdefault("strikes", [])

                        if any(f.get("reason") == view.selected_reason for f in flags):
                            strike_count = sum(1 for s in strikes if s.get("reason") == view.selected_reason)
                            strikes.append({
                                "reason": view.selected_reason,
                                "strike_number": strike_count + 1,
                                "timestamp": discord.utils.utcnow().isoformat()
                            })
                        else:
                            flags.append({
                                "reason": view.selected_reason,
                                "seen": False,
                                "timestamp": discord.utils.utcnow().isoformat()
                            })

                        json_data["warnings"] = {"flags": flags, "strikes": strikes}
                        await cursor.execute(
                            "UPDATE discord_users SET json_data = %s WHERE discord_id = %s",
                            (json.dumps(json_data), str(user.id))
                        )

                        flagged_mentions.append(user.mention)

                    except Exception as e:
                        logger.error(f"Failed to process user {user} ({user.id}) for flagging: {e}", exc_info=True)

            if flagged_mentions:
                mentions_str = ", ".join(flagged_mentions)
                await interaction.response.edit_message(
                    content=f"🚩 Flagged {mentions_str} for **{view.selected_reason}**.",
                    view=None,
                    embed=None
                )
            else:
                await interaction.response.edit_message(
                    content="⚠️ Failed to flag any users due to an internal error or processing issues.",
                    view=None,
                    embed=None
                )

        except Exception as e:
            logger.error(f"DB connection or general error during flagging: {e}", exc_info=True)
            await interaction.response.edit_message(
                content="An error occurred while attempting to flag users.",
                view=None,
                embed=None
            )

        finally:
            if conn:
                conn.close()

class FlagCancelButton(Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="🗑️ Flag operation cancelled.",
            view=None,
            embed=None
        )


class FlagView(View):
    def __init__(self, reasons: list[str]):
        super().__init__(timeout=300)
        self.selected_reason = None
        self.selected_users = None

        # Pass initial selected_reason and selected_users to the select constructors
        self.reason_select = FlagReasonSelect(reasons, self.selected_reason)
        self.user_select = FlagUserSelect(self.selected_users) # Pass initial empty list for users

        self.confirm_button = FlagConfirmButton()
        self.cancel_button = FlagCancelButton()

        self.add_item(self.reason_select)
        self.add_item(self.user_select)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)


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
