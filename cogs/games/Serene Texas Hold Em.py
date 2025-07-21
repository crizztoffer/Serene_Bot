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
        # Return a blank image or a placeholder if no cards are provided
        blank_img = Image.new('RGB', (1, 1), color=(54, 57, 63))
        byte_arr = io.BytesIO()
        blank_img.save(byte_arr, format='PNG')
        byte_arr.seek(0)
        return byte_arr

    card_images = []
    # Original card size from deckofcardsapi.com is 226x314 pixels
    original_width = 226
    original_height = 314
    scale_multiplier = 0.33

    target_width = int(original_width * scale_multiplier)
    target_height = int(original_height * scale_multiplier)
    
    # Set overlap amount to a raw pixel value of 30
    overlap_amount = 30 

    for card_data in cards_info:
        card_code = card_data['code']
        face_up = card_data['face_up']
        image_url = _get_card_image_url(card_code, face_up=face_up)
        
        img_bytes = await _fetch_image_bytes(image_url)
        if img_bytes:
            try:
                img = Image.open(img_bytes)
                # Resize the image
                img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
                card_images.append(img)
            except Exception as e:
                logger.error(f"Failed to open or resize image from bytes for {card_code}: {e}")
                # Fallback to a placeholder image
                placeholder_img = Image.new('RGB', (target_width, target_height), color = (100, 100, 100))
                d = ImageDraw.Draw(placeholder_img)
                # Adjust text position for smaller placeholder
                d.text((5, target_height // 2 - 10), "N/A", fill=(255,255,255), font=ImageFont.load_default())
                card_images.append(placeholder_img)
        else:
            logger.warning(f"Could not fetch image for card code: {card_code}, face_up: {face_up}. Using placeholder.")
            # Fallback for missing images: create a placeholder
            placeholder_img = Image.new('RGB', (target_width, target_height), color = (100, 100, 100))
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

    # Create a new blank image with enough space
    combined_image = Image.new('RGB', (total_width, max_height), color=(54, 57, 63)) # Discord background color

    x_offset = 0
    for img in card_images:
        combined_image.paste(img, (x_offset, 0))
        x_offset += (target_width - overlap_amount) # Move offset by card width minus overlap

    byte_arr = io.BytesIO()
    combined_image.save(byte_arr, format='PNG')
    byte_arr.seek(0)
    return byte_arr


async def _deal_cards(
    interaction: discord.Interaction, # Interaction is not directly used for display in this func, but for context
    deck: list[dict],
    num_players: int,
    cards_per_player: int,
    deal_dealer: bool = True,
    dealer_hidden_cards: int = 2 # Changed to 2 as per new requirement
) -> dict:
    """
    Deals cards to a specified number of players and optionally to a dealer.
    Ensures no duplicate cards are dealt.
    
    Args:
        interaction (discord.Interaction): The Discord interaction object.
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
            current_player_id = interaction.user.id if i == 0 else f"other_player_{i}"

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


async def start(interaction: discord.Interaction, bot):
    """
    This function is the entry point for 'My Card Game'.
    It's the minimum required for game_main.py to recognize this file as a game.
    It now sets up the initial public message with dealer's face-down cards
    and then sends the player's cards in an ephemeral message.
    """
    await interaction.response.send_message("My Card Game has started!", ephemeral=False)
    await asyncio.sleep(1)
    await interaction.followup.send("Setting up the game table...", ephemeral=False)

    # --- Game State (In-memory for demonstration) ---
    game_state = {
        'deck': _create_standard_deck(),
        'player_hands': {},
        'dealer_hand': [],
        'community_cards': [], # No community cards initially
        'public_message_id': None, # To store the ID of the public message
        'channel_id': interaction.channel_id # To retrieve the public message later
    }
    random.shuffle(game_state['deck']) # Shuffle the deck

    # --- Initial Deal ---
    # Deal 2 cards to 1 player and 2 to the dealer
    dealt_info = await _deal_cards(
        interaction, # Pass interaction for logging/context, not for direct message sending within _deal_cards
        game_state['deck'],
        num_players=1, # Only the command invoker for now
        cards_per_player=2,
        deal_dealer=True,
        dealer_hidden_cards=2 # Both dealer cards hidden initially
    )
    game_state['player_hands'] = dealt_info['player_hands']
    game_state['dealer_hand'] = dealt_info['dealer_hand']

    # --- Send Initial Public Message (Dealer's hand - both face down) ---
    public_cards_info = []
    # Both dealer cards are face down
    for card in game_state['dealer_hand']:
        public_cards_info.append({'code': card['code'], 'face_up': False})
    
    # No community cards below them initially, so only dealer cards are combined
    combined_image_bytes = await _create_combined_card_image(public_cards_info)

    if combined_image_bytes:
        public_file = discord.File(combined_image_bytes, filename="dealer_cards.png")
        public_message = await interaction.followup.send(
            "**🃏 Dealer's Hand:**",
            file=public_file
        )
        game_state['public_message_id'] = public_message.id
        logger.info(f"Public message sent with ID: {public_message.id}")
    else:
        await interaction.followup.send(
            "Could not create initial dealer's hand image.",
            ephemeral=False # This message should still be public if image fails
        )

    await asyncio.sleep(1) # Small pause before sending player's ephemeral message

    # --- Send Ephemeral Message to Player (Player's cards face up) ---
    player_id = interaction.user.id
    player_hand = game_state['player_hands'].get(player_id, [])
    
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


    await asyncio.sleep(2)
    await interaction.followup.send("Thanks for playing My Card Game!", ephemeral=False)
