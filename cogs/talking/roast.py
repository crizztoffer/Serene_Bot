
import os
import urllib.parse
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

# This 'start' function will be called by communication_main.py
# It will receive the discord.Interaction object and the bot instance.
async def start(interaction: discord.Interaction, bot: commands.Bot):
    """
    Registers the 'roast' slash command under the 'serene' group.
    This function is designed to be called dynamically by a larger bot structure.
    """
    # Get the existing 'serene' command group from the bot's command tree
    # This assumes the 'serene' group is already defined in the main bot file (e.g., communication_main.py)
    serene_group = bot.tree.get_command("serene")

    if serene_group is None:
        # If the 'serene' group doesn't exist, log an error or raise an exception
        # This indicates a setup issue in the main bot.
        print("Error: '/serene' command group not found. Cannot register 'roast' command.")
        await interaction.followup.send("Bot setup error: '/serene' command group not found.", ephemeral=True)
        return

    # Define the 'roast' command as a nested function
    # It will be added to the serene_group.
    @serene_group.command(name="roast", description="Get roasted by Serene!")
    async def roast_command(interaction: discord.Interaction):
        """
        Handles the /serene roast slash command.
        Sends a predefined "roast me" message to the backend with a 'roast' parameter.
        """
        await interaction.response.defer() # Acknowledge the interaction to prevent timeout

        php_backend_url = "https://serenekeks.com/serene_bot.php"
        player_name = interaction.user.display_name

        text_to_send = "roast me"  # Predefined text for this command
        param_name = "roast"  # Use 'roast' as the parameter name

        # Prepare parameters for the PHP backend
        params = {
            param_name: text_to_send,
            "player": player_name
        }
        # URL-encode parameters to safely pass them in the URL
        encoded_params = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
        full_url = f"{php_backend_url}?{encoded_params}"

        try:
            # Make an asynchronous HTTP GET request
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url) as response:
                    if response.status == 200:
                        php_response_text = await response.text()
                        await interaction.followup.send(php_response_text)
                    else:
                        await interaction.followup.send(
                            f"Serene backend returned an error: HTTP Status {response.status}"
                        )
        except aiohttp.ClientError as e:
            await interaction.followup.send(
                f"Could not connect to the Serene backend. Error: {e}"
            )
        except Exception as e:
            await interaction.followup.send(
                f"An unexpected error occurred: {e}"
            )

    # Add the defined command to the command tree
    # This will make the /serene roast command available
    bot.tree.add_command(roast_command)
    print("'/serene roast' command registered.")

