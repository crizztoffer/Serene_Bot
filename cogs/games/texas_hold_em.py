import discord
from discord.ext import commands
import asyncio
import random
import io
import os # For environment variables like API keys
import urllib.parse # For URL encoding
import json # For parsing JSON data
from itertools import combinations # Import combinations for poker hand evaluation
from collections import Counter # Import Counter for poker hand evaluation
import aiohttp
from PIL import Image, ImageDraw, ImageFont # Pillow library for image manipulation

# --- Game State Storage ---
# This dictionary will store active Texas Hold 'em games by channel ID.
active_texasholdem_games = {}

# --- Database Operations (Placeholders) ---
# These functions are placeholders for actual database interactions.
# In a real application, you would connect to a database (e.g., MySQL, PostgreSQL, SQLite)
# to persist user data like "kekchipz" (a fictional currency).
# For this isolated file, they simply print messages.

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
            print(f"Failed to fetch PNG for card '{card}' from {png_url}: {e}")
            # If the first card fails, ensure default dimensions are set
            if first_card_width is None:
                first_card_width = default_card_width
                first_card_height = default_card_height
            # Append a placeholder for failed cards to avoid breaking the layout
            card_images.append(Image.new('RGBA', (int(first_card_width * scale_factor), int(first_card_height * scale_factor)), (255, 0, 0, 128))) # Red transparent placeholder
        except Exception as e:
            print(f"Error processing PNG for card '{card}' from {png_url}: {e}")
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


# Poker Hand Evaluation Functions (from user's provided code)
RANKS = {'2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
         '7': 7, '8': 8, '9': 9, '0': 10, 'J': 11,
         'Q': 12, 'K': 13, 'A': 14}

HAND_NAMES = {
    1: "high card",
    2: "one pair",
    3: "two pair",
    4: "three of a kind",
    5: "straight",
    6: "flush",
    7: "full house",
    8: "four of a kind",
    9: "straight flush"
}

def get_card_value(card):
    """Extracts the numerical value of a card from its code (e.g., 'AS' -> 14)."""
    return RANKS[card[0]]

def get_card_suit(card):
    """Extracts the suit of a card from its code (e.g., 'AS' -> 'S')."""
    return card[1]

def hand_name(rank):
    """Returns the descriptive name of a poker hand given its rank."""
    return HAND_NAMES.get(rank, "unknown")

def score_hand(cards):
    """
    Scores a 5-card poker hand.
    Returns a list representing the hand's rank and kickers for comparison.
    """
    values = sorted([get_card_value(c) for c in cards])
    suits = [get_card_suit(c) for c in cards]
    counts = Counter(values)
    counts_by_value = sorted(counts.items(), key=lambda x: (-x[1], -x[0]))
    sorted_by_count = []
    for val, count in counts_by_value:
        sorted_by_count.extend([val] * count)

    is_flush = len(set(suits)) == 1
    unique_values = sorted(set(values))

    # Check for straight
    is_straight = False
    high_card = None
    if len(unique_values) >= 5: # Ensure there are enough unique cards for a straight
        for i in range(len(unique_values) - 4):
            window = unique_values[i:i+5]
            if window[-1] - window[0] == 4 and len(set(window)) == 5: # Check for consecutive and unique
                is_straight = True
                high_card = window[-1]

    # Special case: A-2-3-4-5 (Ace treated as 1)
    # Check if the hand contains A, 2, 3, 4, 5 and is a straight
    if set([14, 2, 3, 4, 5]).issubset(values) and len(unique_values) == 5: # Ace (14) is present, and 2,3,4,5
        # Ensure it's not a higher straight that just happens to contain these
        if not is_straight or high_card != 14: # If it's not already a higher straight
            is_straight = True
            high_card = 5 # For A-2-3-4-5, the high card for comparison is 5

    # Straight flush
    if is_straight and is_flush:
        return [9, high_card]

    # Four of a kind
    if 4 in counts.values():
        quad_rank = counts_by_value[0][0] # Rank of the four of a kind
        kicker = [v for v in sorted_by_count if v != quad_rank][0] # The remaining card
        return [8, quad_rank, kicker]

    # Full house
    if 3 in counts.values() and 2 in counts.values():
        trip_rank = counts_by_value[0][0] # Rank of the three of a kind
        pair_rank = counts_by_value[1][0] # Rank of the pair
        return [7, trip_rank, pair_rank]

    # Flush
    if is_flush:
        return [6] + sorted(values, reverse=True)[:5] # Top 5 cards for tie-breaking

    # Straight
    if is_straight:
        return [5, high_card]

    # Three of a kind
    if 3 in counts.values():
        trip_rank = counts_by_value[0][0]
        kickers = [v for v in sorted_by_count if v != trip_rank][:2] # Top 2 kickers
        return [4, trip_rank] + sorted(kickers, reverse=True)

    # Two pair
    if list(counts.values()).count(2) == 2:
        pair1_rank = counts_by_value[0][0]
        pair2_rank = counts_by_value[1][0]
        kicker = [v for v in sorted_by_count if v not in [pair1_rank, pair2_rank]][0]
        return [3, max(pair1_rank, pair2_rank), min(pair1_rank, pair2_rank), kicker]

    # One pair
    if 2 in counts.values():
        pair_rank = counts_by_value[0][0]
        kickers = [v for v in sorted_by_count if v != pair_rank][:3] # Top 3 kickers
        return [2, pair_rank] + sorted(kickers, reverse=True)

    # High card
    return [1] + sorted(values, reverse=True)[:5] # Top 5 high cards

def evaluate_best_hand(seven_cards):
    """
    Evaluates the best 5-card poker hand from a given 7 cards.
    """
    best = None
    for combo in combinations(seven_cards, 5):
        score = score_hand(combo)
        if not best or compare_scores(score, best) > 0:
            best = score
    return best

def compare_scores(score1, score2):
    """
    Compares two poker hand scores.
    Returns 1 if score1 is better, -1 if score2 is better, 0 if tie.
    """
    for a, b in zip(score1, score2):
        if a > b: return 1
        if a < b: return -1
    return 0


class TexasHoldEmGameView(discord.ui.View):
    """
    The Discord UI View that holds the interactive Texas Hold 'em game buttons.
    """
    def __init__(self, game: 'TexasHoldEmGame', bot_instance: commands.Bot):
        super().__init__(timeout=300) # Game times out after 5 minutes of inactivity
        self.game = game # Reference to the TexasHoldEmGame instance
        self.bot_instance = bot_instance # Store the bot instance
        # Initialize button states for the pre-flop phase
        self._set_button_states("pre_flop")

    def _set_button_states(self, phase: str, betting_buttons_visible: bool = False):
        """
        Manages the visibility and disabled state of buttons based on game phase.
        Buttons are dynamically added/removed to control their visibility.
        """
        print(f"Setting button states for phase: {phase}, betting_buttons_visible: {betting_buttons_visible}")
        self.clear_items() # Clear all existing items from the view

        # Define buttons and their callbacks
        # Buttons are created here without decorators so they can be added conditionally
        raise_button = discord.ui.Button(label="Raise", style=discord.ButtonStyle.green, custom_id="holdem_raise_main", row=0)
        call_button = discord.ui.Button(label="Call", style=discord.ButtonStyle.blurple, custom_id="holdem_call_main", row=0)
        check_button = discord.ui.Button(label="Check", style=discord.ButtonStyle.gray, custom_id="holdem_check_main", row=0)
        fold_button = discord.ui.Button(label="Fold", style=discord.ButtonStyle.red, custom_id="holdem_fold_main", row=0)
        bet_5_button = discord.ui.Button(label="$5", style=discord.ButtonStyle.secondary, custom_id="holdem_bet_5", row=1)
        bet_10_button = discord.ui.Button(label="$10", style=discord.ButtonStyle.secondary, custom_id="holdem_bet_10", row=1)
        bet_25_button = discord.ui.Button(label="$25", style=discord.ButtonStyle.secondary, custom_id="holdem_bet_25", row=1)
        play_again_button = discord.ui.Button(label="Play Again", style=discord.ButtonStyle.blurple, custom_id="holdem_play_again", row=2)

        # Assign callbacks to the buttons
        raise_button.callback = self.raise_main_callback
        call_button.callback = self.call_main_callback
        check_button.callback = self.check_main_callback
        fold_button.callback = self.fold_main_callback
        bet_5_button.callback = self.bet_amount_callback
        bet_10_button.callback = self.bet_amount_callback
        bet_25_button.callback = self.bet_amount_callback
        play_again_button.callback = self.play_again_callback

        # Add buttons based on the current game phase and flags
        if betting_buttons_visible:
            # Player is choosing a raise amount, so show bet buttons and Fold
            self.add_item(bet_5_button)
            self.add_item(bet_10_button)
            self.add_item(bet_25_button)
            self.add_item(fold_button) # Player can always fold
        elif phase == "pre_flop":
            # Pre-flop actions: Raise, Call (big blind), Fold
            self.add_item(raise_button)
            self.add_item(call_button)
            self.add_item(fold_button)
            # Check is not available pre-flop
        elif phase in ["flop", "turn", "river"]:
            # Post-flop actions
            if self.game.dealer_raise_amount > 0: # Serene has made a bet/raise
                # Player must respond to dealer's raise: Call or Fold
                self.add_item(call_button)
                self.add_item(fold_button)
            else:
                # No outstanding raise from Serene: Player can Check, Raise, or Fold
                self.add_item(check_button)
                self.add_item(raise_button)
                self.add_item(fold_button)
        # For "showdown" or "folded" phases, no action buttons are added here,
        # only the "Play Again" button will be present (added below).

        # Always add the 'Play Again' button, and set its disabled state
        play_again_button.disabled = (phase not in ["showdown", "folded"])
        self.add_item(play_again_button)

    def _end_game_buttons(self):
        """Disables all game progression buttons and enables 'Play Again'."""
        self.clear_items() # Clear all buttons
        # Add only the Play Again button and ensure it's enabled
        self.add_item(discord.ui.Button(label="Play Again", style=discord.ButtonStyle.blurple, custom_id="holdem_play_again", row=2, disabled=False))
        # Assign callback explicitly since decorators are removed
        self.children[-1].callback = self.play_again_callback
        return self

    async def on_timeout(self):
        """Called when the view times out due to inactivity."""
        if self.game.game_message:
            try:
                self._end_game_buttons() # Enable Play Again, disable others
                await self.game.game_message.edit(content=f"{self.game.player.display_name}'s turn timed out. Click 'Play Again' to start a new game.", view=self, attachments=[])
            except discord.errors.NotFound:
                print("WARNING: Game message not found during timeout, likely already deleted.")
            except Exception as e:
                print(f"WARNING: An error occurred editing game message on timeout: {e}")
        
        if self.game.channel_id in active_texasholdem_games:
            pass # Keep for Play Again functionality
        print(f"Texas Hold 'em game in channel {self.game.channel_id} timed out.")

    # Removed @discord.ui.button decorators from all callbacks
    async def raise_main_callback(self, interaction: discord.Interaction): # Removed button argument as it's not used
        try:
            if interaction.user.id != self.game.player.id:
                await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
                return
            
            await interaction.response.defer() # Defer the interaction to prevent "This interaction failed"

            self.game.current_bet_buttons_visible = True
            self._set_button_states(self.game.game_phase, betting_buttons_visible=True)
            # _update_game_message will handle editing the original deferred response with the new view
            await self.game._update_game_message(self)
        except Exception as e:
            print(f"Error in raise_main_callback: {e}")
            if not interaction.response.is_done():
                await interaction.followup.send("An error occurred during your Raise action. Please try again or contact support.", ephemeral=True)


    async def call_main_callback(self, interaction: discord.Interaction): # Removed button argument
        try:
            if interaction.user.id != self.game.player.id:
                await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
                return
            
            await interaction.response.defer() # Defer to allow time for updates

            # Case 1: Pre-flop Call (player calls the initial big blind)
            if self.game.game_phase == "pre_flop":
                self.game.g_total = self.game.minimum_bet * 2 # Player matches big blind
                self.game.deal_flop()
                self._set_button_states("flop") # Transition to flop buttons
                await self.game._update_game_message(self)
                return # Important: return after handling a case

            # Case 2: Player calls after dealer's raise (post-flop)
            elif self.game.player_action_pending and self.game.dealer_raise_amount > 0:
                self.game.g_total += self.game.dealer_raise_amount * 2 # Player matches dealer's raise
                self.game.dealer_raise_amount = 0 # Reset dealer's raise
                self.game.player_action_pending = False # Player's action is complete

                # Advance game phase
                if self.game.game_phase == "flop":
                    self.game.deal_turn()
                    self._set_button_states("turn")
                elif self.game.game_phase == "turn":
                    self.game.deal_river()
                    self._set_button_states("river")
                elif self.game.game_phase == "river":
                    self.game.game_phase = "showdown"
                    self._end_game_buttons()
                    await self.game._update_game_message(self, reveal_opponent=True)
                    del active_texasholdem_games[self.game.channel_id]
                    self.stop()
                    return # Game ends here
                await self.game._update_game_message(self)
                return

            # If we reach here, it means the 'Call' button was clicked in an unexpected state
            # (e.g., no outstanding bet to call, but the button was somehow visible).
            # This indicates an internal logic error or an unexpected user interaction.
            else:
                await interaction.followup.send("Invalid call action: No active bet to call or unexpected game state.", ephemeral=True)
                # Re-evaluate button states to try and correct the display
                self._set_button_states(self.game.game_phase, betting_buttons_visible=self.game.current_bet_buttons_visible) # Ensure betting buttons state is preserved if applicable
                await self.game._update_game_message(self)

        except Exception as e:
            print(f"Error in call_main_callback: {e}")
            if not interaction.response.is_done():
                await interaction.followup.send("An error occurred during your Call action. Please try again or contact support.", ephemeral=True)


    async def fold_main_callback(self, interaction: discord.Interaction): # Removed button argument
        try:
            if interaction.user.id != self.game.player.id:
                await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
                return
            
            await interaction.response.defer() # Defer to allow time for updates

            # Player folds, loses minimum bet (pre-flop) or current Gtotal contribution
            kekchipz_lost = self.game.minimum_bet if self.game.game_phase == "pre_flop" else self.game.g_total / 2 # Assuming half of Gtotal is player's contribution
            await update_user_kekchipz(interaction.guild.id, interaction.user.id, -int(kekchipz_lost))
            
            self._end_game_buttons()
            self.game.game_phase = "folded" # Indicate game ended by fold
            await self.game._update_game_message(self, reveal_opponent=True)
            await interaction.followup.send(f"{self.game.player.display_name} folded. You lost ${int(kekchipz_lost)} kekchipz. Game over.")
            del active_texasholdem_games[self.game.channel_id]
            self.stop()
        except Exception as e:
            print(f"Error in fold_main_callback: {e}")
            if not interaction.response.is_done():
                await interaction.followup.send("An error occurred during your Fold action. Please try again or contact support.", ephemeral=True)


    async def check_main_callback(self, interaction: discord.Interaction): # Removed button argument
        try:
            if interaction.user.id != self.game.player.id:
                await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
                return
            
            await interaction.response.defer() # Defer to allow time for updates

            # Get the bot's current hand (hole cards + community cards)
            bot_all_cards = self.game.bot_hole_cards + self.game.community_cards
            bot_hand_score = evaluate_best_hand([c['code'] for c in bot_all_cards])
            bot_hand_rank = bot_hand_score[0] # Get the hand rank (1-9)

            dealer_action = 1 # Default to check (1)
            raise_amount = 0

            # AI Logic for dealer's action
            if bot_hand_rank >= 7: # Strong hand: Full House, Four of a Kind, Straight Flush
                if random.random() < 0.8: # 80% chance to raise
                    dealer_action = 2 # Raise
                    raise_amount = random.choice([10, 25]) # Aggressive raise
                else: # 20% chance to check (for deception)
                    dealer_action = 1 # Check
            elif bot_hand_rank >= 4: # Medium hand: Three of a Kind, Straight, Flush
                if random.random() < 0.5: # 50% chance to raise
                    dealer_action = 2 # Raise
                    raise_amount = random.choice([5, 10]) # Moderate raise
                else: # 50% chance to check
                    dealer_action = 1 # Check
            else: # Weak hand: High Card, One Pair, Two Pair
                if random.random() < 0.2: # 20% chance to bluff raise
                    dealer_action = 2 # Raise
                    raise_amount = random.choice([5]) # Small bluff raise
                else: # 80% chance to check
                    dealer_action = 1 # Check

            if dealer_action == 1: # Dealer checks
                if self.game.game_phase == "flop":
                    self.game.deal_turn()
                    self._set_button_states("turn")
                elif self.game.game_phase == "turn":
                    self.game.deal_river()
                    self._set_button_states("river")
                elif self.game.game_phase == "river": # If dealer checks on river, go to showdown
                    self.game.game_phase = "showdown"
                    self._end_game_buttons()
                    await self.game._update_game_message(self, reveal_opponent=True)
                    del active_texasholdem_games[self.game.channel_id]
                    self.stop()
                    return # Exit early after showdown
                
                await self.game._update_game_message(self)
                # No separate message for Serene checking, it's implied by game progression
            else: # Dealer raises
                self.game.dealer_raise_amount = raise_amount
                self.game.player_action_pending = True # Player must now call or fold

                # When dealer raises, player can only Call or Fold
                self._set_button_states(self.game.game_phase, betting_buttons_visible=False)
                await self.game._update_game_message(self) # Update the main message with raise info
                # The raise message will now be part of the main game message content
        except Exception as e:
            print(f"Error in check_main_callback: {e}")
            if not interaction.response.is_done():
                await interaction.followup.send("An error occurred during your Check action. Please try again or contact support.", ephemeral=True)


    async def bet_amount_callback(self, interaction: discord.Interaction): # Removed button argument
        try:
            if interaction.user.id != self.game.player.id:
                await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
                return
            
            await interaction.response.defer() # Defer to allow time for updates

            # The button object is passed implicitly to the callback
            # We can get the custom_id from interaction.data
            custom_id = interaction.data['custom_id']
            bet_amount = int(custom_id.split('_')[-1]) # Extract amount from "holdem_bet_X"
            
            self.game.handle_player_raise(bet_amount) # This updates g_total and sets betting_buttons_visible to False

            # Advance game phase based on current phase
            if self.game.game_phase == "pre_flop":
                self.game.deal_flop()
                self._set_button_states("flop")
            elif self.game.game_phase == "flop":
                self.game.deal_turn()
                self._set_button_states("turn")
            elif self.game.game_phase == "turn":
                self.game.deal_river()
                self._set_button_states("river")
            elif self.game.game_phase == "river": # After river, next is showdown
                self.game.game_phase = "showdown" # Set game phase to showdown
                self._end_game_buttons() # Disable all game buttons, enable Play Again
                await self.game._update_game_message(self, reveal_opponent=True)
                del active_texasholdem_games[self.game.channel_id]
                self.stop()
                return # Exit early after showdown
            
            await self.game._update_game_message(self)
        except Exception as e:
            print(f"Error in bet_amount_callback: {e}")
            if not interaction.response.is_done():
                await interaction.followup.send("An error occurred during your Bet action. Please try again or contact support.", ephemeral=True)


    async def play_again_callback(self, interaction: discord.Interaction): # Removed button argument
        if interaction.user.id != self.game.player.id:
            await interaction.response.send_message("This is not your Texas Hold 'em game!", ephemeral=True)
            return
        
        # Defer the interaction immediately
        await interaction.response.defer()

        # Stop the current view (the one with the "Play Again" button)
        self.stop() 

        # Remove the old game from active games to allow a new one
        if self.game.channel_id in active_texasholdem_games:
            del active_texasholdem_games[self.game.channel_id]

        # Always attempt to edit the old message to indicate game over
        try:
            # Create a temporary view with no active buttons to replace the old one
            temp_view = discord.ui.View(timeout=1) # Short timeout
            temp_view.add_item(discord.ui.Button(label="Game Ended", style=discord.ButtonStyle.red, disabled=True))
            await self.game.game_message.edit(content=f"Game over for {self.game.player.display_name}. Starting a new game...", view=temp_view, attachments=[])
        except discord.errors.NotFound:
            print("WARNING: Old game message not found during 'Play Again' cleanup. It might have been deleted manually.")
        except Exception as e:
            print(f"WARNING: Error editing old game message during 'Play Again' cleanup: {e}")

        # Start a brand new game
        try:
            new_holdem_game = TexasHoldEmGame(interaction.channel.id, interaction.user, self.bot_instance)
            await new_holdem_game.start_game(interaction) # This will send a new message
        except Exception as e:
            print(f"Error starting new game from Play Again: {e}")
            await interaction.followup.send("An error occurred while trying to start a new game. Please try `/serene game texas_hold_em` again.", ephemeral=True)


class TexasHoldEmGame:
    """
    Represents a single Texas Hold 'em game instance.
    Manages game state, player hands, and community cards.
    """
    def __init__(self, channel_id: int, player: discord.User, bot_instance: commands.Bot):
        self.channel_id = channel_id
        self.player = player # Human player
        self.bot_player = bot_instance.user # Serene bot as opponent
        self.bot_instance = bot_instance # Store the bot instance
        self.deck = self._create_standard_deck()
        self.player_hole_cards = []
        self.bot_hole_cards = []
        self.community_cards = []
        
        # Game state for betting
        self.minimum_bet = 10
        self.g_total = 0
        self.current_bet_buttons_visible = False # Flag to control visibility of $5, $10, $25 buttons
        self.dealer_raise_amount = 0 # Stores the amount dealer raised
        self.player_action_pending = False # True if player needs to respond to dealer's raise

        # Store reference to the single game message
        self.game_message = None
        
        self.game_phase = "pre_flop" # pre_flop, flop, turn, river, showdown, folded

    def _create_standard_deck(self) -> list[dict]:
        """
        Generates a standard 52-card deck with titles, numbers, and codes.
        """
        suits = ['S', 'D', 'C', 'H'] # Spades, Diamonds, Clubs, Hearts
        ranks = {
            'A': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
            '0': 10, 'J': 10, 'Q': 12, 'K': 13, 'A': 14 # '0' for Ten (as per deckofcardsapi.com)
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
            print("Warning: Deck is empty, cannot deal more cards.")
            return {"title": "No Card", "cardNumber": 0, "code": "NO_CARD"} 
        
        card = random.choice(self.deck)
        self.deck.remove(card)
        return card

    def deal_hole_cards(self):
        """Deals 2 hole cards to each player."""
        self.player_hole_cards = [self.deal_card(), self.deal_card()]
        self.bot_hole_cards = [self.deal_card(), self.deal_card()]
        self.game_phase = "pre_flop"

    def deal_flop(self):
        """Deals 3 community cards (the flop)."""
        self.community_cards.extend([self.deal_card(), self.deal_card(), self.deal_card()])
        self.game_phase = "flop"

    def deal_turn(self):
        """Deals 1 community card (the turn)."""
        self.community_cards.append(self.deal_card())
        self.game_phase = "turn"

    def deal_river(self):
        """Deals 1 community card (the river)."""
        self.community_cards.append(self.deal_card())
        self.game_phase = "river"

    def handle_player_raise(self, bet_amount: int):
        """Handles player's raise action."""
        if self.game_phase == "pre_flop":
            self.g_total = (bet_amount * 2) + self.minimum_bet # Raise amount * 2 + minimum
        else:
            self.g_total += (bet_amount * 2) # Add raise amount * 2 to existing Gtotal
        self.current_bet_buttons_visible = False # Hide betting buttons after selection
        self.dealer_raise_amount = 0 # Reset dealer raise if player initiated raise
        self.player_action_pending = False # Reset pending action

    def handle_player_fold(self):
        """Handles player's fold action."""
        # This is handled directly in the view callback for immediate response
        pass # Logic moved to view

    def reset_game(self):
        """Resets the game state for a new round."""
        self.deck = self._create_standard_deck()
        random.shuffle(self.deck)
        self.player_hole_cards = []
        self.bot_hole_cards = []
        self.community_cards = []
        self.game_phase = "pre_flop" # Reset phase
        self.g_total = 0 # Reset Gtotal
        self.current_bet_buttons_visible = False
        self.dealer_raise_amount = 0
        self.player_action_pending = False


    async def _create_combined_holdem_image(self, player_name: str, bot_name: str, reveal_opponent: bool = False) -> Image.Image:
        """
        Creates a single combined image for Texas Hold 'em, showing dealer's cards,
        community cards, and player's cards, along with text labels.

        Args:
            player_name (str): The display name of the human player.
            bot_name (str): The display name of the bot player.
            reveal_opponent (bool): If True, reveals the bot's hole cards.

        Returns:
            PIL.Image.Image: A Pillow Image object containing the combined game state.
        """
        # Define image scaling and padding
        card_scale_factor = 1.0 # Changed to 1.0
        card_overlap_percent = 0.33
        vertical_padding = 40 # Increased padding
        text_padding_x = 20 # Increased padding
        text_padding_y = 30 # Further increased padding to move text higher from cards

        # Get individual card images
        # Bot's hand
        bot_display_card_codes = [card['code'] for card in self.bot_hole_cards if 'code' in card] if reveal_opponent else ["XX", "XX"]
        bot_hand_img = await create_card_combo_image(','.join(bot_display_card_codes), scale_factor=card_scale_factor, overlap_percent=card_overlap_percent)

        # Community cards
        community_card_codes = [card['code'] for card in self.community_cards if 'code' in card]
        community_img = await create_card_combo_image(','.join(community_card_codes), scale_factor=card_scale_factor, overlap_percent=card_overlap_percent)
        
        # Player's hand
        player_card_codes = [card['code'] for card in self.player_hole_cards if 'code' in card]
        player_hand_img = await create_card_combo_image(','.join(player_card_codes), scale_factor=card_scale_factor, overlap_percent=card_overlap_percent)

        # --- Font Loading ---
        font_url = "http://serenekeks.com/OpenSans-CondBold.ttf" # Changed font URL
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
        font_small = ImageFont.load_default()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(font_url) as response:
                    response.raise_for_status()
                    font_bytes = await response.read()
                    font_io = io.BytesIO(font_bytes)
                    # Adjusted font sizes
                    font_large = ImageFont.truetype(font_io, 48) # Increased size
                    font_io.seek(0) # Reset buffer for next font size
                    font_medium = ImageFont.truetype(font_io, 36) # Increased size
                    font_io.seek(0)
                    font_small = ImageFont.truetype(font_io, 28) # Increased size
                    print(f"Successfully loaded font from {font_url}")
        except aiohttp.ClientError as e:
            print(f"WARNING: Failed to fetch font from {font_url}: {e}. Using default Pillow font.")
        except Exception as e:
            print(f"WARNING: Error loading font from bytes: {e}. Using default Pillow font.")

        # Define Discord purple color (R, G, B)
        discord_purple = (114, 137, 218)

        # Calculate text heights and widths for layout
        dummy_img = Image.new('RGBA', (1, 1))
        dummy_draw = ImageDraw.Draw(dummy_img)

        dealer_text = "Serene's Hand" # Changed dealer's hand text
        player_text = f"{player_name}'s Hand"
        
        # Determine showdown result if applicable
        showdown_result_text = ""
        if reveal_opponent and self.game_phase == "showdown":
            player_all_cards = self.player_hole_cards + self.community_cards
            bot_all_cards = self.bot_hole_cards + self.community_cards

            player_best_hand = evaluate_best_hand([c['code'] for c in player_all_cards])
            bot_best_hand = evaluate_best_hand([c['code'] for c in bot_all_cards])

            player_hand_name = hand_name(player_best_hand[0])
            bot_hand_name = hand_name(bot_best_hand[0])

            comparison = compare_scores(player_best_hand, bot_best_hand)

            if comparison > 0:
                showdown_result_text = f"{player_name} wins with {player_hand_name}!" # Removed emojis
                # Award kekchipz to player
                await update_user_kekchipz(self.player.guild.id, self.player.id, 200) # Example: 200 kekchipz for winning
            elif comparison < 0:
                showdown_result_text = f"Serene wins with {bot_hand_name}!" # Removed emojis
                # Deduct kekchipz from player
                await update_user_kekchipz(self.player.guild.id, self.player.id, -100) # Example: -100 kekchipz for losing
            else:
                showdown_result_text = f"It's a tie with {player_hand_name}!" # Removed emojis
                # Small kekchipz for tie
                await update_user_kekchipz(self.player.guild.id, self.player.id, 50) # Example: 50 kekchipz for a tie

        # Calculate text dimensions
        showdown_text_width = 0
        showdown_text_height = 0
        if showdown_result_text:
            bbox = dummy_draw.textbbox((0,0), showdown_result_text, font=font_large)
            showdown_text_width = bbox[2] - bbox[0]
            showdown_text_height = bbox[3] - bbox[1]

        bbox = dummy_draw.textbbox((0,0), dealer_text, font=font_medium)
        dealer_text_width = bbox[2] - bbox[0]
        dealer_text_height = bbox[3] - bbox[1]

        bbox = dummy_draw.textbbox((0,0), player_text, font=font_medium)
        player_text_width = bbox[2] - bbox[0]
        player_text_height = bbox[3] - bbox[1]
        
        # Determine overall image dimensions
        # Max content width must account for both card images and text labels
        max_content_width = max(
            bot_hand_img.width,
            community_img.width,
            player_hand_img.width,
            showdown_text_width,
            dealer_text_width,
            player_text_width
        )
        # Increase overall image width to accommodate text - INCREASED MULTIPLIER HERE
        combined_image_width = max_content_width + text_padding_x * 20 # Increased from 12 to 20 for more width
        
        # Calculate total height
        total_height = (
            vertical_padding + # Top padding
            showdown_text_height + text_padding_y + # Showdown text and its padding
            dealer_text_height + text_padding_y + # Dealer text and its padding
            bot_hand_img.height + vertical_padding + # Dealer cards and padding
            community_img.height + vertical_padding + # Community cards and padding
            player_text_height + text_padding_y + # Player text and its padding
            player_hand_img.height + vertical_padding # Player cards and bottom padding
        )

        # Create the final combined image with a transparent background
        combined_image = Image.new('RGBA', (combined_image_width, total_height), (0, 0, 0, 0)) # Transparent background

        draw = ImageDraw.Draw(combined_image)

        current_y_offset = vertical_padding # Start with some top padding

        # Draw Showdown Result if applicable
        if showdown_result_text:
            showdown_x_offset = (combined_image.width - showdown_text_width) // 2
            draw.text((showdown_x_offset, current_y_offset), showdown_result_text, font=font_large, fill=(255, 255, 0)) # Yellow for result
            current_y_offset += showdown_text_height + text_padding_y


        # Draw Dealer's Hand text
        dealer_text_x_offset = (combined_image.width - dealer_text_width) // 2
        draw.text((dealer_text_x_offset, current_y_offset), dealer_text, font=font_medium, fill=discord_purple) # Discord purple text
        current_y_offset += dealer_text_height + text_padding_y # Increased padding

        # Calculate dealer_img_x_offset here, before it's used
        dealer_img_x_offset = (combined_image.width - bot_hand_img.width) // 2

        # Draw Dealer's Raise amount if applicable
        if self.dealer_raise_amount > 0 and not reveal_opponent: # Only show if dealer raised and not yet showdown
            dealer_raise_text = f"Raise: ${self.dealer_raise_amount}"
            bbox = dummy_draw.textbbox((0,0), dealer_raise_text, font=font_small)
            dealer_raise_text_width = bbox[2] - bbox[0]
            dealer_raise_text_height = bbox[3] - bbox[0]
            # Position to the left of dealer's hand image
            dealer_raise_x = dealer_img_x_offset - dealer_raise_text_width - text_padding_x
            draw.text((dealer_raise_x, current_y_offset + bot_hand_img.height // 2 - dealer_raise_text_height // 2),
                      dealer_raise_text, font=font_small, fill=(255, 165, 0)) # Orange for raise amount

        # Paste Dealer's Hand image
        combined_image.paste(bot_hand_img, (dealer_img_x_offset, current_y_offset), bot_hand_img)
        current_y_offset += bot_hand_img.height + vertical_padding

        # Paste Community Cards (no text label)
        community_img_x_offset = (combined_image.width - community_img.width) // 2
        combined_image.paste(community_img, (community_img_x_offset, current_y_offset), community_img)
        current_y_offset += community_img.height + vertical_padding

        # Draw Player's Hand text
        player_text_x_offset = (combined_image.width - player_text_width) // 2
        draw.text((player_text_x_offset, current_y_offset), player_text, font=font_medium, fill=discord_purple) # Discord purple text
        current_y_offset += player_text_height + text_padding_y # Increased padding

        # Calculate player_img_x_offset here, before it's used
        player_img_x_offset = (combined_image.width - player_hand_img.width) // 2

        # Draw Minimum and Gtotal text to the left of player's cards
        min_text = f"Minimum: ${self.minimum_bet}"
        gtotal_text = f"Gtotal: ${self.g_total}"

        bbox_min = dummy_draw.textbbox((0,0), min_text, font=font_small)
        min_text_width = bbox_min[2] - bbox_min[0]
        min_text_height = bbox_min[3] - bbox_min[1]

        bbox_gtotal = dummy_draw.textbbox((0,0), gtotal_text, font=font_small)
        gtotal_text_width = bbox_gtotal[2] - bbox_gtotal[0]
        gtotal_text_height = bbox_gtotal[3] - bbox_gtotal[1]

        # Position to the left of player's hand image
        player_info_x = player_img_x_offset - max(min_text_width, gtotal_text_width) - text_padding_x
        player_info_y_start = current_y_offset + player_hand_img.height // 2 - (min_text_height + gtotal_text_height + 5) // 2 # Center vertically

        draw.text((player_info_x, player_info_y_start), min_text, font=font_small, fill=(255, 255, 255)) # White for minimum
        draw.text((player_info_x, player_info_y_start + min_text_height + 5), gtotal_text, font=font_small, fill=(0, 255, 0)) # Green for Gtotal


        # Paste Player's Hand image
        combined_image.paste(player_hand_img, (player_img_x_offset, current_y_offset), player_hand_img)
        current_y_offset += player_hand_img.height + vertical_padding

        return combined_image

    async def _update_game_message(self, view: TexasHoldEmGameView, reveal_opponent: bool = False):
        """
        Updates the single game message for Texas Hold 'em with the combined image and view.
        This function is used for subsequent edits to the game message after the initial send.
        """
        player_kekchipz = await get_user_kekchipz(self.player.guild.id, self.player.id)
        combined_image_pil = await self._create_combined_holdem_image(
            self.player.display_name,
            self.bot_player.display_name,
            reveal_opponent=reveal_opponent
        )

        combined_image_bytes = io.BytesIO()
        combined_image_pil.save(combined_image_bytes, format='PNG')
        combined_image_bytes.seek(0)
        combined_file = discord.File(combined_image_bytes, filename="texas_holdem_game.png")

        # Message content now includes Kekchipz balance
        message_content = f"**{self.player.display_name}'s Kekchipz:** ${player_kekchipz}"

        # Append Serene's raise message if applicable
        if self.dealer_raise_amount > 0 and not reveal_opponent:
            message_content += f"\nSerene raises by ${self.dealer_raise_amount}! You must Call or Fold."


        if self.game_message:
            try:
                # Use 'attachments' keyword argument instead of 'files'
                await self.game_message.edit(content=message_content, view=view, attachments=[combined_file])
            except discord.errors.NotFound:
                print("WARNING: Game message not found during edit. This should not happen if initial message was sent correctly.")
            except Exception as e:
                print(f"WARNING: Error editing game message: {e}")
        else:
            print("ERROR: _update_game_message called but self.game_message is None. This function should only be called after initial message is sent.")


    async def _send_initial_game_message(self, interaction: discord.Interaction, view: TexasHoldEmGameView):
        """
        Sends the initial game message to the channel.
        This function is called once at the start of the game.
        """
        player_kekchipz = await get_user_kekchipz(self.player.guild.id, self.player.id)
        combined_image_pil = await self._create_combined_holdem_image(
            self.player.display_name,
            self.bot_player.display_name,
            reveal_opponent=False # Don't reveal opponent at start
        )

        combined_image_bytes = io.BytesIO()
        combined_image_pil.save(combined_image_bytes, format='PNG')
        combined_image_bytes.seek(0)
        combined_file = discord.File(combined_image_bytes, filename="texas_holdem_game.png")

        message_content = f"**{self.player.display_name}'s Kekchipz:** ${player_kekchipz}"
        
        # Use followup.send because the initial interaction has already been deferred
        self.game_message = await interaction.followup.send(content=message_content, view=view, files=[combined_file])


    async def start_game(self, interaction: discord.Interaction):
        """
        Starts the Texas Hold 'em game: shuffles, deals initial hands,
        and displays the initial state in a single message with a combined image.
        """
        random.shuffle(self.deck)
        self.deal_hole_cards() # Deal initial 2 cards to player and bot
        self.g_total = self.minimum_bet # Initial ante for Gtotal

        game_view = TexasHoldEmGameView(game=self, bot_instance=self.bot_instance) # View init will set pre_flop buttons
        
        # Send initial message and store its reference
        await self._send_initial_game_message(interaction, game_view)

        active_texasholdem_games[self.channel_id] = game_view


# --- Entry Point for game_main.py ---
async def start(interaction: discord.Interaction, bot_instance: commands.Bot):
    """
    This function serves as the entry point for the Texas Hold 'em game
    when called by game_main.py.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction immediately

    if interaction.channel.id in active_texasholdem_games:
        await interaction.followup.send(
            "A Texas Hold 'em game is already active in this channel! Please finish it or wait.",
            ephemeral=True
        )
        return
    
    await interaction.followup.send("Setting up Texas Hold 'em game...", ephemeral=True)
    
    # Pass the bot_instance to the TexasHoldEmGame constructor
    holdem_game = TexasHoldEmGame(interaction.channel.id, interaction.user, bot_instance)
    
    # Call the start_game method of the TexasHoldEmGame instance
    await holdem_game.start_game(interaction)

