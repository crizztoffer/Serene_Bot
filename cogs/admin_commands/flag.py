import discord
from discord import app_commands
from discord.ui import View, Select, UserSelect, Button
import logging
import json
import aiomysql
from typing import List, Optional, Tuple, Dict
from datetime import timedelta
import re

logger = logging.getLogger(__name__)

# ---------- Small name-matching helpers (space/hyphen tolerant) ----------

def _normalize_role_variants(name: str) -> List[str]:
    if not name:
        return []
    base = name.strip()
    alts = {
        base,
        base.lower(),
        base.replace("-", " "),
        base.replace(" ", "-"),
        base.lower().replace("-", " "),
        base.lower().replace(" ", "-"),
        re.sub(r"\s{2,}", " ", base),
    }
    return list(alts)

def _find_role_fuzzy(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
    if not role_name or not guild:
        return None
    # exact first
    r = discord.utils.get(guild.roles, name=role_name)
    if r:
        return r
    # case-insensitive
    low = role_name.lower()
    for role in guild.roles:
        if role.name.lower() == low:
            return role
    # spacing/hyphen variants
    for variant in _normalize_role_variants(role_name):
        role = discord.utils.get(guild.roles, name=variant)
        if role:
            return role
    # fallback: compare names with spaces normalized to hyphens
    for role in guild.roles:
        if role.name.lower().replace(" ", "-") == low.replace(" ", "-"):
            return role
    return None

# ---------- DB fetch helpers ----------

async def fetch_flag_reasons(db_user: str, db_password: str, db_host: str, guild_id: int | str) -> List[str]:
    """
    Fetch the latest reasons right before building the view.

    Logic:
      1) Look up bot_use_custom.use_custom for this guild_id.
      2) If use_custom = 1 -> SELECT reason FROM rule_flagging WHERE guild_id = %s
      3) Else -> SELECT reason FROM rule_flagging WHERE guild_id = 'DEFAULT'
    """
    if not all([db_user, db_password, db_host]):
        logger.error("Missing DB credentials; cannot fetch flag reasons.")
        return []

    reasons: List[str] = []
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
            # 1) Check if this guild uses custom flags
            use_custom = 0
            try:
                await cursor.execute(
                    "SELECT use_custom FROM bot_use_custom WHERE guild_id = %s",
                    (str(guild_id),)
                )
                row = await cursor.fetchone()
                if row is not None:
                    use_custom = int(row[0]) if row[0] is not None else 0
            except Exception as e:
                logger.error(f"Failed to read bot_use_custom for guild {guild_id}: {e}", exc_info=True)
                use_custom = 0  # fallback to default

            # 2) Load reasons based on flag mode
            if use_custom == 1:
                await cursor.execute(
                    "SELECT reason FROM rule_flagging WHERE guild_id = %s ORDER BY rule_class ASC, id ASC",
                    (str(guild_id),)
                )
            else:
                await cursor.execute(
                    "SELECT reason FROM rule_flagging WHERE guild_id = 'DEFAULT' ORDER BY rule_class ASC, id ASC"
                )

            rows = await cursor.fetchall()
            seen = set()
            for r in rows or []:
                reason = r[0]
                if reason and reason not in seen:
                    seen.add(reason)
                    reasons.append(reason)

    except Exception as e:
        logger.error(f"Failed to fetch reasons: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
    return reasons

async def _fetch_actions_config(cursor, guild_id: int | str) -> Dict[str, str]:
    """
    Returns dict with keys: first_flag, second_flag, final_flag, instant_ban_behavior.
    Applies same defaults as your PHP.
    """
    defaults = {
        "first_flag": "show_rules_disable_chat",
        "second_flag": "timeout_1h",
        "final_flag": "ban",
        "instant_ban_behavior": "none",
    }
    try:
        await cursor.execute(
            "SELECT first_flag, second_flag, final_flag, instant_ban_behavior "
            "FROM bot_flag_actions WHERE guild_id = %s",
            (str(guild_id),)
        )
        row = await cursor.fetchone()
        if not row:
            return defaults
        return {
            "first_flag": row[0] or defaults["first_flag"],
            "second_flag": row[1] or defaults["second_flag"],
            "final_flag": row[2] or defaults["final_flag"],
            "instant_ban_behavior": row[3] or defaults["instant_ban_behavior"],
        }
    except Exception as e:
        logger.error(f"_fetch_actions_config failed for guild {guild_id}: {e}", exc_info=True)
    return defaults

async def _fetch_quarantine_options(cursor, guild_id: int | str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (quarantine_role_name, quarantine_channel_name) from bot_flag_action_options.
    """
    try:
        await cursor.execute(
            "SELECT quarantine_role_name, quarantine_channel_name FROM bot_flag_action_options WHERE guild_id = %s",
            (str(guild_id),)
        )
        row = await cursor.fetchone()
        if not row:
            return (None, None)
        # Row might be tuple; handle dict cursor too just in case
        role_name = row[0] if isinstance(row, tuple) else row.get("quarantine_role_name")
        ch_name = row[1] if isinstance(row, tuple) else row.get("quarantine_channel_name")
        return (role_name, ch_name)
    except Exception as e:
        logger.error(f"_fetch_quarantine_options error for guild {guild_id}: {e}", exc_info=True)
    return (None, None)

# ---------- Components ----------

class FlagReasonSelect(Select):
    def __init__(self, reasons: List[str], current_selection: Optional[str] = None):
        self.all_reasons = reasons
        options = []
        for reason in reasons:
            option = discord.SelectOption(label=reason, value=reason)
            if reason == current_selection:
                option.default = True
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
        self.view.reason_select = FlagReasonSelect(self.all_reasons, self.view.selected_reason)
        self.view.remove_item(self)
        self.view.add_item(self.view.reason_select)
        self.view.confirm_button.disabled = not (self.view.selected_reason and self.view.selected_users)
        await interaction.response.edit_message(view=self.view)

class FlagUserSelect(UserSelect):
    def __init__(self, current_selections: Optional[List[discord.User]] = None):
        super().__init__(
            placeholder="Select user(s) to flag",
            min_values=1,
            max_values=5,
            custom_id="flag_users"
        )
        self.current_selected_users = current_selections if current_selections is not None else []

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_users = self.values
        self.view.user_select = FlagUserSelect(self.view.selected_users)
        self.view.remove_item(self)
        self.view.add_item(self.view.user_select)
        self.view.confirm_button.disabled = not (self.view.selected_reason and self.view.selected_users)
        await interaction.response.edit_message(view=self.view)

# ---------- Punishment executor ----------

async def _apply_timeout(member: discord.Member, action: str):
    durations = {
        "timeout_5m":  timedelta(minutes=5),
        "timeout_10m": timedelta(minutes=10),
        "timeout_1h":  timedelta(hours=1),
        "timeout_1d":  timedelta(days=1),
        "timeout_1w":  timedelta(weeks=1),
    }
    delta = durations.get(action)
    if not delta:
        return "no-op"
    try:
        await member.timeout(delta, reason=f"Serene flag action: {action}")
        return f"timeout {delta}"
    except discord.Forbidden:
        logger.error(f"Cannot timeout {member} due to permissions.")
        return "forbidden"
    except Exception as e:
        logger.error(f"Timeout error for {member}: {e}", exc_info=True)
        return "error"

async def _apply_ban(member: discord.Member, pm_behavior: str):
    # Optional DM
    if pm_behavior == "pm":
        try:
            await member.send(
                "I'm sorry kek, but you're just not a good fit for our community. Hate to say it, but this is where our story ends. "
                "Best of luck in all your endeavors, and remember... you get back what you put out.\n\n-Serene"
            )
        except Exception:
            pass
    try:
        await member.ban(reason="Serene flag action: ban", delete_message_days=0)
        return "banned"
    except discord.Forbidden:
        logger.error(f"Cannot ban {member} due to permissions.")
        return "forbidden"
    except Exception as e:
        logger.error(f"Ban error for {member}: {e}", exc_info=True)
        return "error"

async def _apply_show_rules_disable_chat(
    cursor,
    bot: discord.Client,
    member: discord.Member,
    guild_id: int | str,
    quarantine_role_name: Optional[str],
    quarantine_channel_name: Optional[str]
):
    """
    Save current roles -> clear manageable roles -> assign quarantine role (by name)
    -> enforce quarantine visibility across the guild (hide everything except the quarantine channel).
    """
    guild = member.guild
    if not guild:
        return "no-guild"

    # 1) Save current roles into discord_users.role_data
    role_ids = [str(r.id) for r in member.roles if not r.is_default()]
    try:
        role_data_json = json.dumps({"roles": role_ids})
        await cursor.execute(
            "UPDATE discord_users SET role_data = %s WHERE guild_id = %s AND discord_id = %s",
            (role_data_json, str(guild_id), str(member.id))
        )
    except Exception as e:
        logger.error(f"Failed updating role_data for {member}: {e}", exc_info=True)

    # 2) Remove all manageable roles
    me = guild.me
    removable = []
    for r in member.roles:
        if r.is_default():
            continue
        if r.managed:
            continue
        if me and r >= me.top_role:
            continue
        removable.append(r)

    try:
        if removable:
            await member.remove_roles(*removable, reason="Serene: show_rules_disable_chat")
    except discord.Forbidden:
        logger.error(f"Missing perms to remove roles from {member}")
        return "forbidden"
    except Exception as e:
        logger.error(f"Error removing roles from {member}: {e}", exc_info=True)
        return "error"

    # 3) Assign quarantine role by name (fuzzy)
    qrole = _find_role_fuzzy(guild, quarantine_role_name or "")
    if not qrole:
        logger.warning(f"Quarantine role '{quarantine_role_name}' not found in guild {guild_id}")
        return "no-quarantine-role"

    try:
        await member.add_roles(qrole, reason="Serene: show_rules_disable_chat")
    except discord.Forbidden:
        logger.error(f"Missing perms to add quarantine role to {member}")
        return "forbidden"
    except Exception as e:
        logger.error(f"Error adding quarantine role to {member}: {e}", exc_info=True)
        return "error"

    # 4) Enforce server-wide visibility: only see quarantine channel
    try:
        # We rely on bot.ensure_quarantine_objects (added/exposed in bot.py)
        if hasattr(bot, "ensure_quarantine_objects") and callable(getattr(bot, "ensure_quarantine_objects")):
            await bot.ensure_quarantine_objects(str(guild.id), quarantine_role_name or "", quarantine_channel_name or "quarantine")
        else:
            logger.warning("bot.ensure_quarantine_objects not available; cannot enforce channel denies.")
    except Exception as e:
        logger.error(f"Failed to enforce quarantine visibility for guild {guild_id}: {e}", exc_info=True)

    return "quarantined"

# ---------- Confirm button ----------

class FlagConfirmButton(Button):
    def __init__(self):
        super().__init__(
            label="Confirm Flag",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_flag",
            disabled=True
        )

    async def callback(self, interaction: discord.Interaction):
        view: "FlagView" = self.view

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

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        selected_reason = view.selected_reason

        results_lines = []
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
                # Fetch action config + quarantine options once
                actions_cfg = await _fetch_actions_config(cursor, guild_id)
                quarantine_role_name, quarantine_channel_name = await _fetch_quarantine_options(cursor, guild_id)

                for user in view.selected_users:
                    # Convert to Member (we need roles/permissions)
                    member: Optional[discord.Member] = guild.get_member(user.id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user.id)
                        except Exception:
                            member = None

                    # Ensure user exists in DB
                    try:
                        await cursor.execute(
                            "SELECT json_data FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                            (str(guild_id), str(user.id))
                        )
                        row = await cursor.fetchone()
                        if not row:
                            # Let the bot bootstrap the user
                            if hasattr(bot, "add_user_to_db_if_not_exists"):
                                await bot.add_user_to_db_if_not_exists(guild_id, user.display_name, user.id)
                                await cursor.execute(
                                    "SELECT json_data FROM discord_users WHERE guild_id = %s AND discord_id = %s",
                                    (str(guild_id), str(user.id))
                                )
                                row = await cursor.fetchone()
                        if not row:
                            results_lines.append(f"• {user.mention}: not in DB; skipped.")
                            continue

                        json_data = {}
                        raw = row[0]
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode("utf-8", errors="ignore")
                        try:
                            json_data = json.loads(raw) if raw else {}
                        except Exception:
                            json_data = {}

                        warnings = json_data.setdefault("warnings", {})
                        flags = warnings.setdefault("flags", [])
                        strikes = warnings.setdefault("strikes", [])

                        # Compute current counts per reason
                        has_flag = any(f.get("reason") == selected_reason for f in flags)
                        strikes_for_reason = sum(1 for s in strikes if s.get("reason") == selected_reason)

                        # Update data: first time -> add flag; else add a new strike
                        if not has_flag:
                            flags.append({
                                "reason": selected_reason,
                                "seen": False,
                                "timestamp": discord.utils.utcnow().isoformat()
                            })
                            new_offense_count = 1
                        else:
                            new_strike_num = strikes_for_reason + 1
                            strikes.append({
                                "reason": selected_reason,
                                "strike_number": new_strike_num,
                                "timestamp": discord.utils.utcnow().isoformat()
                            })
                            new_offense_count = 1 + new_strike_num  # 2 or 3+

                        json_data["warnings"] = {"flags": flags, "strikes": strikes}
                        await cursor.execute(
                            "UPDATE discord_users SET json_data = %s WHERE guild_id = %s AND discord_id = %s",
                            (json.dumps(json_data), str(guild_id), str(user.id))
                        )

                        # Decide punishment tier (per-reason)
                        if new_offense_count == 1:
                            action_to_apply = actions_cfg["first_flag"]
                            tier = "first"
                        elif new_offense_count == 2:
                            action_to_apply = actions_cfg["second_flag"]
                            tier = "second"
                        else:
                            action_to_apply = actions_cfg["final_flag"]
                            tier = "final"

                        # Only apply punishment to non-admins
                        applied = "recorded"
                        if member and not member.guild_permissions.administrator:
                            if action_to_apply == "show_rules_disable_chat":
                                applied = await _apply_show_rules_disable_chat(
                                    cursor,
                                    bot,
                                    member,
                                    guild_id,
                                    quarantine_role_name,
                                    quarantine_channel_name
                                )
                            elif action_to_apply.startswith("timeout_"):
                                applied = await _apply_timeout(member, action_to_apply)
                            elif action_to_apply == "ban":
                                applied = await _apply_ban(member, actions_cfg.get("instant_ban_behavior", "none"))
                            else:
                                applied = "no-op"
                        else:
                            if member and member.guild_permissions.administrator:
                                applied = "skipped (admin)"

                        results_lines.append(f"• {user.mention}: {tier} flag for **{selected_reason}** → {action_to_apply} ({applied}).")

                    except Exception as e:
                        logger.error(f"Failed to process {user} ({user.id}) for flagging: {e}", exc_info=True)
                        results_lines.append(f"• {user.mention}: error while flagging.")

            # Respond with a compact summary
            text = "🚩 **Flag results**\n" + "\n".join(results_lines) if results_lines else "No users processed."
            if interaction.response.is_done():
                await interaction.edit_original_response(content=text, view=None, embed=None)
            else:
                await interaction.response.edit_message(content=text, view=None, embed=None)

        except Exception as e:
            logger.error(f"DB connection or general error during flagging: {e}", exc_info=True)
            if interaction.response.is_done():
                await interaction.edit_original_response(
                    content="An error occurred while attempting to flag users.",
                    view=None,
                    embed=None
                )
            else:
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
    def __init__(self, reasons: List[str]):
        super().__init__(timeout=300)
        self.selected_reason: Optional[str] = None
        self.selected_users: Optional[List[discord.User]] = None

        self.reason_select = FlagReasonSelect(reasons, self.selected_reason)
        self.user_select = FlagUserSelect(self.selected_users)

        self.confirm_button = FlagConfirmButton()
        self.cancel_button = FlagCancelButton()

        self.add_item(self.reason_select)
        self.add_item(self.user_select)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

# ---------- Entry point that builds the view with fresh reasons ----------

async def start(serene_group, bot, interaction: discord.Interaction):
    """
    Called when the admin opens the flag UI.
    We fetch the latest reasons at this moment so dropdowns are never stale.
    """
    db_user = getattr(bot, "db_user", None)
    db_password = getattr(bot, "db_password", None)
    db_host = getattr(bot, "db_host", None)

    reasons = await fetch_flag_reasons(db_user, db_password, db_host, interaction.guild_id)
    if not reasons:
        await interaction.response.send_message("❌ No flag reasons configured.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚩 Flag Users",
        description=(
            "Serene Bot will handle the hassle of administering disciplinary actions towards a user or group of users.\n\n"
            "• Flags are **per reason**. If you flag different reasons once each (A/B/C), that counts as a **first** step — not a final.\n"
            "• For the **same reason**, offenses escalate: 1st → first action, 2nd → second action, 3rd+ → final action."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text="Admins only — all actions are logged.")

    view = FlagView(reasons)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
