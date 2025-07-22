# --- cogs/games/Serene Texas Hold Em.py ---

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
import time # Import time for Unix timestamps

# Explicitly import UI components for clarity
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
        return byte_arr # Return the BytesIO object here

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

async def _create_public_board_image(dealer_cards_info: list[dict], community_cards_info: list[dict]) -> io.BytesIO | None:
    """
    Creates a single image for the public game board, combining dealer's cards
    and community cards, with enough height for both rows.
    
    Args:
        dealer_cards_info (list[dict]): List of dicts for dealer's cards ({'code': str, 'face_up': bool}).
        community_cards_info (list[dict]): List of dicts for community cards ({'code': str, 'face_up': bool}).
        
    Returns:
        io.BytesIO | None: A BytesIO object containing the combined image, or None if an error occurs.
    """
    # Original card size from deckofcardsapi.com is 226x314 pixels
    original_width = 226
    original_height = 314
    scale_multiplier = 0.33
    overlap_amount = 60

    target_width = int(original_width * scale_multiplier)
    target_height = int(original_height * scale_multiplier)

    # Calculate image bytes for dealer's cards
    dealer_image_bytes = await _create_combined_card_image(dealer_cards_info)
    dealer_img = Image.open(dealer_image_bytes) if dealer_image_bytes else None

    # Calculate image bytes for community cards
    community_image_bytes = await _create_combined_card_image(community_cards_info)
    community_img = Image.open(community_image_bytes) if community_image_bytes else None

    # Determine overall dimensions
    max_row_width = 0
    if dealer_img:
        max_row_width = max(max_row_width, dealer_img.width)
    if community_img:
        max_row_width = max(max_row_width, community_img.width)
    
    # If no cards at all, return a minimal transparent image
    if not dealer_img and not community_img:
        blank_img = Image.new('RGBA', (target_width, target_height * 2 + 20), color=(0, 0, 0, 0))
        byte_arr = io.BytesIO()
        blank_img.save(byte_arr, format='PNG')
        byte_arr.seek(0)
        return byte_arr

    # Height for two rows of cards + some padding between them
    total_height = (target_height * 2) + 20 # 20 pixels padding between rows

    # Create the combined image with transparent background
    combined_image = Image.new('RGBA', (max_row_width, total_height), color=(0, 0, 0, 0))

    y_offset = 0
    if dealer_img:
        # Center dealer cards horizontally
        x_offset_dealer = (max_row_width - dealer_img.width) // 2
        combined_image.paste(dealer_img, (x_offset_dealer, y_offset), dealer_img)
        y_offset += dealer_img.height + 20 # Move y_offset past dealer cards + padding

    if community_img:
        # Center community cards horizontally
        x_offset_community = (max_row_width - community_img.width) // 2
        combined_image.paste(community_img, (x_offset_community, y_offset), community_img)

    byte_arr = io.BytesIO()
    combined_image.save(byte_arr, format='PNG')
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


# --- UI Component Classes (Ordered by dependency) ---

class GameModeSelect(Select):
    def __init__(self):
        super().__init__(
            placeholder="Select game mode...",
            options=[
                discord.SelectOption(label="Single Player", value="single_player", default=True),
                discord.SelectOption(label="Multiplayer", value="multiplayer")
            ],
            min_values=1,
            max_values=1,
            row=0 # Place it at the top row
        )

    async def callback(self, interaction: discord.Interaction):
        selected_mode = self.values[0]
        view = self.view # Get reference to the parent view

        if selected_mode == "single_player":
            view.invite_user_select.disabled = True
            view.invite_button.disabled = True
            # The StartGameButton is now on GameBoardView, not BetButtonView.
            # Its state will be managed when BetButtonView transitions to GameBoardView.
            view.selected_users_for_invite = [] # Clear selected users if switching to single player
            view.game_state['invited_players_status'] = {} # Clear invited players status
            logger.info(f"Game mode set to Single Player by {interaction.user.display_name}.")
            # Immediately trigger transition to GameBoardView for single player
            await view._check_all_players_responded()
        elif selected_mode == "multiplayer":
            view.invite_user_select.disabled = False
            view.invite_button.disabled = True # Invite button disabled until user(s) selected
            # StartGameButton is on GameBoardView, not BetButtonView.
            logger.info(f"Game mode set to Multiplayer by {interaction.user.display_name}.")
        
        # Update the default selection in the select menu itself
        for option in self.options:
            option.default = (option.value == selected_mode)

        await interaction.response.edit_message(view=view)


class InviteUserSelect(UserSelect):
    def __init__(self):
        # Initially disabled for single player default
        super().__init__(placeholder="Select players to invite...", min_values=1, max_values=25, disabled=True, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view # Get reference to the parent view
        view.selected_users_for_invite = self.values # Store the selected user objects

        # Enable invite button if users are selected in multiplayer mode
        if not self.disabled and view.game_mode_select.values[0] == "multiplayer" and len(self.values) > 0:
            view.invite_button.disabled = False
        else:
            view.invite_button.disabled = True

        await interaction.response.edit_message(view=view)
        logger.info(f"User {interaction.user.display_name} selected {len(self.values)} users for invite.")

class StartGameButton(Button): # Renamed from PlayButton
    def __init__(self):
        super().__init__(label="Play ($10.00 Minimum)", style=discord.ButtonStyle.green, row=0) # Row 0 for GameBoardView

    async def callback(self, interaction: discord.Interaction):
        view = self.view # This will be GameBoardView
        if not view:
            logger.error("StartGameButton callback: View is not set.")
            await interaction.response.defer()
            return

        # Disable and remove this button after it's clicked
        self.disabled = True
        view.remove_item(self)
        await interaction.response.edit_message(view=view) # Update the message to remove the button

        logger.info(f"Player {interaction.user.user.display_name} clicked the Play button. Starting game flow.")

        # If players_in_game is not set yet, default to the interacting user for single player
        if 'players_in_game' not in view.game_state or not view.game_state['players_in_game']:
            view.game_state['players_in_game'] = [interaction.user.id]
            num_players_to_deal = 1
        else:
            num_players_to_deal = len(view.game_state['players_in_game'])


        dealt_info = await _deal_cards(
            view.game_state['deck'],
            num_players=num_players_to_deal,
            cards_per_player=2,
            deal_dealer=True,
            dealer_hidden_cards=2
        )
        player_idx = 0
        for player_id in view.game_state['players_in_game']:
            view.game_state['player_hands'][player_id] = dealt_info['player_hands'].get(f"player_{player_idx}", [])
            player_idx += 1
        view.game_state['dealer_hand'] = dealt_info['dealer_hand']

        dealer_public_cards_info = []
        for card in view.game_state['dealer_hand']:
            dealer_public_cards_info.append({'code': card['code'], 'face_up': False})
        
        # Update the public board image with dealt cards
        updated_public_board_image_bytes = await _create_public_board_image(dealer_public_cards_info, view.game_state['community_cards'])

        if updated_public_board_image_bytes:
            updated_public_file = discord.File(updated_public_board_image_bytes, filename="game_board_dealt.png")
            
            try:
                channel = view.bot.get_channel(view.game_state['channel_id'])
                if not channel:
                    channel = await view.bot.fetch_channel(view.game_state['channel_id'])
                
                public_message_to_edit = await channel.fetch_message(view.game_state['public_message_id'])
                
                # Add and enable the "Show Me My Cards Again" button now that cards are dealt
                view.add_item(view.show_my_cards_button) # Add the item to the view
                view.show_my_cards_button.disabled = False # Enable it

                await public_message_to_edit.edit(
                    content="**🃏 Dealer's Hand & Community Cards:**",
                    attachments=[updated_public_file],
                    view=view # Keep the existing GameBoardView
                )
                logger.info(f"Public message {view.game_state['public_message_id']} updated with dealt cards and 'Show Me My Cards Again' button.")
            except discord.NotFound:
                logger.error(f"Public message with ID {view.game_state['public_message_id']} not found during update.")
            except Exception as e:
                logger.error(f"Error updating public message: {e}")
        else:
            try:
                channel = view.bot.get_channel(view.game_state['channel_id'])
                if not channel:
                    channel = await view.bot.fetch_channel(view.game_state['channel_id'])
                public_message_to_edit = await channel.fetch_message(view.game_state['public_message_id'])
                # Add and enable even if image fails
                view.add_item(view.show_my_cards_button)
                view.show_my_cards_button.disabled = False
                await public_message_to_edit.edit(
                    content="**🃏 Dealer's Hand & Community Cards: (Image failed to load)**",
                    view=view
                )
            except Exception as e:
                logger.error(f"Error handling image creation failure in public message: {e}")
            logger.error("Could not create updated dealer's hand image for public display.")

        # No need to stop the view here, as GameBoardView should persist.


class ShowMyCardsButton(Button):
    def __init__(self, game_state: dict, bot: commands.Bot):
        super().__init__(label="Show Me My Cards Again", style=discord.ButtonStyle.primary, row=1) # Moved to row 1 to make space for StartGameButton
        self.game_state = game_state
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        player_id = interaction.user.id
        if player_id not in self.game_state['players_in_game']:
            await interaction.response.send_message("You are not a player in this game!", ephemeral=True)
            return

        player_hand = self.game_state['player_hands'].get(player_id)
        if player_hand:
            player_cards_info = [{'code': card['code'], 'face_up': True} for card in player_hand]
            player_combined_image = await _create_combined_card_image(player_cards_info)

            card_titles = [card['title'] for card in player_hand]
            player_hand_text = ", ".join(card_titles)

            if player_combined_image:
                player_file = discord.File(player_combined_image, filename="your_hand.png")
                await interaction.response.send_message(
                    f"👋 {interaction.user.mention}, here is your hand: {player_hand_text}",
                    file=player_file,
                    ephemeral=True
                )
                logger.info(f"Ephemeral message sent to {interaction.user.display_name}")
            else:
                await interaction.response.send_message(
                    f"Could not display your hand image, {interaction.user.mention}. Your cards: {player_hand_text}",
                    ephemeral=True
                )
        else:
            await interaction.response.send_message(f"You don't have any cards, {interaction.user.mention}.", ephemeral=True)


class GameBoardView(View): # This view will hold the game board and in-game buttons
    def __init__(self, game_state: dict, bot: commands.Bot):
        super().__init__(timeout=None) # This view should persist indefinitely for the game board
        self.game_state = game_state
        self.bot = bot
        
        # Add the StartGameButton here, initially disabled
        self.start_game_button = StartGameButton()
        self.add_item(self.start_game_button)
        self.start_game_button.disabled = True # Disabled until setup is complete

        # Instantiate ShowMyCardsButton, but DO NOT add it to the view yet
        self.show_my_cards_button = ShowMyCardsButton(game_state, bot)
        # self.add_item(self.show_my_cards_button) # Removed this line
        # self.show_my_cards_button.disabled = True # This will be set when added


class InviteButton(Button):
    def __init__(self):
        super().__init__(label="Invite to Game", style=discord.ButtonStyle.blurple, row=2)

    async def _handle_invite_timeout(self, invited_user_id: int, invite_message_view: discord.ui.View, invite_message_obj: discord.Message, invited_user_mention: str, inviter_mention: str):
        """Handles the automatic denial of an invite after a timeout."""
        countdown_seconds = 60
        view = self.view # Get the parent view (BetButtonView)

        # Wait for the full countdown duration
        await asyncio.sleep(countdown_seconds)

        # After countdown, check if status is still 'waiting'
        user_status_info = view.game_state['invited_players_status'].get(invited_user_id)

        if user_status_info and user_status_info.get('status') == 'waiting':
            logger.info(f"Invite for {invited_user_id} timed out. Automatically declining.")
            user_status_info['status'] = 'denied'
            user_status_info['countdown_end_time'] = 0 

            for item in invite_message_view.children:
                item.disabled = True
            
            try:
                await invite_message_obj.edit(
                    content=f"{invited_user_mention} did not respond to {inviter_mention}'s game invite in time and it has been automatically declined.",
                    view=invite_message_view
                )
                await invite_message_obj.delete(delay=5)
            except discord.NotFound:
                logger.warning(f"Invite message {invite_message_obj.id} not found for timeout update or deletion.")
            except Exception as e:
                logger.error(f"Error updating/deleting invite message on timeout for {invited_user_id}: {e}")

            invite_message_view.stop()

            await view._update_public_game_status_message()
            await view._check_all_players_responded()


    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not view:
            logger.error("InviteButton callback: View is not set.")
            await interaction.response.defer()
            return

        invited_users = view.selected_users_for_invite
        if not invited_users:
            logger.warning("Invite button clicked without selected users.")
            await interaction.response.defer()
            return

        # Remove the game mode select, user select, invite button from the view
        view.remove_item(view.game_mode_select)
        view.remove_item(view.invite_user_select)
        view.remove_item(self)
        # The StartGameButton is now handled by GameBoardView, so we don't remove it from BetButtonView here.
        # BetButtonView will be replaced entirely by GameBoardView.

        await interaction.response.edit_message(view=view) # Update the message to remove the select menus and invite button
        
        logger.info(f"User {interaction.user.display_name} clicked the Invite button for {len(invited_users)} users.")

        DB_USER = os.getenv("DB_USER")
        DB_PASSWORD = os.getenv("DB_PASSWORD")
        DB_HOST = os.getenv("DB_HOST")
        
        notif_channel_id = None
        conn = None
        try:
            conn = await aiomysql.connect(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                db="serene_users",
                charset='utf8mb4',
                autocommit=True
            )
            async with conn.cursor() as cur:
                guild_id = interaction.guild_id
                if guild_id:
                    await cur.execute("SELECT notif_channel FROM bot_guild_settings WHERE guild_id = %s", (str(guild_id),))
                    result = await cur.fetchone()
                    if result:
                        notif_channel_id = result[0]
                        logger.info(f"Retrieved notif_channel_id: {notif_channel_id} for guild {guild_id}")
                    else:
                        logger.warning(f"No notif_channel found for guild_id: {guild_id}")
                else:
                    logger.warning("Interaction has no guild_id. Cannot fetch notif_channel.")

        except Exception as e:
            logger.error(f"Database error when fetching notif_channel: {e}")
            # Re-add components if DB error occurs
            view.add_item(view.game_mode_select)
            view.add_item(view.invite_user_select)
            view.add_item(self)
            # view.add_item(view.start_game_button) # This button is not on BetButtonView anymore
            self.disabled = False
            await interaction.response.edit_message(view=view)
            await interaction.followup.send("Failed to fetch notification channel from database.", ephemeral=True)
            return
        finally:
            if conn:
                conn.close()

        if not notif_channel_id:
            # Re-add components if no channel found
            view.add_item(view.game_mode_select)
            view.add_item(view.invite_user_select)
            view.add_item(self)
            # view.add_item(view.start_game_button) # This button is not on BetButtonView anymore
            self.disabled = False
            await interaction.response.edit_message(view=view)
            await interaction.followup.send("Notification channel not configured for this server.", ephemeral=True)
            return

        for invited_user in invited_users:
            try:
                notif_channel = view.bot.get_channel(int(notif_channel_id))
                if not notif_channel:
                    notif_channel = await view.bot.fetch_channel(int(notif_channel_id))
                
                # Create a new view for each invite message
                invite_message_view = discord.ui.View(timeout=60)

                # Accept Button
                accept_button = discord.ui.Button(
                    label="Accept", # Changed label to "Accept"
                    style=discord.ButtonStyle.green,
                    custom_id=f"accept_invite_{invited_user.id}",
                    row=0
                )
                invite_message_view.add_item(accept_button)

                # Deny Button
                deny_button = discord.ui.Button(
                    label="Deny", 
                    style=discord.ButtonStyle.red, 
                    custom_id=f"deny_invite_{invited_user.id}",
                    row=0
                )
                invite_message_view.add_item(deny_button)

                async def accept_callback(accept_interaction: discord.Interaction):
                    if accept_interaction.user.id == invited_user.id:
                        await accept_interaction.response.defer()
                        logger.info(f"Invite accepted by {accept_interaction.user.display_name}.")
                        
                        # Update game state
                        view.game_state['invited_players_status'][invited_user.id]['status'] = 'accepted'
                        view.game_state['invited_players_status'][invited_user.id]['countdown_end_time'] = 0
                        await view._update_public_game_status_message()
                        await view._check_all_players_responded()

                        # Create a new view with only the "Join Game" link button
                        join_game_link_view = discord.ui.View(timeout=None) # This view can persist
                        join_game_link_button = discord.ui.Button(
                            label="Join Game",
                            style=discord.ButtonStyle.link,
                            url=interaction.channel.jump_url, # Link to the game channel
                            row=0
                        )
                        join_game_link_view.add_item(join_game_link_button)

                        # Edit the original invite message to show "Accepted!" and the "Join Game" link
                        await accept_interaction.message.edit(
                            content=f"{accept_interaction.user.mention} has accepted the game invite! Click 'Join Game' to go to the channel.",
                            view=join_game_link_view # Replace the old view with the new link view
                        )
                        invite_message_view.stop() # Stop the old view with Accept/Deny buttons
                        logger.info(f"Invite message for {invited_user.display_name} updated with Join Game link.")

                        # The message will not be deleted on click of "Join Game" as it's a URL button.
                        # It will be cleaned up by the main game flow's timeout or when the game starts.

                    else:
                        await accept_interaction.response.send_message("This invite is not for you!", ephemeral=True)

                accept_button.callback = accept_callback

                async def deny_callback(deny_interaction: discord.Interaction):
                    if deny_interaction.user.id == invited_user.id:
                        await deny_interaction.response.defer() 
                        logger.info(f"Invite denied by {deny_interaction.user.display_name}.")
                        for item in invite_message_view.children:
                            item.disabled = True
                        await deny_interaction.message.edit(content=f"{deny_interaction.user.mention} has denied the game invite.", view=invite_message_view)
                        invite_message_view.stop()

                        view.game_state['invited_players_status'][invited_user.id]['status'] = 'denied'
                        view.game_state['invited_players_status'][invited_user.id]['countdown_end_time'] = 0 
                        await view._update_public_game_status_message()
                        await view._check_all_players_responded()
                        await deny_interaction.message.delete(delay=5)
                    else:
                        await deny_interaction.response.send_message("This invite is not for you!", ephemeral=True)
                
                deny_button.callback = deny_callback

                countdown_end_timestamp = int(time.time() + 60)

                initial_invite_content = (
                    f"**🎮 Game Invite!** {invited_user.mention}, {interaction.user.mention} has invited you to a game of Serene Texas Hold'em!\n"
                    f"Please respond by <t:{countdown_end_timestamp}:R>."
                )
                
                invite_message_obj = await notif_channel.send(
                    initial_invite_content,
                    view=invite_message_view
                )
                logger.info(f"Invite sent to {invited_user.display_name} in channel {notif_channel.name}.")

                view.game_state['invited_players_status'][invited_user.id] = {
                    'status': 'waiting',
                    'invite_message_id': invite_message_obj.id,
                    'countdown_end_time': countdown_end_timestamp
                }

                view.bot.loop.create_task(
                    self._handle_invite_timeout(
                        invited_user.id, 
                        invite_message_view, 
                        invite_message_obj, 
                        invited_user.mention, 
                        interaction.user.mention
                    )
                )

            except Exception as e:
                logger.error(f"Error sending invite message to {invited_user.display_name}: {e}")
                view.game_state['invited_players_status'][invited_user.id] = {
                    'status': 'failed',
                    'invite_message_id': None,
                    'countdown_end_time': 0
                }
        
        await view._update_public_game_status_message()
        await interaction.response.defer()


class BetButtonView(View): # Inherit from View
    def __init__(self, game_state: dict, bot: commands.Bot, original_interaction: discord.Interaction):
        super().__init__(timeout=180) # Timeout after 3 minutes if no interaction
        self.game_state = game_state
        self.bot = bot
        self.original_interaction = original_interaction # Store the initial interaction
        self.selected_users_for_invite = [] # To store the list of user objects selected for invite
        # Initialize invited_players_status with a more detailed structure
        self.game_state['invited_players_status'] = {} # {user_id: {'status': 'waiting'/'accepted'/'denied'/'failed', 'invite_message_id': None, 'countdown_end_time': 0}}

        # Instantiate UI components
        self.game_mode_select = GameModeSelect()
        self.invite_user_select = InviteUserSelect()
        self.invite_button = InviteButton()
        # The StartGameButton is NOT part of BetButtonView initially.
        # It will be added to GameBoardView.
        # We need a dummy instance here to pass to GameModeSelect's callback for disabling.
        self.start_game_button = StartGameButton() 

        # Add items to the view in desired order
        self.add_item(self.game_mode_select)
        self.add_item(self.invite_user_select)
        self.add_item(self.invite_button)
        # self.add_item(self.start_game_button) # Removed from here, as it's on GameBoardView

        # Set initial states based on default single player
        self.invite_user_select.disabled = True
        self.invite_button.disabled = True
        # The start_game_button is not part of this view, so its disabled state here is irrelevant.
        # Its state will be managed by GameBoardView.

    async def _update_public_game_status_message(self):
        """Updates the public message with the current status of invited players, including countdown."""
        if not self.game_state['public_message_id']:
            logger.warning("No public message ID to update status.")
            return

        status_lines = []
        for user_id, status_info in self.game_state['invited_players_status'].items():
            status = status_info['status']
            countdown_end_time = status_info.get('countdown_end_time', 0)
            
            try:
                user = await self.bot.fetch_user(user_id)
                emoji = ""
                status_text = status.capitalize()
                
                if status == 'waiting':
                    # Use Discord's relative timestamp for the public status message
                    emoji = "⏳"
                    status_text = f"Waiting (responds by <t:{countdown_end_time}:R>)"
                elif status == 'accepted':
                    emoji = "✅"
                    status_text = "Accepted"
                elif status == 'denied':
                    emoji = "❌"
                    status_text = "Denied"
                elif status == 'failed':
                    emoji = "⚠️"
                    status_text = "Failed to send invite"
                
                status_lines.append(f"{emoji} {user.mention} ({status_text})")
            except discord.NotFound:
                status_lines.append(f"❓ Unknown User (ID: {user_id}) ({status.capitalize()})")
            except Exception as e:
                logger.error(f"Error fetching user {user_id} for status update: {e}")
                status_lines.append(f"❓ Error User (ID: {user_id}) ({status.capitalize()})")
        
        status_text_combined = "\n".join(status_lines) if status_lines else "No players invited yet."
        
        try:
            channel = self.bot.get_channel(self.game_state['channel_id'])
            if not channel:
                channel = await self.bot.fetch_channel(self.game_state['channel_id'])
            
            public_message_to_edit = await channel.fetch_message(self.game_state['public_message_id'])
            
            # Preserve the image if it exists, otherwise send without it
            # For simplicity, we'll just update the content, the image stays.
            await public_message_to_edit.edit(
                content=f"**🃏 Game Table: Player Status**\n{status_text_combined}",
            )
            logger.info(f"Public message {self.game_state['public_message_id']} updated with player statuses.")
        except discord.NotFound:
            logger.error(f"Public message with ID {self.game_state['public_message_id']} not found during status update.")
        except Exception as e:
            logger.error(f"Error updating public message with player status: {e}")

    async def _check_all_players_responded(self):
        """Checks if all invited players have responded and enables the Play button if so."""
        # This method is now responsible for transitioning from BetButtonView to GameBoardView
        if not self.game_state['invited_players_status']: # No players invited (implies single player)
            if self.game_mode_select.values[0] == "single_player":
                # Finalize players_in_game for single player
                self.game_state['players_in_game'] = [self.original_interaction.user.id]

                game_board_view = GameBoardView(self.game_state, self.bot)
                game_board_view.start_game_button.disabled = False
                # The show_my_cards_button is not added to GameBoardView yet, so no need to disable it here.
                
                try:
                    channel = self.bot.get_channel(self.game_state['channel_id'])
                    if not channel:
                        channel = await self.bot.fetch_channel(self.game_state['channel_id'])
                    public_message_to_edit = await channel.fetch_message(self.game_state['public_message_id'])
                    await public_message_to_edit.edit(
                        content="**🃏 Game Table: Ready to Play!**",
                        view=game_board_view # Replace BetButtonView with GameBoardView
                    )
                    logger.info("Single player mode: GameBoardView set with enabled Play button.")
                except Exception as e:
                    logger.error(f"Error transitioning to GameBoardView for single player: {e}")
                self.stop() # Stop BetButtonView
            return

        all_responded = True
        for status_info in self.game_state['invited_players_status'].values():
            if status_info['status'] == 'waiting':
                all_responded = False
                break
        
        if all_responded:
            # Collect accepted players for the game_state
            accepted_players = [
                user_id for user_id, status_info in self.game_state['invited_players_status'].items() 
                if status_info['status'] == 'accepted'
            ]
            # Add the initiator if they are not already in the accepted list (e.g., if they didn't invite themselves)
            if self.original_interaction.user.id not in accepted_players:
                accepted_players.append(self.original_interaction.user.id)
            self.game_state['players_in_game'] = accepted_players

            # Transition to GameBoardView and enable its StartGameButton
            game_board_view = GameBoardView(self.game_state, self.bot)
            game_board_view.start_game_button.disabled = False
            # The show_my_cards_button is not added to GameBoardView yet, so no need to disable it here.

            try:
                channel = self.bot.get_channel(self.game_state['channel_id'])
                if not channel:
                    channel = await self.bot.fetch_channel(self.game_state['channel_id'])
                public_message_to_edit = await channel.fetch_message(self.game_state['public_message_id'])
                await public_message_to_edit.edit(
                    content="**🃏 Game Table: All players responded! Ready to Play!**",
                    view=game_board_view # Replace BetButtonView with GameBoardView
                )
                logger.info("All invited players have responded. GameBoardView set with enabled Play button.")
            except Exception as e:
                logger.error(f"Error transitioning to GameBoardView for multiplayer: {e}")
            self.stop() # Stop BetButtonView


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
    await interaction.response.send_message("My Card Game is starting! Please select a game mode.", ephemeral=False)
    await asyncio.sleep(1)

    # --- Game State (In-memory for demonstration) ---
    game_state = {
        'deck': _create_standard_deck(),
        'player_hands': {},
        'dealer_hand': [],
        'community_cards': [],
        'public_message_id': None,
        'channel_id': interaction.channel_id,
        'invited_players_status': {},
        'players_in_game': [] # Initialize players_in_game here
    }
    random.shuffle(game_state['deck'])

    initial_combined_image_bytes = await _create_public_board_image([], [])

    view = BetButtonView(game_state, bot, interaction)

    if initial_combined_image_bytes:
        initial_public_file = discord.File(initial_combined_image_bytes, filename="game_start_placeholder.png")
        public_message = await interaction.followup.send(
            "**🃏 Game Table: Select your game mode:**",
            file=initial_public_file,
            view=view
        )
        game_state['public_message_id'] = public_message.id
        logger.info(f"Initial public message sent with ID: {public_message.id} and game mode selection.")
    else:
        await interaction.followup.send(
            "Could not create initial game table image. Game might not proceed as expected.",
            view=view,
            ephemeral=False
        )

    await view.wait()
