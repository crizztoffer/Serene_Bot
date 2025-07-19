import os
import urllib.parse
import aiohttp
import discord
from discord import app_commands

@app_commands.command(name="talk", description="Talk with Serene about anything.")
@app_commands.describe(text_input="Your message or question for Serene.")
async def command(interaction: discord.Interaction, text_input: str):
    """
    Handles the /serene talk slash command.
    Sends user input to the serene_bot.php backend and displays the response.
    """
    await interaction.response.defer()

    php_backend_url = "https://serenekeks.com/serene_bot.php"
    player_name = interaction.user.display_name

    # Determine the parameter name based on the input text
    lower_text_input = text_input.lower()
    if lower_text_input.startswith(("hello", "hi", "hail")):
        param_name = "hail"
    elif lower_text_input.startswith(("start", "begin")):
        param_name = "start"
    else:
        param_name = "question"

    # Prepare URL-encoded parameters
    params = {
        param_name: text_input,
        "player": player_name
    }
    encoded_params = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    full_url = f"{php_backend_url}?{encoded_params}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(full_url) as response:
                if response.status == 200:
                    php_response_text = await response.text()
                    display_message = (
                        f"**{player_name} says:** {text_input}\n"
                        f"**Serene says:** {php_response_text}"
                    )
                    await interaction.followup.send(display_message)
                else:
                    await interaction.followup.send(
                        f"**{player_name} says:** {text_input}\n"
                        f"Serene backend returned an error: HTTP Status {response.status}"
                    )
    except aiohttp.ClientError as e:
        await interaction.followup.send(
            f"**{player_name} says:** {text_input}\n"
            f"Could not connect to the Serene backend. Error: {e}"
        )
    except Exception as e:
        await interaction.followup.send(
            f"**{player_name} says:** {text_input}\n"
            f"An unexpected error occurred: {e}"
        )
