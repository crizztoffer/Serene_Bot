import discord
from discord import app_commands
from discord.ui import View, Select, UserSelect, Button
import logging
import json
import aiomysql

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
                            # Assumes bot has this method, else you must implement it
                            # You need to ensure bot.add_user_to_db_if_not_exists is defined and works
                            if hasattr(bot, 'add_user_to_db_if_not_exists'):
                                await bot.add_user_to_db_if_not_exists(interaction.guild_id, user.display_name, user.id)
                            else:
                                logger.warning("bot.add_user_to_db_if_not_exists method not found. User might not be added.")
                                continue

                            await cursor.execute("SELECT json_data FROM discord_users WHERE discord_id = %s", (str(user.id),))
                            row = await cursor.fetchone()
                            if not row:
                                logger.error(f"Could not add user {user.display_name} to DB after initial attempt.")
                                continue  # Skip this user

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

            # Remove all components by setting view=None
            if flagged_mentions:
                mentions_str = ", ".join(flagged_mentions)
                await interaction.response.edit_message(
                    content=f"🚩 Flagged {mentions_str} for **{view.selected_reason}**.",
                    view=None, # Set view to None to remove all components
                    embed=None
                )
            else:
                await interaction.response.edit_message( # Use edit_message here for consistency
                    content="⚠️ Failed to flag any users due to an internal error or processing issues.",
                    view=None, # Remove components even on partial failure for a clean state
                    embed=None
                )

        except Exception as e:
            logger.error(f"DB connection or general error during flagging: {e}", exc_info=True)
            await interaction.response.edit_message( # Use edit_message here for consistency
                content="An error occurred while attempting to flag users.",
                view=None, # Remove components
                embed=None
            )

        finally:
            if conn:
                conn.close() # Use conn.close() for aiomysql connections

class FlagCancelButton(Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_flag"
        )

    async def callback(self, interaction: discord.Interaction):
        # Edit the original ephemeral message to remove all components and update message
        await interaction.response.edit_message(
            content="🗑️ Flag operation cancelled.",
            view=None, # Set view to None to remove all components
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
        self.cancel_button = FlagCancel_Button() # Typo fixed here

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
