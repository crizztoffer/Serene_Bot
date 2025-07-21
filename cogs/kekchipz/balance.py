import discord
import asyncio
import random
import io
import aiohttp
from PIL import Image, ImageDraw, ImageFont # Pillow library for image manipulation

# --- Database Operations (Copied from the.py for self-containment) ---
# In a real application, these would ideally be imported from a central database module.
async def update_user_kekchipz(guild_id: int, discord_id: int, amount: int):
    """
    Placeholder function to simulate updating a user's kekchipz balance in a database.
    In a real scenario, this would interact with a database.
    """
    print(f"Simulating update: User {discord_id} in guild {guild_id} kekchipz changed by {amount}.")
    # Example of how you might integrate a real database call:
    # try:
    #     conn = await aiomysql.connect(...)\
    #     async with conn.cursor() as cursor:
    #         await cursor.execute("UPDATE discord_users SET kekchipz = kekchipz + %s WHERE guild_id = %s AND discord_id = %s", (amount, str(guild_id), str(discord_id)))
    # except Exception as e:
    #     print(f"Database update failed: {e}")

async def get_user_kekchipz(guild_id: int, discord_id: int) -> int:
    """
    Placeholder function to simulate fetching a user's kekchipz balance from a database.
    Returns 0 if the user is not found or an error occurs.
    """
    print(f"Simulating fetch: Getting kekchipz for user {discord_id} in guild {guild_id}.")
    # In a real scenario, this would query a database.
    # For now, let's return a dummy value or a default.
    return 1000 # Example: User starts with 1000 kekchipz for testing


async def create_kekchipz_balance_image(guild_id: int, discord_id: int, player_display_name: str) -> io.BytesIO:
    """
    Creates an image displaying the player's kekchipz balance on a base image.

    Args:
        guild_id (int): The ID of the Discord guild.
        discord_id (int): The Discord ID of the player.
        player_display_name (str): The display name of the player.

    Returns:
        io.BytesIO: A BytesIO object containing the generated PNG image.
    """
    base_image_url = "https://serenekeks.com/kcpz.png"
    font_url = "http://serenekeks.com/OpenSans-CondBold.ttf"

    try:
        # Fetch the player's kekchipz balance
        balance = await get_user_kekchipz(guild_id, discord_id)
        balance_text = f"{player_display_name}'s Kekchipz: ${balance}"

        # Fetch the base image
        async with aiohttp.ClientSession() as session:
            async with session.get(base_image_url) as response:
                response.raise_for_status()
                base_image = Image.open(io.BytesIO(await response.read()))
                if base_image.mode != 'RGBA':
                    base_image = base_image.convert('RGBA')

        # Load font
        font = ImageFont.load_default() # Default font in case of failure
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(font_url) as response:
                    response.raise_for_status()
                    font_bytes = await response.read()
                    font_io = io.BytesIO(font_bytes)
                    font = ImageFont.truetype(font_io, 40) # Adjust font size as needed
        except aiohttp.ClientError as e:
            print(f"WARNING: Failed to fetch font from {font_url}: {e}. Using default Pillow font.")
        except Exception as e:
            print(f"WARNING: Error loading font from bytes: {e}. Using default Pillow font.")

        draw = ImageDraw.Draw(base_image)

        # Define text color (e.g., white)
        text_color = (255, 255, 255, 255) # RGBA for white

        # Calculate text size and position to center it
        # Use textbbox for accurate measurement
        bbox = draw.textbbox((0,0), balance_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Center the text
        x = (base_image.width - text_width) // 2
        y = (base_image.height - text_height) // 2

        # Draw the text on the image
        draw.text((x, y), balance_text, font=font, fill=text_color)

        # Save the modified image to a BytesIO object
        img_byte_arr = io.BytesIO()
        base_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0) # Rewind to the beginning of the stream

        return img_byte_arr

    except aiohttp.ClientError as e:
        print(f"Error fetching base image from {base_image_url}: {e}")
        # Return an empty BytesIO or a placeholder image if fetching fails
        return io.BytesIO(Image.new('RGBA', (400, 200), (255, 0, 0, 128)).save(io.BytesIO(), format='PNG'))
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return io.BytesIO(Image.new('RGBA', (400, 200), (0, 255, 0, 128)).save(io.BytesIO(), format='PNG'))


async def start(interaction: discord.Interaction, bot: commands.Bot):
    """
    Serves as the entry point for the kekchipz balance display.
    This function is called by game_main.py when the 'kekchipz' command is invoked.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction immediately

    try:
        image_bytes = await create_kekchipz_balance_image(
            interaction.guild.id,
            interaction.user.id,
            interaction.user.display_name
        )
        
        # Create a Discord File object from the BytesIO stream
        discord_file = discord.File(image_bytes, filename="kekchipz_balance.png")

        await interaction.followup.send(
            content=f"Here is {interaction.user.display_name}'s Kekchipz balance:",
            file=discord_file,
            ephemeral=False # Make it visible to everyone in the channel
        )
    except Exception as e:
        print(f"Error sending kekchipz balance message: {e}")
        await interaction.followup.send("An error occurred while trying to display your kekchipz balance.", ephemeral=True)


