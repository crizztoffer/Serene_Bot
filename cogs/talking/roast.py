import os
import urllib.parse
import aiohttp
import discord
from discord import app_commands

@app_commands.command(name="roast", description="Get roasted by Serene!")
async def command(interaction: discord.Interaction):
    """
    Handles the /serene roast slash command.
    Sends a predefined "roast me" message to the backend with a 'roast' parameter.
    """
    await interaction.response.defer()  # Acknowledge the interaction

    php_backend_url = "https://serenekeks.com/serene_bot.php"
    player_name = interaction.user.display_name

    text_to_send = "roast me"  # Predefined text for this command
    param_name = "roast"       # Backend parameter name

    # Prepare URL-encoded parameters
    params = {
        param_name: text_to_send,
        "player": player_name
    }
    encoded_params = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    full_url = f"{php_backend_url}?{encoded_params}"

    try:
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
