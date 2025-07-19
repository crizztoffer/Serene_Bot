import os
import importlib
from discord.ext import commands
from discord import app_commands, Interaction

# --- Admin Check ---

async def is_admin_or_mod(interaction: Interaction) -> bool:
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.kick_members
    )

# --- Admin Command Group ---

class AdminGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="admin", description="Admin-only commands")

admin_group = AdminGroup()

# --- Dynamic Loader ---

def load_admin_commands():
    command_folder = "admin_commands"
    for filename in os.listdir(command_folder):
        if filename.endswith(".py") and not filename.startswith("_"):
            module_name = f"{command_folder}.{filename[:-3]}"
            try:
                module = importlib.import_module(module_name)
                if hasattr(module, "command"):
                    admin_group.add_command(module.command)
                    print(f"[admin_main] Loaded admin command: {filename}")
                else:
                    print(f"[admin_main] Skipped {filename}: no 'command' object.")
            except Exception as e:
                print(f"[admin_main] Failed to load {filename}: {e}")

# --- Setup ---

async def setup(bot: commands.Bot):
    load_admin_commands()
    bot.tree.add_command(admin_group)

    @bot.tree.error
    async def on_app_command_error(interaction: Interaction, error):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message("You don’t have permission to use this command.", ephemeral=True)
        else:
            await interaction.response.send_message("An error occurred while processing your command.", ephemeral=True)

