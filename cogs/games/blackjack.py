import discord
from discord.ext import commands
import asyncio
import random
import io
import os # For environment variables like API keys
import urllib.parse # For URL encoding
import aiohttp # For asynchronous HTTP requests
from PIL import Image, ImageDraw, ImageFont # Pillow library for image manipulation
import aiomysql # Import aiomysql for database interaction
import logging

# Set up logging for this module
logger = logging.getLogger(__name__)

# --- Game State Storage ---
# This dictionary will store active Blackjack games by channel ID.
# This ensures only one game can run per channel at a time.
active_blackjack_games = {}

# --- Database Operations ---
async def update_user_kekchipz(guild_id: int, discord_id: int, amount: int, bot_instance: commands.Bot):
    """
    Updates a user's kekchipz balance in the database.
    Ensures the balance does not go below zero.
    """
    if not all([bot_instance.db_user, bot_instance.db_password, bot_instance.db_host]):
        logger.error("Missing DB credentials in bot_instance for update_user_kekchipz.")
        return

    conn = None
    try:
        conn = await aiomysql.connect(
            host=bot_instance.db_host,
            user=bot_instance.db_user,
            password=bot_instance.db_password,
            db="serene_users",
            charset='utf8mb4',
            autocommit=True
        )
        async with conn.cursor() as cursor:
            # First, get the current kekchipz balance
            await cursor.execute(
                "SELECT kekchipz FROM discord_users WHERE channel_id = %s AND discord_id = %s",
                (str(guild_id), str(discord_id))
            )
            result = await cursor.fetchone()
            
            current_kekchipz = result[0] if result else 0
            new_kekchipz = current_kekchipz + amount

            # Ensure kekchipz does not go below zero
            if new_kekchipz < 0:
                new_kekchipz = 0
                logger.info(f"User {discord_id} balance would go negative, setting to 0.")

            await cursor.execute(
                "UPDATE discord_users SET kekchipz = %s WHERE channel_id = %s AND discord_id = %s",
                (new_kekchipz, str(guild_id), str(discord_id))
            )
            logger.info(f"Updated user {discord_id} in guild {guild_id} kekchipz from {current_kekchipz} to {new_kekchipz} (change: {amount}).")
    except Exception as e:
        logger.error(f"DB error in update_user_kekchipz for user {discord_id}: {e}")
    finally:
        if conn:
            await conn.ensure_closed()

async def get_user_kekchipz(guild_id: int, discord_id: int, bot_instance: commands.Bot) -> int:
    """
    Fetches a user's kekchipz balance from the database.
    Returns 0 if the user is not found or an error occurs.
    """
    if not all([bot_instance.db_user, bot_instance.db_password, bot_instance.db_host]):
        logger.error("Missing DB credentials in bot_instance for get_user_kekchipz.")
        return 0

    conn = None
    try:
        conn = await aiomysql.connect(
            host=bot_instance.db_host,
            user=bot_instance.db_user,
            password=bot_instance.db_password,
            db="serene_users",
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
                # If user not found, add them with 0 kekchipz and then return 0
                await bot_instance.add_user_to_db_if_not_exists(guild_id, "UnknownUser", discord_id)
                return 0
    except Exception as e:
        logger.error(f"DB error in get_user_kekchipz for user {discord_id}: {e}")
        return 0
    finally:
        if conn:
            await conn.ensure_closed()


# --- Image Generation Function ---
async def create_card_combo_image(combo_str: str, scale_factor: float = 1.0, overlap_percent: float = 0.2) -> Image.Image:
    """
    Creates a combined image of playing cards from a comma-separated string of card codes.
    Fetches PNG images from deckofcardsapi.com and combines them using Pillow.

    Args:
        combo_str (str): A comma-separated string of card codes (e.g., "AS,KD,TH").
                         "XX" can be used for a hidden card (back of card).
        scale_factor (float): Factor to scale the card images (e.g., 1.0 for original size).
        overlap_percent (float): The percentage of card width that cards should overlap.

    Returns:
        PIL.Image.Image: A Pillow Image object containing the combined cards.

    Raises:
        ValueError: If no valid card codes are provided and it's not a special "XX" case.
    """
    cards = [card.strip().upper() for card in combo_str.split(',') if card.strip()]

    # Define a default size for cards in case the first fetch fails
    default_card_width, default_card_height = 73, 98 # Standard playing card dimensions in pixels (approx)

    if not cards:
        # If no valid card codes are provided (e.g., empty string), return a transparent placeholder.
        # The "XX" case is now handled within the loop if it's explicitly in the combo_str.
        return Image.new('RGBA', (default_card_width, default_card_height), (0, 0, 0, 0))


    card_images = []
    first_card_width, first_card_height = None, None

    for i, card in enumerate(cards):
        if card == "XX":
            png_url = "https://deckofcardsapi.com/static/img/back.png"
        else:
            png_url = f"https://deckofcardsapi.com/static/img/{card}.png"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(png_url) as response:
                    response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)

                    # Open the image directly using Pillow
                    pil_image = Image.open(io.BytesIO(await response.read()))

                    # Set background to transparent if it's not already
                    if pil_image.mode != 'RGBA':
                        pil_image = pil_image.convert('RGBA')

                    # Get initial dimensions from the first successfully loaded card
                    if first_card_width is None:
                        first_card_width, first_card_height = pil_image.size
                        # If this is the first card, set defaults if not already
                        if first_card_width is None: # This inner check is redundant if pil_image.size is always valid here.
                            first_card_width = default_card_width
                            first_card_height = default_card_height

                    # Scale the image based on the first card's dimensions
                    scaled_width = int(first_card_width * scale_factor)
                    scaled_height = int(first_card_height * scale_factor)

                    # Resize the image if scaling is applied
                    if scaled_width != pil_image.width or scaled_height != pil_image.height:
                        pil_image = pil_image.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

                    card_images.append(pil_image)

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch PNG for card '{card}' from {png_url}: {e}")
            # If the first card fails, ensure default dimensions are set
            if first_card_width is None:
                first_card_width = default_card_width
                first_card_height = default_card_height
            # Append a placeholder for failed cards to avoid breaking the layout
            card_images.append(Image.new('RGBA', (int(first_card_width * scale_factor), int(first_card_height * scale_factor)), (255, 0, 0, 128))) # Red transparent placeholder
        except Exception as e:
            logger.error(f"Error processing PNG for card '{card}' from {png_url}: {e}")
            # If the first card fails, ensure default dimensions are set
            if first_card_width is None:
                first_card_width = default_card_width
                first_card_height = default_card_height
            card_images.append(Image.new('RGBA', (int(first_card_width * scale_factor), int(first_card_height * scale_factor)), (0, 255, 0, 128))) # Green transparent placeholder

    # If no images were successfully loaded at all, return a generic transparent placeholder
    if not card_images:
        return Image.new('RGBA', (default_card_width, default_card_height), (0, 0, 0, 0))

    # Calculate dimensions for the combined image
    num_cards = len(card_images)
    
    # Calculate overlap based on scaled card width
    # Use the width of the first successfully loaded card, or default if none
    base_card_width = card_images[0].width if card_images else default_card_width
    overlap_px = int(base_card_width * overlap_percent)
    
    # Ensure overlap is not too large
    if overlap_px >= base_card_width:
        overlap_px = int(base_card_width * 0.1) # Default to 10% if overlap is too aggressive

    combined_width = base_card_width + (num_cards - 1) * overlap_px
    combined_height = card_images[0].height if card_images else default_card_height

    # Create a new blank transparent image
    combined_image = Image.new('RGBA', (combined_width, combined_height), (0, 0, 0, 0)) # RGBA for transparency

    # Paste each card onto the combined image
    x_offset = 0
    for img in card_images:
        combined_image.paste(img, (x_offset, 0), img) # Use img as mask for transparency
        x_offset += overlap_px

    return combined_image


# --- Blackjack Game UI Components ---

class BlackjackGameView(discord.ui.View):
    """
    The Discord UI View that holds the interactive Blackjack game buttons.
    """
    def __init__(self, game: 'BlackjackGame', bot_instance: commands.Bot):
        super().__init__(timeout=300) # Game times out after 5 minutes of inactivity
        self.game = game # Reference to the BlackjackGame instance
        self.bot_instance = bot_instance # Store the bot instance
        self.message = None # To store the message containing the game UI
        self.play_again_timeout_task = None # To store the task for the "Play Again" timeout

    async def _update_game_message(self, embed: discord.Embed, player_file: discord.File, dealer_file: discord.File, view_to_use: discord.ui.View = None):
        """Helper to update the main game message by editing the original response, including image files."""
        try:
            if self.message: # self.message holds the actual discord.Message object
                await self.message.edit(embed=embed, view=view_to_use, attachments=[player_file, dealer_file])
            else:
                logger.warning("self.message is not set. Cannot update game message.")
        except discord.errors.NotFound:
            logger.warning("Game message not found during edit, likely already deleted.")
        except Exception as e:
            logger.warning(f"An error occurred editing game message: {e}")

    def _set_button_states(self, game_state: str):
        """
        Sets the disabled state of all buttons based on the current game state.
        game_state: "playing", "game_over"
        """
        for item in self.children:
            if item.custom_id == "blackjack_hit":
                item.disabled = (game_state != "playing")
            elif item.custom_id == "blackjack_stay":
                item.disabled = (game_state != "playing")
            elif item.custom_id == "blackjack_play_again":
                item.disabled = (game_state != "game_over") # Enabled only when game is over
        
        # Manage the "Play Again" timeout task
        if game_state == "game_over":
            if self.play_again_timeout_task and not self.play_again_timeout_task.done():
                self.play_again_timeout_task.cancel() # Cancel any existing task
            self.play_again_timeout_task = self.bot_instance.loop.create_task(self._handle_play_again_timeout())
        elif self.play_again_timeout_task and not self.play_again_timeout_task.done():
            self.play_again_timeout_task.cancel() # Cancel if game is no longer over

    async def _handle_play_again_timeout(self):
        """Handles the timeout for the 'Play Again' button."""
        try:
            await asyncio.sleep(10) # Wait for 10 seconds
            
            # If we reach here, the "Play Again" button was not pressed in time
            if self.game.channel_id in active_blackjack_games:
                # Disable all buttons
                for item in self.children:
                    item.disabled = True
                
                # Update the message to indicate timeout
                if self.message:
                    try:
                        await self.message.edit(content="Blackjack game ended due to inactivity (Play Again not pressed).", view=self, embed=self.message.embed)
                    except discord.errors.NotFound:
                        logger.warning("Game message not found during play again timeout, likely already deleted.")
                    except Exception as e:
                        logger.warning(f"An error occurred editing game message on play again timeout: {e}")
                
                # Clean up the game state
                del active_blackjack_games[self.game.channel_id]
                self.stop() # Stop the view's main timeout as well
                logger.info(f"Blackjack game in channel {self.game.channel_id} ended due to Play Again timeout.")
        except asyncio.CancelledError:
            # Task was cancelled because "Play Again" was clicked
            logger.info(f"Play Again timeout task for channel {self.game.channel_id} cancelled.")
        except Exception as e:
            logger.error(f"An unexpected error occurred in _handle_play_again_timeout: {e}")


    async def on_timeout(self):
        """Called when the view times out due to inactivity."""
        if self.message:
            try:
                # Disable all buttons and add a play again button if it's not already there
                self._set_button_states("game_over") # Set buttons for game over (Play Again enabled)
                await self.message.edit(content="Blackjack game timed out due to inactivity. Click 'Play Again' to start a new game.", view=self, embed=self.message.embed)

            except discord.errors.NotFound:
                logger.warning("Game message not found during timeout, likely already deleted.")
            except Exception as e:
                logger.warning(f"An error occurred editing board message on timeout: {e}")
        
        if self.game.channel_id in active_blackjack_games:
            # We don't delete the game from active_blackjack_games here,
            # as we want the "Play Again" button to be functional.
            # The game will be removed when "Play Again" is clicked or a new game starts.
            pass
        logger.info(f"Blackjack game in channel {self.game.channel_id} timed out.")


    @discord.ui.button(label="Hit", style=discord.ButtonStyle.green, custom_id="blackjack_hit")
    async def hit_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handles the 'Hit' button click."""
        if interaction.user.id != self.game.player.id:
            await interaction.response.send_message("This is not your Blackjack game!", ephemeral=True)
            return
        
        # Disable all action buttons immediately for feedback, re-enable if game continues
        for item in self.children:
            if item.custom_id in ["blackjack_hit", "blackjack_stay"]:
                item.disabled = True
        await interaction.response.edit_message(view=self) # Immediate visual update

        self.game.player_hand.append(self.game.deal_card())
        player_value = self.game.calculate_hand_value(self.game.player_hand)

        if player_value > 21:
            self._set_button_states("game_over") # Set buttons for game over
            embed, player_file, dealer_file = await self.game._create_game_embed_with_images()
            embed.set_footer(text="BUST! Serene wins.")
            await self._update_game_message(embed, player_file, dealer_file, self) # Use helper
            await update_user_kekchipz(interaction.guild.id, interaction.user.id, -50, self.bot_instance)
            # Game is over, cancel any pending play_again_timeout_task
            if self.play_again_timeout_task and not self.play_again_timeout_task.done():
                self.play_again_timeout_task.cancel()
            del active_blackjack_games[self.game.channel_id]
        else:
            self._set_button_states("playing") # Set buttons for continuing game
            embed, player_file, dealer_file = await self.game._create_game_embed_with_images()
            await self._update_game_message(embed, player_file, dealer_file, self) # Use helper

    @discord.ui.button(label="Stay", style=discord.ButtonStyle.red, custom_id="blackjack_stay")
    async def stay_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handles the 'Stay' button click."""
        if interaction.user.id != self.game.player.id:
            await interaction.response.send_message("This is not your Blackjack game!", ephemeral=True)
            return
        
        # Disable all action buttons immediately for feedback
        for item in self.children:
            if item.custom_id in ["blackjack_hit", "blackjack_stay"]:
                item.disabled = True
        await interaction.response.edit_message(view=self) # Immediate visual update

        # Serene's turn
        player_value = self.game.calculate_hand_value(self.game.player_hand)
        serene_value = self.game.calculate_hand_value(self.game.dealer_hand)

        # Serene hits until 17 or more
        while serene_value < 17:
            self.game.dealer_hand.append(self.game.deal_card())
            serene_value = self.game.calculate_hand_value(self.game.dealer_hand)
            embed, player_file, dealer_file = await self.game._create_game_embed_with_images(reveal_dealer=True)
            await self._update_game_message(embed, player_file, dealer_file, self) # Use helper
            await asyncio.sleep(1)

        result_message = ""
        kekchipz_change = 0

        player_blackjack = self.game.is_blackjack(self.game.player_hand)
        dealer_blackjack = self.game.is_blackjack(self.game.dealer_hand)

        if serene_value > 21:
            result_message = "Serene busts! You win!"
            kekchipz_change = 100 if player_blackjack else 50
        elif player_value > serene_value:
            result_message = "You win!"
            kekchipz_change = 100 if player_blackjack else 50
        elif serene_value > player_value:
            result_message = "Serene wins!"
            kekchipz_change = -100 if dealer_blackjack else -50
        else:
            result_message = "It's a push (tie)!"
            kekchipz_change = 0

        self._set_button_states("game_over") # Set buttons for game over
        embed, player_file, dealer_file = await self.game._create_game_embed_with_images(reveal_dealer=True)
        embed.set_footer(text=result_message)
        await self._update_game_message(embed, player_file, dealer_file, self) # Use helper
        await update_user_kekchipz(interaction.guild.id, interaction.user.id, kekchipz_change, self.bot_instance)
        # Game is over, cancel any pending play_again_timeout_task
        if self.play_again_timeout_task and not self.play_again_timeout_task.done():
            self.play_again_timeout_task.cancel()
        del active_blackjack_games[self.game.channel_id]

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.blurple, custom_id="blackjack_play_again", disabled=True)
    async def play_again_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handles the 'Play Again' button click by resetting the game and updating the current message."""
        if interaction.user.id != self.game.player.id:
            await interaction.response.send_message("This is not your Blackjack game!", ephemeral=True)
            return

        # Cancel the play_again_timeout_task if it's running
        if self.play_again_timeout_task and not self.play_again_timeout_task.done():
            self.play_again_timeout_task.cancel()
            self.play_again_timeout_task = None # Clear the reference

        # Disable Play Again button immediately
        button.disabled = True
        await interaction.response.edit_message(view=self) # Immediate visual update

        self.game.reset_game()
        self.game.player_hand = [self.game.deal_card(), self.game.deal_card()]
        self.game.dealer_hand = [self.game.deal_card(), self.game.deal_card()]

        self._set_button_states("playing") # Reset buttons for new game
        
        embed, player_file, dealer_file = await self.game._create_game_embed_with_images()

        try:
            await self._update_game_message(embed, player_file, dealer_file, self) # Use helper
            active_blackjack_games[self.game.channel_id] = self
        except discord.errors.NotFound:
            logger.warning("Original game message not found during 'Play Again' edit.")
            await interaction.followup.send("Could not restart game. Please try `/serene game blackjack` again.", ephemeral=True)
            if self.game.channel_id in active_blackjack_games:
                del active_blackjack_games[self.game.channel_id]
        except Exception as e:
            logger.error(f"An error occurred during 'Play Again' edit: {e}")
            await interaction.followup.send("An error occurred while restarting the game.", ephemeral=True)
            if self.game.channel_id in active_blackjack_games:
                del active_blackjack_games[self.channel_id]
        

class BlackjackGame:
    """
    Represents a single Blackjack game instance.
    Manages game state, player and Serene hands, and card deck.
    """
    def __init__(self, channel_id: int, player: discord.User, bot_instance: commands.Bot):
        self.channel_id = channel_id
        self.player = player
        self.bot_instance = bot_instance # Store the bot instance
        self.deck = self._create_standard_deck() # Initialize deck locally
        self.player_hand = []
        self.dealer_hand = [] # This will be Serene's hand
        self.game_message = None # To store the message containing the game UI
        self.game_over = False # New flag to track if the game has ended

    def _create_standard_deck(self) -> list[dict]:
        """
        Generates a standard 52-card deck with titles, numbers, and codes.
        """
        suits = ['S', 'D', 'C', 'H'] # Spades, Diamonds, Clubs, Hearts
        ranks = {
            'A': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
            '0': 10, 'J': 10, 'Q': 10, 'K': 10
        }
        rank_titles = {
            'A': 'Ace', '2': 'Two', '3': 'Three', '4': 'Four', '5': 'Five',
            '6': 'Six', '7': 'Seven', '8': 'Eight', '9': 'Nine', '0': 'Ten',
            'J': 'Jack', 'Q': 'Queen', 'K': 'King'
        }
        suit_titles = {
            'S': 'Spades', 'D': 'Diamonds', 'C': 'Clubs', 'H': 'Hearts'
        }

        deck = []
        for suit_code in suits:
            for rank_code, num_value in ranks.items():
                title = f"{rank_titles[rank_code]} of {suit_titles[suit_code]}"
                card_code = f"{rank_code}{suit_code}"
                deck.append({
                    "title": title,
                    "cardNumber": num_value,
                    "code": card_code
                })
        return deck

    def deal_card(self) -> dict:
        """
        Deals a random card from the deck. Removes the card from the deck.
        Returns the dealt card (dict with 'title', 'cardNumber', and 'code').
        """
        if not self.deck:
            # Handle case where deck is empty (e.g., reshuffle or end game)
            logger.warning("Deck is empty, cannot deal more cards.")
            # Return a dummy card with empty image and code for graceful failure
            return {"title": "No Card", "cardNumber": 0, "code": "NO_CARD"} 
        
        card = random.choice(self.deck)
        self.deck.remove(card) # Remove the dealt card from the deck
        return card

    def calculate_hand_value(self, hand: list[dict]) -> int:
        """
        Calculates the value of a Blackjack hand.
        Handles Aces (1 or 11) dynamically.
        """
        value = 0
        num_aces = 0
        for card in hand:
            card_number = card.get("cardNumber", 0)
            if card_number == 1: # Ace
                num_aces += 1
                value += 11 # Assume 11 initially
            elif card_number >= 10: # Face cards (10, Jack, Queen, King)
                value += 10
            else: # Number cards
                value += card_number
        
        # Adjust for Aces if hand value exceeds 21
        while value > 21 and num_aces > 0:
            value -= 10 # Change an Ace from 11 to 1
            num_aces -= 1
        return value

    def is_blackjack(self, hand: list[dict]) -> bool:
        """
        Checks if a hand is a "true" Blackjack (2 cards, value 21).
        """
        return len(hand) == 2 and self.calculate_hand_value(hand) == 21

    async def _create_game_embed_with_images(self, reveal_dealer: bool = False) -> tuple[discord.Embed, discord.File]:
        """
        Creates and returns a Discord Embed object and Discord.File objects
        representing the current game state with combined card images.
        :param reveal_dealer: If True, reveals Serene's hidden card.
        :return: A tuple of (discord.Embed, player_image_file, dealer_image_file).
        """
        player_value = self.calculate_hand_value(self.player_hand)
        serene_value = self.calculate_hand_value(self.dealer_hand)

        # Fetch player's kekchipz using the updated function
        player_kekchipz = await get_user_kekchipz(self.player.guild.id, self.player.id, self.bot_instance)

        # Generate player's hand image
        player_card_codes = [card['code'] for card in self.player_hand if 'code' in card]
        player_image_pil = await create_card_combo_image(','.join(player_card_codes), scale_factor=0.4, overlap_percent=0.4) # Changed scale_factor
        player_image_bytes = io.BytesIO()
        player_image_pil.save(player_image_bytes, format='PNG')
        player_image_bytes.seek(0) # Rewind to the beginning of the BytesIO object
        player_file = discord.File(player_image_bytes, filename="player_hand.png")

        # Generate Serene's hand image
        serene_display_cards_codes = []
        if reveal_dealer:
            serene_display_cards_codes = [card['code'] for card in self.dealer_hand if 'code' in card]
        else:
            # Only show the first card and a back card
            if self.dealer_hand and 'code' in self.dealer_hand[0]:
                serene_display_cards_codes.append(self.dealer_hand[0]['code'])
            serene_display_cards_codes.append("XX") # Placeholder for back of card

        serene_image_pil = await create_card_combo_image(','.join(serene_display_cards_codes), scale_factor=0.4, overlap_percent=0.4) # Changed scale_factor
        serene_image_bytes = io.BytesIO()
        serene_image_pil.save(serene_image_bytes, format='PNG')
        serene_image_bytes.seek(0)
        dealer_file = discord.File(serene_image_bytes, filename="serene_hand.png")

        # Create an embed for the game display
        embed = discord.Embed(
            title="Blackjack Game",
            description=f"**{self.player.display_name} vs. Serene**\n\n"
                        f"**{self.player.display_name}'s Kekchipz:** ${player_kekchipz}", # Display kekchipz here
            color=discord.Color.dark_green()
        )

        embed.add_field(
            name=f"{self.player.display_name}'s Hand",
            value=f"Value: {player_value}",
            inline=False
        )

        serene_hand_value_str = f"{serene_value}" if reveal_dealer else f"{self.calculate_hand_value([self.dealer_hand[0]])} + ?"
        serene_hand_titles = ', '.join([card['title'] for card in self.dealer_hand]) if reveal_dealer else f"{self.dealer_hand[0]['title']}, [Hidden Card]"
        
        embed.add_field(
            name=f"Serene's Hand (Value: {serene_hand_value_str})",
            value=serene_hand_titles,
            inline=False
        )

        # Reference the attachments in the embed
        embed.set_image(url="attachment://player_hand.png")
        embed.set_thumbnail(url="attachment://serene_hand.png")
        
        embed.set_footer(text="What would you like to do? (Hit or Stand)")
        
        return embed, player_file, dealer_file

    def reset_game(self):
        """Resets the game state for a new round."""
        self.deck = self._create_standard_deck()
        random.shuffle(self.deck)
        self.player_hand = []
        self.dealer_hand = []
        self.game_over = False

    async def start_game(self, interaction: discord.Interaction):
        """
        Starts the Blackjack game: shuffles, deals initial hands,
        and displays the initial state using an embed with combined card images.
        """
        # Deck is already created in __init__, just shuffle it
        random.shuffle(self.deck) 

        # Deal initial hands
        self.player_hand = [self.deal_card(), self.deal_card()]
        self.dealer_hand = [self.deal_card(), self.deal_card()] # This is Serene's hand
        
        # Create the view for the game, passing the bot_instance
        game_view = BlackjackGameView(game=self, bot_instance=self.bot_instance)
        
        # Create the initial embed and get image files
        initial_embed, player_file, dealer_file = await self._create_game_embed_with_images()

        # Send the message as a follow-up to the deferred slash command interaction
        self.game_message = await interaction.followup.send(embed=initial_embed, view=game_view, files=[player_file, dealer_file])
        game_view.message = self.game_message # Store message in the view for updates
        
        active_blackjack_games[self.channel_id] = game_view # Store the view instance, not the game itself


# --- Entry Point for game_main.py ---
async def start(interaction: discord.Interaction, bot_instance: commands.Bot):
    """
    This function serves as the entry point for the Blackjack game
    when called by game_main.py.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction

    if interaction.channel.id in active_blackjack_games:
        await interaction.followup.send(
            "A Blackjack game is already active in this channel! Please finish it or wait.",
            ephemeral=True
        )
        return
    
    await interaction.followup.send("Setting up Blackjack game...", ephemeral=True)
    
    # Pass the bot_instance to the BlackjackGame constructor
    blackjack_game = BlackjackGame(interaction.channel.id, interaction.user, bot_instance)
    
    # Call the start_game method of the BlackjackGame instance
    await blackjack_game.start_game(interaction)
