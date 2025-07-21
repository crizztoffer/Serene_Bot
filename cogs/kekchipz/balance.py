import discord
import asyncio
import random
import io
import aiohttp
from PIL import Image, ImageDraw, ImageFont  # Pillow library for image manipulation
from discord.ext import commands  # <-- ADD THIS LINE
import aiomysql # Added for database interaction

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

async def get_user_kekchipz(guild_id: int, discord_id: int, db_config: dict) -> int:
    """
    Fetches a user's kekchipz balance from the database.
    Returns 0 if the user is not found or an error occurs.
    """
    print(f"Fetching kekchipz for user {discord_id} in guild {guild_id} from DB.")
    conn = None
    try:
        conn = await aiomysql.connect(
            host=db_config['host'],
            user=db_config['user'],
            password=db_config['password'],
            db="serene_users", # Assuming this is the database name
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT kekchipz FROM discord_users WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            result = await cursor.fetchone()
            if result:
                return result[0]
            else:
                print(f"User {discord_id} not found in DB for guild {guild_id}. Returning 0.")
                return 0 # Or handle initial user setup if not found
    except Exception as e:
        print(f"Database error in get_user_kekchipz: {e}")
        return 0 # Return 0 on error
    finally:
        if conn:
            await conn.ensure_closed()


async def create_kekchipz_balance_image(guild_id: int, discord_id: int, player_display_name: str, db_config: dict) -> io.BytesIO:
    """
    Creates an image displaying the player's kekchipz balance on a base image.

    Args:
        guild_id (int): The ID of the Discord guild.
        discord_id (int): The Discord ID of the player.
        player_display_name (str): The display name of the player.
        db_config (dict): Dictionary containing database connection details.

    Returns:
        io.BytesIO: A BytesIO object containing the generated PNG image.
    """
    base_image_url = "https://serenekeks.com/kcpz.png"
    font_url = "http://serenekeks.com/OpenSans-CondBold.ttf"

    try:
        # Fetch the player's kekchipz balance from the actual DB
        balance = await get_user_kekchipz(guild_id, discord_id, db_config)
        # Only show the kekchipz amount
        balance_text = f"${balance}"

        # Fetch the base image
        async with aiohttp.ClientSession() as session:
            async with session.get(base_image_url) as response:
                response.raise_for_status()
                base_image = Image.open(io.BytesIO(await response.read()))
                if base_image.mode != 'RGBA':
                    base_image = base_image.convert('RGBA')

        # Resize the entire image to be 1/4 smaller (0.33 of original size)
        original_width, original_height = base_image.size
        new_width = int(original_width * 0.33)
        new_height = int(original_height * 0.33)
        base_image = base_image.resize((new_width, new_height), Image.LANCZOS) # Use LANCZOS for high-quality downscaling


        # Load font
        font = ImageFont.load_default() # Default font in case of failure
        font_size = 36 # Retained font size
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(font_url) as response:
                    response.raise_for_status()
                    font_bytes = await response.read()
                    font_io = io.BytesIO(font_bytes)
                    font = ImageFont.truetype(font_io, font_size)
        except aiohttp.ClientError as e:
            print(f"WARNING: Failed to fetch font from {font_url}: {e}. Using default Pillow font.")
        except Exception as e:
            print(f"WARNING: Error loading font from bytes: {e}. Using default Pillow font.")

        draw = ImageDraw.Draw(base_image)

        # Define text color: #0066ff converted to RGBA (0, 128, 255, 255)
        text_color = (0, 128, 255, 255) # Retained text color

        # Calculate text size and position to center it
        bbox = draw.textbbox((0,0), balance_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Center the text, with Y position adjusted to 1/6th of image height
        x = (base_image.width - text_width) // 2
        y = (base_image.height - text_height) // 6 # Retained Y position adjustment

        # Draw the text on the image
        draw.text((x, y), balance_text, font=font, fill=text_color)

        # Save the modified image to a BytesIO object
        img_byte_arr = io.BytesIO()
        base_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0) # Rewind to the beginning of the stream

        return img_byte_arr

    except aiohttp.ClientError as e:
        print(f"Error fetching base image from {base_image_url}: {e}")
        return io.BytesIO(Image.new('RGBA', (400, 200), (255, 0, 0, 128)).save(io.BytesIO(), format='PNG'))
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return io.BytesIO(Image.new('RGBA', (400, 200), (0, 255, 0, 128)).save(io.BytesIO(), format='PNG'))


async def start(interaction: discord.Interaction, bot: commands.Bot):
    """
    Serves as the entry point for the kekchipz balance display.
    This function is called by game_main.py when the 'kekchipz' command is invoked.
    """
    # Defer the response without ephemeral, so it's visible while the image is generated
    await interaction.response.defer(ephemeral=False) 

    # Prepare database configuration to pass to the image creation function
    db_config = {
        'host': bot.db_host,
        'user': bot.db_user,
        'password': bot.db_password
    }

    try:
        image_bytes = await create_kekchipz_balance_image(
            interaction.guild.id,
            interaction.user.id,
            interaction.user.display_name, # player_display_name is still passed but not used in balance_text for image
            db_config # Pass the db_config here
        )
        
        # Create a Discord File object from the BytesIO stream
        discord_file = discord.File(image_bytes, filename="kekchipz_balance.png")

        # Send the message without ephemeral and without the "Here is..." text
        await interaction.followup.send(
            file=discord_file,
            ephemeral=False # Ensure it's visible to everyone in the channel
        )
    except Exception as e:
        print(f"Error sending kekchipz balance message: {e}")
        # Keep this ephemeral for error messages, as they are likely only relevant to the user who triggered the command
        await interaction.followup.send("An error occurred while trying to display your kekchipz balance.", ephemeral=True)
