# --- cogs/games/my_card_game.py ---

import discord
from discord.ext import commands
import asyncio
import random
import io
import os # For environment variables like API keys
import urllib.parse # For URL encoding
import json # For parsing JSON data
from itertools import combinations # Import combinations for poker hand evaluation
from collections import Counter # Import Counter for poker hand hand evaluation
import aiohttp
from PIL import Image, ImageDraw, ImageFont # Pillow library for image manipulation
import aiomysql # Import aiomysql for database operations
import logging # Import logging

# Explicitly import UI components for clarity and to resolve potential AttributeErrors
from discord.ui import View, Select, UserSelect, Button


# Configure logging for this game module
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


def _create_standard_deck() -> list[dict]:
    """
    Generates a standard 52-card deck with titles, numbers, and codes.
    Each card is represented as a dictionary with 'code', 'title', and 'number'.
    """
    suits = ['S', 'D', 'C', 'H'] # Spades, Diamonds, Clubs, Hearts
    ranks = {
        'A': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
        '0': 10, 'J': 11, 'Q': 12, 'K': 13 # '0' for Ten as per deckofcardsapi.com, J, Q, K are 11, 12, 13
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
        for rank_code, rank_number in ranks.items():
            card_code = f"{rank_code}{suit_code}"
            card_title = f"{rank_titles[rank_code]} of {suit_titles[suit_code]}"
            
            deck.append({
                'code': card_code,
                'title': card_title,
                'number': rank_number
            })
    return deck

def _get_card_image_url(card_code: str, face_up: bool = True) -> str:
    """
    Generates the URL for a card image from deckofcardsapi.com.
    
    Args:
        card_code (str): The two-character code for the card (e.g., "AS", "0H").
        face_up (bool): If True, returns the front image URL. If False, returns the back image URL.
    
    Returns:
        str: The URL to the card image.
    """
    base_url = "https://deckofcardsapi.com/static/img/"
    if face_up:
        return f"{base_url}{card_code}.png"
    else:
        return f"{base_url}back.png"

async def _fetch_image_bytes(url: str) -> io.BytesIO | None:
    """
    Fetches image bytes from a given URL using aiohttp.
    
    Args:
        url (str): The URL of the image to fetch.
        
    Returns:
        io.BytesIO | None: A BytesIO object containing the image data, or None if fetching fails.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                image_bytes = await response.read()
                return io.BytesIO(image_bytes)
    except aiohttp.ClientError as e:
        logger.error(f"Failed to fetch image from {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching image from {url}: {e}")
        return None

async def _create_combined_card_image(cards_info: list[dict]) -> io.BytesIO | None:
    """
    Creates a single image by combining multiple card images horizontally,
    with cards overlapping and scaled down by roughly 1/3 (0.33 multiplier).
    
    Args:
        cards_info (list[dict]): A list of dictionaries, where each dict contains
                                 'code' (str) and 'face_up' (bool) for a card.
                                 Example: [{'code': 'AS', 'face_up': True}, {'code': 'back', 'face_up': False}]
                                 Note: For 'back' cards, 'code' can be anything, as face_up=False determines the URL.
    Returns:
        io.BytesIO | None: A BytesIO object containing the combined image, or None if an error occurs.
    """
    if not cards_info:
        # Return a blank transparent image if no cards are provided
        blank_img = Image.new('RGBA', (1, 1), color=(0, 0, 0, 0)) # Transparent background
        byte_arr = io.BytesIO()
        blank_img.save(byte_arr, format='PNG') # Save as PNG to preserve transparency
        byte_arr.seek(0)
        return blank_img

    card_images = []
    # Original card size from deckofcardsapi.com is 226x314 pixels
    original_width = 226
    original_height = 314
    scale_multiplier = 0.33
    # Fixed overlap amount in pixels
    overlap_amount = 60 # Increased overlap to make card numbers more visible

    target_width = int(original_width * scale_multiplier)
    target_height = int(original_height * scale_multiplier)
    
    for card_data in cards_info:
        card_code = card_data['code']
        face_up = card_data['face_up']
        image_url = _get_card_image_url(card_code, face_up=face_up)
        
        img_bytes = await _fetch_image_bytes(image_url)
        if img_bytes:
            try:
                img = Image.open(img_bytes)
                # Convert to RGBA if not already, to ensure transparency can be preserved if needed
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                # Resize the image
                img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
                card_images.append(img)
            except Exception as e:
                logger.error(f"Failed to open or resize image from bytes for {card_code}: {e}")
                # Fallback to a placeholder image with transparent background
                placeholder_img = Image.new('RGBA', (target_width, target_height), color = (100, 100, 100, 0)) # Transparent
                d = ImageDraw.Draw(placeholder_img)
                # Adjust text position for smaller placeholder
                d.text((5, target_height // 2 - 10), "N/A", fill=(255,255,255), font=ImageFont.load_default())
                card_images.append(placeholder_img)
        else:
            logger.warning(f"Could not fetch image for card code: {card_code}, face_up: {face_up}. Using placeholder.")
            # Fallback for missing images: create a placeholder with transparent background
            placeholder_img = Image.new('RGBA', (target_width, target_height), color = (100, 100, 100, 0)) # Transparent
            d = ImageDraw.Draw(placeholder_img)
            # Adjust text position for smaller placeholder
            d.text((5, target_height // 2 - 10), "N/A", fill=(255,255,255), font=ImageFont.load_default())
            card_images.append(placeholder_img)

    if not card_images:
        return None

    # Calculate total width for overlapping cards
    # Each card contributes its full width, except the last one,
    # and we subtract (num_cards - 1) * overlap_amount
    total_width = (len(card_images) * target_width) - ((len(card_images) - 1) * overlap_amount) if len(card_images) > 0 else 0
    if len(card_images) == 1: # If only one card, no overlap calculation needed
        total_width = target_width

    max_height = target_height # All cards are now the same height

    # Create a new blank image with enough space, using RGBA for transparency
    combined_image = Image.new('RGBA', (total_width, max_height), color=(0, 0, 0, 0)) # Transparent background

    x_offset = 0
    for img in card_images:
        combined_image.paste(img, (x_offset, 0), img) # Use img as mask for transparency
        x_offset += (target_width - overlap_amount) # Move offset by card width minus overlap

    byte_arr = io.BytesIO()
    combined_image.save(byte_arr, format='PNG') # Save as PNG to preserve transparency
    byte_arr.seek(0)
    return byte_arr


async def _deal_cards(
    deck: list[dict],
    num_players: int,
    cards_per_player: int,
    deal_dealer: bool = True,
    dealer_hidden_cards: int = 2
) -> dict:
    """
    Deals cards to a specified number of players and optionally to a dealer.
    Ensures no duplicate cards are dealt.
    
    Args:
        deck (list[dict]): The shuffled deck of cards. This list will be modified (cards popped).
        num_players (int): The number of players to deal to (excluding the dealer).
        cards_per_player (int): The number of cards each player receives.
        deal_dealer (bool): Whether to deal cards to a dealer.
        dealer_hidden_cards (int): Number of dealer's cards to keep face down initially.

    Returns:
        dict: A dictionary containing 'player_hands' (dict of user_id -> list of cards)
              and 'dealer_hand' (list of cards).
    """
    player_hands = {}
    dealer_hand = []

    # Deal to players
    for _ in range(cards_per_player): # Deal one card at a time to each player, then repeat
        for i in range(num_players):
            # For simplicity, we'll assume the interaction.user is player 0
            # In a real multi-player game, you'd iterate through a list of actual players
            # We don't have interaction.user here, so we'll use a placeholder for now.
            # The actual user ID will be passed from the calling context (e.g., button callback)
            current_player_id = f"player_{i}" # Placeholder for player ID

            if not deck:
                logger.warning("Not enough cards in the deck to deal to player(s).")
                return {"player_hands": player_hands, "dealer_hand": dealer_hand}

            card = deck.pop(0)
            player_hands.setdefault(current_player_id, []).append(card)

    # Deal to dealer
    if deal_dealer:
        for i in range(cards_per_player): # Dealer also gets cards_per_player
            if not deck:
                logger.warning("Not enough cards in the deck to deal to the dealer.")
                return {"player_hands": player_hands, "dealer_hand": dealer_hand}

            card = deck.pop(0)
            dealer_hand.append(card)
            
    logger.info(f"Player hands dealt: {player_hands}")
    logger.info(f"Dealer hand dealt: {dealer_hand}")
    logger.info(f"Cards remaining in deck after deal: {len(deck)}")

    return {"player_hands": player_hands, "dealer_hand": dealer_hand}


# --- UI Component Classes ---

class InviteUserSelect(UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select a player to invite...", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        selected_user = self.values[0]
        await interaction.response.send_message(
            f"You selected {selected_user.mention} to invite.", ephemeral=True
        )
        logger.info(f"User {interaction.user.display_name} selected {selected_user.display_name} for invite.")

class InviteButton(Button):
    def __init__(self):
        super().__init__(label="Invite to Game", style=discord.ButtonStyle.blurple)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Invite button clicked! (Functionality to be added)", ephemeral=True)
        logger.info(f"User {interaction.user.display_name} clicked the Invite button.")

class PlayButton(Button):
    def __init__(self):
        super().__init__(label="Play (10.00 Minimum)", style=discord.ButtonStyle.green)
        # We need to access game_state, bot, original_interaction from the parent view.
        # These will be set by the BetButtonView's __init__ method.
        self.game_state = None
        self.bot = None
        self.original_interaction = None

    async def callback(self, interaction: discord.Interaction):
        # Access the parent view to get game_state, bot, original_interaction
        view = self.view # The view property is automatically set by discord.py
        if not view:
            logger.error("PlayButton callback: View is not set.")
            await interaction.response.send_message("An internal error occurred. Please try again.", ephemeral=True)
            return

        # Disable the button immediately to prevent multiple clicks
        self.disabled = True
        # Disable all other buttons and selects in the view
        for item in view.children:
            if isinstance(item, Button) and item.label == "Invite to Game":
                item.disabled = True
            if isinstance(item, UserSelect):
                item.disabled = True
        await interaction.response.edit_message(view=view)

        await interaction.followup.send("You clicked 'Play'! Dealing cards...", ephemeral=True)
        logger.info(f"Player {interaction.user.display_name} clicked the Play button.")

        # Assign the actual player ID to the game state
        # For simplicity, assuming only one player for now (the one who clicked)
        view.game_state['players_in_game'] = [interaction.user.id]

        # Deal cards using the game_state's deck
        dealt_info = await _deal_cards(
            view.game_state['deck'],
            num_players=1, # Only the command invoker for now
            cards_per_player=2,
            deal_dealer=True,
            dealer_hidden_cards=2 # Both dealer cards hidden initially
        )
        view.game_state['player_hands'][interaction.user.id] = dealt_info['player_hands'].get(f"player_0", [])
        view.game_state['dealer_hand'] = dealt_info['dealer_hand']

        # --- Update Public Message (Dealer's hand - both face down) ---
        public_cards_info = []
        for card in view.game_state['dealer_hand']:
            public_cards_info.append({'code': card['code'], 'face_up': False})
        
        updated_combined_image_bytes = await _create_combined_card_image(public_cards_info)

        if updated_combined_image_bytes:
            updated_public_file = discord.File(updated_combined_image_bytes, filename="dealer_cards_updated.png")
            
            try:
                # Retrieve the original public message to edit it
                channel = view.bot.get_channel(view.game_state['channel_id'])
                if not channel:
                    channel = await view.bot.fetch_channel(view.game_state['channel_id'])
                
                public_message_to_edit = await channel.fetch_message(view.game_state['public_message_id'])
                await public_message_to_edit.edit(
                    content="**🃏 Dealer's Hand:**",
                    attachments=[updated_public_file], # Replace existing file
                    view=None # Remove all buttons and selects from the public message
                )
                logger.info(f"Public message {view.game_state['public_message_id']} updated with dealer's cards.")
            except discord.NotFound:
                logger.error(f"Public message with ID {view.game_state['public_message_id']} not found during update.")
            except Exception as e:
                logger.error(f"Error updating public message: {e}")
        else:
            await interaction.followup.send(
                "Could not create updated dealer's hand image for public display.",
                ephemeral=True
            )

        # --- Send Ephemeral Message to Player (Player's cards face up) ---
        player_hand = view.game_state['player_hands'].get(interaction.user.id, [])
        
        if player_hand:
            player_cards_info = [{'code': card['code'], 'face_up': True} for card in player_hand]
            player_combined_image = await _create_combined_card_image(player_cards_info)

            if player_combined_image:
                player_file = discord.File(player_combined_image, filename="your_hand.png")
                await interaction.followup.send(
                    f"👋 {interaction.user.mention}, here is your hand:",
                    file=player_file,
                    ephemeral=True # This makes the message private
                )
                logger.info(f"Ephemeral message sent to {interaction.user.display_name}")
            else:
                await interaction.followup.send(
                    f"Could not display your hand image, {interaction.user.mention}.",
                    ephemeral=True
                )
        else:
            await interaction.followup.send(f"You don't have any cards, {interaction.user.mention}.", ephemeral=True)

        view.stop() # Stop the view after the button is clicked and actions are performed

class BetButtonView(View): # Inherit from View
    def __init__(self, game_state: dict, bot: commands.Bot, original_interaction: discord.Interaction):
        super().__init__(timeout=180) # Timeout after 3 minutes if no interaction
        self.game_state = game_state
        self.bot = bot
        self.original_interaction = original_interaction # Store the initial interaction
        
        # Add instances of the custom UI components
        self.add_item(InviteUserSelect())
        self.add_item(InviteButton())
        
        # Instantiate PlayButton and pass necessary data from the view's context
        play_button_instance = PlayButton()
        play_button_instance.game_state = game_state
        play_button_instance.bot = bot
        play_button_instance.original_interaction = original_interaction
        self.add_item(play_button_instance)


    async def on_timeout(self):
        # This method is called when the view times out
        logger.info("BetButtonView timed out.")
        if self.game_state['public_message_id']:
            try:
                channel = self.bot.get_channel(self.game_state['channel_id'])
                if not channel:
                    channel = await self.bot.fetch_channel(self.game_state['channel_id'])
                public_message = await channel.fetch_message(self.game_state['public_message_id'])
                await public_message.edit(content="Game setup timed out. Please start a new game.", view=None)
            except discord.NotFound:
                logger.error(f"Public message with ID {self.game_state['public_message_id']} not found on timeout.")
            except Exception as e:
                logger.error(f"Error updating public message on timeout: {e}")


async def start(interaction: discord.Interaction, bot):
    """
    This function is the entry point for 'My Card Game'.
    It now sets up the initial public message with a 'Play' button.
    Card dealing and ephemeral messages are triggered by the button click.
    """
    await interaction.response.send_message("My Card Game is starting! Click 'Play' to begin.", ephemeral=False)
    await asyncio.sleep(1)

    # --- Game State (In-memory for demonstration) ---
    game_state = {
        'deck': _create_standard_deck(),
        'player_hands': {},
        'dealer_hand': [],
        'community_cards': [],
        'public_message_id': None,
        'channel_id': interaction.channel_id
    }
    random.shuffle(game_state['deck'])

    # --- Send Initial Public Message (Placeholder for dealer's hand) with button ---
    # Initially, display a blank image or a game logo if preferred, or just a message.
    # For now, we'll send a blank transparent image as a placeholder for the dealer's cards.
    initial_public_cards_info = [] # No cards to display yet, just the button
    initial_combined_image_bytes = await _create_combined_card_image(initial_public_cards_info) # Creates a 1x1 transparent image

    view = BetButtonView(game_state, bot, interaction)

    if initial_combined_image_bytes:
        initial_public_file = discord.File(initial_combined_image_bytes, filename="game_start_placeholder.png")
        public_message = await interaction.followup.send(
            "**🃏 Game Table: Ready to Play!**\nClick the button below to start the game.",
            file=initial_public_file,
            view=view # Attach the view with the button
        )
        game_state['public_message_id'] = public_message.id
        logger.info(f"Initial public message sent with ID: {public_message.id} and Play button.")
    else:
        await interaction.followup.send(
            "Could not create initial game table image. Game might not proceed as expected.",
            view=view, # Still attach the view even if image fails
            ephemeral=False
        )

    # Wait for the button to be clicked or for the timeout
    await view.wait()
    
    # The rest of the game logic (dealing, sending ephemeral messages, updating public message)
    # is now handled within the BetButtonView's callback (play_button method)
    
