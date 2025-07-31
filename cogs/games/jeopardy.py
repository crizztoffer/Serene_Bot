import discord
from discord.ext import commands
import asyncio
import random
import io
import os # For environment variables like API keys
import urllib.parse # For URL encoding
import json # For parsing JSON data
import re # Import the re module for regular expressions
import aiohttp # For asynchronous HTTP requests
import aiomysql # Import aiomysql for database operations
import logging # Import logging

# Set up logging for this module
logger = logging.getLogger(__name__)

# --- Game State Storage ---
# This dictionary will store active Jeopardy games by channel ID.
active_jeopardy_games = {}

# --- Helper for fuzzy matching (MODIFIED to use Levenshtein distance) ---
def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculates the Levenshtein distance between two strings.
    This is the minimum number of single-character edits (insertions, deletions, or substitutions)
    required to change one word into the other.
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    # Initialize the first row of the distance matrix
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Calculate costs for insertion, deletion, and substitution
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2) # Cost is 0 if characters match, 1 otherwise
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def calculate_word_similarity(word1: str, word2: str) -> float:
    """
    Calculates a percentage of similarity between two words using Levenshtein distance.
    A higher percentage means more similarity.
    """
    word1_lower = word1.lower()
    word2_lower = word2.lower()

    max_len = max(len(word1_lower), len(word2_lower))
    if max_len == 0:
        return 100.0 # Both empty strings are 100% similar

    dist = levenshtein_distance(word1_lower, word2_lower)
    # Similarity is calculated as (max_length - distance) / max_length
    similarity_percentage = ((max_len - dist) / max_len) * 100.0
    return similarity_percentage

# --- Database Operations ---
async def update_user_kekchipz(guild_id: int, discord_id: int, amount: int, db_config: dict):
    """
    Updates a user's kekchipz balance in the database.
    Ensures the balance does not go below zero.
    """
    conn = None
    try:
        # Removed the 'db' parameter from the connection
        conn = await aiomysql.connect(
            host=db_config['DB_HOST'],
            user=db_config['DB_USER'],
            password=db_config['DB_PASSWORD'],
            autocommit=True
        )

        async with conn.cursor() as cursor:
            # First, check if the user exists
            await cursor.execute("SELECT kekchipz FROM discord_users WHERE guild_id = %s AND discord_id = %s", (str(guild_id), str(discord_id)))
            result = await cursor.fetchone()

            if result:
                current_kekchipz = result[0]
                new_kekchipz = max(0, current_kekchipz + amount) # Ensure balance doesn't go below zero
                await cursor.execute("UPDATE discord_users SET kekchipz = %s WHERE guild_id = %s AND discord_id = %s", (new_kekchipz, str(guild_id), str(discord_id)))
                logger.info(f"Updated user {discord_id} in guild {guild_id}: kekchipz changed by {amount}. New balance: {new_kekchipz}")
            else:
                # If the user doesn't exist, insert them with the given amount (if positive)
                initial_kekchipz = max(0, amount)
                await cursor.execute("INSERT INTO discord_users (guild_id, discord_id, kekchipz) VALUES (%s, %s, %s)", (str(guild_id), str(discord_id), initial_kekchipz))
                logger.info(f"Inserted new user {discord_id} in guild {guild_id} with initial kekchipz: {initial_kekchipz}")
    except Exception as e:
        logger.error(f"Database update failed for user {discord_id} in guild {guild_id}: {e}")
    finally:
        if conn:
            conn.close()

async def get_user_kekchipz(guild_id: int, discord_id: int, db_config: dict) -> int:
    """
    Fetches a user's kekchipz balance from the database.
    Returns 0 if the user is not found or an error occurs.
    """
    conn = None
    try:
        # Removed the 'db' parameter from the connection
        conn = await aiomysql.connect(
            host=db_config['DB_HOST'],
            user=db_config['DB_USER'],
            password=db_config['DB_PASSWORD'],
        )

        async with conn.cursor() as cursor:
            await cursor.execute("SELECT kekchipz FROM discord_users WHERE guild_id = %s AND discord_id = %s", (str(guild_id), str(discord_id)))
            result = await cursor.fetchone()
            if result:
                return result[0]
            else:
                # User not found, return a default of 0
                return 0
    except Exception as e:
        logger.error(f"Database fetch failed for user {discord_id} in guild {guild_id}: {e}")
        return 0
    finally:
        if conn:
            conn.close()


# --- Jeopardy Game UI Components ---

class CategoryValueSelect(discord.ui.Select):
    """A dropdown (select) for choosing a question's value within a specific category."""
    def __init__(self, category_name: str, options: list[discord.SelectOption], placeholder: str, row: int):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"jeopardy_select_{category_name.replace(' ', '_').lower()}_{row}", # Add row to custom_id for uniqueness
            row=row
        )
        self.category_name = category_name # Store category name for later use

    async def callback(self, interaction: discord.Interaction):
        """Handles a selection from the dropdown."""
        view: 'JeopardyGameView' = self.view
        game: 'NewJeopardyGame' = view.game

        # Ensure it's the active player's turn to select
        if interaction.user.id != game.player.id:
            await interaction.response.send_message("You are not the active player for this Jeopardy game.", ephemeral=True)
            return
        
        if game.current_question: # If a question is already being answered, prevent new selections
            await interaction.response.send_message("A question is currently active. Please wait for it to conclude.", ephemeral=True)
            return

        # Store the selected category and value in the view's state
        selected_value_str = self.values[0] # The selected value is always a string from SelectOption
        selected_value = int(selected_value_str) # Convert back to int

        # Find the actual question data
        question_data = None
        
        # Determine which data set to search based on current game phase
        categories_to_search = []
        if game.game_phase == "NORMAL_JEOPARDY":
            categories_to_search = game.normal_jeopardy_data.get("normal_jeopardy", [])
        elif game.game_phase == "DOUBLE_JEOPARDY":
            categories_to_search = game.double_jeopardy_data.get("double_data", []) # Corrected key
        
        for cat_data in categories_to_search:
            if cat_data["category"] == self.category_name:
                for q_data in cat_data["questions"]:
                    if q_data["value"] == selected_value and not q_data["guessed"]:
                        question_data = q_data
                        break
                if question_data:
                    break
        
        if question_data:
            # Respond immediately to the interaction to acknowledge the selection
            # This is crucial to avoid "Unknown interaction" errors.
            await interaction.response.send_message(
                f"**{game.player.display_name}** selected **{question_data['category']}** for **${question_data['value']}**.\n\n"
                "*Processing your selection...*",
                ephemeral=True # Make this initial response ephemeral
            )

            # Mark the question as guessed
            question_data["guessed"] = True
            game.current_question = question_data # Set current question in game state

            # Clear the view's internal selection state (not strictly necessary but good practice)
            view._selected_category = None
            view._selected_value = None

            # Delete the original board message that contained the dropdowns
            if game.board_message:
                try:
                    await game.board_message.delete()
                    game.board_message = None # Clear reference after deletion
                except discord.errors.NotFound:
                    print("WARNING: Original board message not found (already deleted or inaccessible).")
                    game.board_message = None
                except discord.errors.Forbidden:
                    print("WARNING: Missing permissions to delete the original board message. Please ensure the bot has 'Manage Messages' permission.")
                    # Keep game.board_message as is if deletion fails due to permissions,
                    # as it might still be visible but uneditable.
                except Exception as delete_e:
                    print(f"WARNING: An unexpected error occurred during original board message deletion: {delete_e}")
                    game.board_message = None # Assume it's gone or broken
            
            # --- Determine the correct prefix using Gemini ---
            determined_prefix = "What is" # Default fallback
            api_key = os.getenv('GEMINI_API_KEY')
            if api_key:
                try:
                    # Prompt Gemini to determine the single most appropriate prefix
                    gemini_prompt = f"Given the answer '{question_data['answer']}', what is the single most grammatically appropriate prefix (e.g., 'What is', 'Who is', 'What are', 'Who are', 'What was', 'Who was', 'What were', 'Who were') that would precede it in a Jeopardy-style question? Provide only the prefix string, exactly as it should be used (e.g., 'Who is', 'What were')."
                    chat_history = [{"role": "user", "parts": [{"text": gemini_prompt}]}]
                    payload = {"contents": chat_history}
                    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, headers={'Content-Type': 'application/json'}, json=payload) as response:
                            if response.status == 200:
                                gemini_result = await response.json()
                                if gemini_result.get("candidates") and len(gemini_result["candidates"]) > 0 and \
                                   gemini_result["candidates"][0].get("content") and \
                                   gemini_result["candidates"][0]["content"].get("parts") and \
                                   len(gemini_result["candidates"][0]["content"]["parts"]) > 0:
                                    
                                    generated_text = gemini_result["candidates"][0]["content"]["parts"][0]["text"].strip()
                                    # Basic validation to ensure it's one of the expected prefixes
                                    valid_prefixes = ("what is", "who is", "what are", "who are", "what was", "who was", "what were", "who were")
                                    if generated_text.lower() in valid_prefixes:
                                        determined_prefix = generated_text
                                    else:
                                        print(f"Gemini returned unexpected prefix: '{generated_text}'. Using default.")
                                else:
                                    print("Gemini response structure unexpected for prefix determination. Using default.")
                            else:
                                print(f"Gemini API call failed for prefix determination with status {response.status}. Using default.")
                except Exception as e:
                    print(f"Error calling Gemini API for prefix determination: {e}. Using default.")
            else:
                print("GEMINI_API_KEY not set. Cannot determine dynamic prefixes. Using default.")

            # --- Daily Double Wager Logic ---
            is_daily_double = question_data.get("daily_double", False) # Corrected key name
            
            # Initialize game.current_wager with the question's value by default
            game.current_wager = question_data['value'] 

            if is_daily_double:
                # Send the initial Daily Double message using followup.send
                await interaction.followup.send(
                    f"**DAILY DOUBLE!** {game.player.display_name}, you found the Daily Double!\n"
                    f"Your current score is **{'-' if game.score < 0 else ''}${abs(game.score)}**." # Format negative score
                )

                max_wager = max(2000, game.score) if game.score >= 0 else 2000
                print(f"DEBUG: Player score: {game.score}, Calculated max_wager: {max_wager}") # DEBUG
                
                wager_prompt_message = await interaction.channel.send(
                    f"{game.player.display_name}, please enter your wager. "
                    f"You can wager any amount up to **${max_wager}** (must be positive)."
                )

                def check_wager(m: discord.Message):
                    return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

                try:
                    wager_msg = await view.bot_instance.wait_for('message', check=check_wager, timeout=30.0)
                    wager_input = int(wager_msg.content)
                    print(f"DEBUG: User entered wager: {wager_input}") # DEBUG

                    if wager_input <= 0:
                        await interaction.channel.send("Your wager must be a positive amount. Defaulting to $500.", delete_after=5)
                        game.current_wager = 500
                        print("DEBUG: Wager defaulted to 500 (<=0)") # DEBUG
                    elif wager_input > max_wager:
                        await interaction.channel.send(f"Your wager exceeds the maximum allowed (${max_wager}). Defaulting to max wager.", delete_after=5)
                        game.current_wager = max_wager
                        print(f"DEBUG: Wager defaulted to max_wager ({max_wager})") # DEBUG
                    else:
                        game.current_wager = wager_input
                        print(f"DEBUG: Wager set to user input: {game.current_wager}") # DEBUG
                    
                    # Attempt to delete messages, but handle potential errors gracefully
                    try:
                        await wager_prompt_message.delete()
                        await wager_msg.delete()
                    except discord.errors.Forbidden:
                        print("WARNING: Missing permissions to delete wager messages. Please ensure the bot has 'Manage Messages' permission.")
                        # Do not reset wager if deletion fails due to permissions
                    except Exception as delete_e:
                        print(f"WARNING: An unexpected error occurred during message deletion: {delete_e}")
                        game.current_wager = 500
                except asyncio.TimeoutError:
                    print("DEBUG: Wager input timed out.") # DEBUG
                    await interaction.channel.send("Time's up! You didn't enter a wager. Defaulting to $500.", delete_after=5)
                    game.current_wager = 500
                except Exception as e:
                    # This block now only catches errors *during bot.wait_for* or initial processing of wager_input
                    print(f"DEBUG: Error getting wager (before deletion attempt): {e}") # DEBUG
                    await interaction.channel.send("An error occurred while getting your wager. Defaulting to $500.", delete_after=5)
                    game.current_wager = 500
                
                print(f"DEBUG: Final game.current_wager before sending question: {game.current_wager}") # DEBUG
                # Now send the question for Daily Double, reflecting the wager
                await interaction.followup.send(
                    f"You wagered **${game.current_wager}**.\n*For the Daily Double:*\n**{question_data['question']}**"
                )
            else: # Not a Daily Double, proceed as before
                # The wager is already set to question_data['value']
                await interaction.followup.send(
                    f"*For ${question_data['value']}:*\n**{question_data['question']}**"
                )


            # Define a list of valid Jeopardy prefixes for user answers
            valid_user_prefixes = (
                "what is", "who is", "what are", "who are",
                "what was", "who was", "what were", "who were"
            )

            def check_answer(m: discord.Message):
                # Check if message is in the same channel, from the same user
                if not (m.channel.id == interaction.channel.id and m.author.id == interaction.user.id):
                    return False
                
                # Check if the message content starts with any of the valid Jeopardy prefixes
                msg_content_lower = m.content.lower()
                for prefix in valid_user_prefixes:
                    if msg_content_lower.startswith(prefix):
                        return True
                return False

            try:
                # Wait for the user's response for a limited time (e.g., 30 seconds)
                user_answer_msg = await view.bot_instance.wait_for('message', check=check_answer, timeout=30.0)
                user_raw_answer = user_answer_msg.content.lower()

                # Determine which prefix was used and strip it
                matched_prefix_len = 0
                for prefix in valid_user_prefixes:
                    if user_raw_answer.startswith(prefix):
                        matched_prefix_len = len(prefix)
                        break # Take the first match (order in tuple matters if there are overlaps, but for these prefixes, it's fine)
                
                processed_user_answer = user_raw_answer[matched_prefix_len:].strip()
                
                correct_answer_raw_lower = question_data['answer'].lower()
                # Remove text in parentheses from the correct answer for direct comparison
                correct_answer_for_comparison = re.sub(r'\s*\(.*\)', '', correct_answer_raw_lower).strip()

                is_correct = False
                # Check for exact match first (after stripping prefix and parentheses from correct answer)
                if processed_user_answer == correct_answer_for_comparison:
                    is_correct = True
                else:
                    # Tokenize answers and question for word-by-word comparison
                    # Remove punctuation from words before tokenizing
                    user_words = set(re.findall(r'\b\w+\b', processed_user_answer))
                    correct_words_full = set(re.findall(r'\b\w+\b', correct_answer_for_comparison))
                    question_words = set(re.findall(r'\b\w+\b', question_data['question'].lower()))

                    # Filter correct words: keep only those NOT in the question
                    # This creates a list of 'significant' words from the correct answer
                    significant_correct_words = [word for word in correct_words_full if word not in question_words]

                    # If the user's answer is a single word and it's an exact match for a significant correct word
                    if len(user_words) == 1 and list(user_words)[0] in significant_correct_words:
                        is_correct = True
                    else:
                        # Perform fuzzy matching for each user word against significant correct words
                        for user_word in user_words:
                            for sig_correct_word in significant_correct_words:
                                similarity = calculate_word_similarity(user_word, sig_correct_word)
                                if similarity >= 70.0:
                                    is_correct = True
                                    break
                            if is_correct:
                                break
                
                # Compare the processed user answer with the correct answer
                if is_correct:
                    game.score += game.current_wager # Use wager for score
                    await interaction.followup.send(
                        f"✅ Correct, {game.player.display_name}! Your score is now **{'-' if game.score < 0 else ''}${abs(game.score)}**."
                    )
                else:
                    game.score -= game.current_wager # Use wager for score
                    # Removed spoiler tags, added quotes, and ensured full answer is bold/underlined
                    full_correct_answer = f'"{determined_prefix} {question_data["answer"]}"'.strip()
                    await interaction.followup.send(
                        f"❌ Incorrect, {game.player.display_name}! The correct answer was: "
                        f"**__{full_correct_answer}__**. Your score is now **{'-' if game.score < 0 else ''}${abs(game.score)}**."
                    )
            except asyncio.TimeoutError:
                # No score change for timeout
                full_correct_answer = f'"{determined_prefix} {question_data["answer"]}"'.strip()
                await interaction.followup.send(
                    f"⏰ Time's up, {game.player.display_name}! You didn't answer in time for '${question_data['value']}' question. The correct answer was: "
                    f"**__{full_correct_answer}__**."
                )
            except Exception as e:
                print(f"Error waiting for answer: {e}")
                await interaction.followup.send("An unexpected error occurred while waiting for your answer.")
            finally:
                game.current_question = None # Clear current question state
                game.current_wager = 0 # Reset wager
                # Check if all questions in the current phase are guessed
                current_phase_completed = False
                if game.game_phase == "NORMAL_JEOPARDY" and game.is_all_questions_guessed("normal_jeopardy"):
                    current_phase_completed = True
                    game.game_phase = "DOUBLE_JEOPARDY"
                    await interaction.channel.send(f"**Double Jeopardy!** All normal jeopardy questions have been answered. Get ready for new challenges, {game.player.display_name}!")
                elif game.game_phase == "DOUBLE_JEOPARDY" and game.is_all_questions_guessed("double_jeopardy"):
                    current_phase_completed = True
                    # --- Final Jeopardy Logic ---
                    if game.score <= 0:
                        await interaction.channel.send(
                            f"Thank you for playing Jeopardy, {game.player.display_name}! "
                            f"Your balance is **${game.score}**, and so here's where your game ends. "
                            "We hope to see you in Final Jeopardy very soon!"
                        )
                        if game.channel_id in active_jeopardy_games:
                            del active_jeopardy_games[game.channel_id]
                        view.stop() # Stop the current view's timeout
                        return # End the game here
                    # If player has positive earnings, proceed to Final Jeopardy
                    game.game_phase = "FINAL_JEOPARDY"
                    await interaction.channel.send(f"**Final Jeopardy!** All double jeopardy questions have been answered. Get ready for the final round, {game.player.display_name}!")
                    # Final Jeopardy Wager
                    final_max_wager = max(2000, game.score)
                    wager_prompt_message = await interaction.channel.send(
                        f"{game.player.display_name}, your current score is **{'-' if game.score < 0 else ''}${abs(game.score)}**. "
                        f"Please enter your Final Jeopardy wager. You can wager any amount up to **${final_max_wager}** (must be positive)."
                    )
                    def check_final_wager(m: discord.Message):
                        return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

                    try:
                        final_wager_msg = await view.bot_instance.wait_for('message', check=check_final_wager, timeout=60.0) # Longer timeout for wager
                        final_wager_input = int(final_wager_msg.content)
                        if final_wager_input <= 0:
                            await interaction.channel.send("Your wager must be a positive amount. Defaulting to $1.", delete_after=5)
                            game.current_wager = 1
                        elif final_wager_input > final_max_wager:
                            await interaction.channel.send(f"Your wager exceeds the maximum allowed (${final_max_wager}). Defaulting to max wager.", delete_after=5)
                            game.current_wager = final_max_wager
                        else:
                            game.current_wager = final_wager_input
                        try:
                            await wager_prompt_message.delete()
                            await final_wager_msg.delete()
                        except discord.errors.Forbidden:
                            print("WARNING: Missing permissions to delete wager messages.")
                        except Exception as delete_e:
                            print(f"WARNING: An unexpected error occurred during message deletion: {delete_e}")
                    except asyncio.TimeoutError:
                        await interaction.channel.send("Time's up! You didn't enter a wager. Defaulting to $0.", delete_after=5)
                        game.current_wager = 0
                    except Exception as e:
                        print(f"Error getting final wager: {e}")
                        await interaction.channel.send("An error occurred while getting your final wager. Defaulting to $0.", delete_after=5)
                        game.current_wager = 0
                    
                    # Send Final Jeopardy question
                    final_jeopardy_question = game.final_jeopardy_data.get("final_question")
                    if final_jeopardy_question:
                        # Attempt to get a dynamic prefix for the Final Jeopardy answer
                        final_determined_prefix = "What is"
                        api_key = os.getenv('GEMINI_API_KEY')
                        if api_key:
                            try:
                                gemini_prompt = f"Given the answer '{final_jeopardy_question['answer']}', what is the single most grammatically appropriate prefix (e.g., 'What is', 'Who is', 'What are', 'Who are', 'What was', 'Who was', 'What were', 'Who were') that would precede it in a Jeopardy-style question? Provide only the prefix string, exactly as it should be used (e.g., 'Who is', 'What were')."
                                chat_history = [{"role": "user", "parts": [{"text": gemini_prompt}]}]
                                payload = {"contents": chat_history}
                                api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

                                async with aiohttp.ClientSession() as session:
                                    async with session.post(api_url, headers={'Content-Type': 'application/json'}, json=payload) as response:
                                        if response.status == 200:
                                            gemini_result = await response.json()
                                            if gemini_result.get("candidates") and len(gemini_result["candidates"]) > 0 and \
                                               gemini_result["candidates"][0].get("content") and \
                                               gemini_result["candidates"][0]["content"].get("parts") and \
                                               len(gemini_result["candidates"][0]["content"]["parts"]) > 0:
                                                
                                                generated_text = gemini_result["candidates"][0]["content"]["parts"][0]["text"].strip()
                                                valid_prefixes = ("what is", "who is", "what are", "who are", "what was", "who was", "what were", "who were")
                                                if generated_text.lower() in valid_prefixes:
                                                    final_determined_prefix = generated_text
                            except Exception as e:
                                print(f"Error calling Gemini API for final jeopardy prefix determination: {e}. Using default.")
                        
                        await interaction.channel.send(
                            f"**Final Jeopardy Category:** {final_jeopardy_question['category']}\n\n"
                            f"You wagered **${game.current_wager}**.\n"
                            f"**__{final_jeopardy_question['question']}__**"
                        )
                        
                        def check_final_answer(m: discord.Message):
                            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.lower().startswith(final_determined_prefix.lower())

                        try:
                            final_answer_msg = await view.bot_instance.wait_for('message', check=check_final_answer, timeout=60.0) # Longer timeout for answer
                            final_raw_answer = final_answer_msg.content.lower()
                            
                            processed_final_answer = final_raw_answer[len(final_determined_prefix):].strip()
                            correct_final_answer = final_jeopardy_question['answer'].lower().strip()
                            
                            if processed_final_answer == correct_final_answer:
                                game.score += game.current_wager
                                await interaction.channel.send(
                                    f"✅ Correct, {game.player.display_name}! Your final score is **{'-' if game.score < 0 else ''}${abs(game.score)}**."
                                )
                                # Add the update to the database here
                                await update_user_kekchipz(
                                    guild_id=interaction.guild.id,
                                    discord_id=interaction.user.id,
                                    amount=game.current_wager,
                                    db_config=game.db_config
                                )
                            else:
                                game.score -= game.current_wager
                                final_correct_answer_full = f'"{final_determined_prefix} {final_jeopardy_question["answer"]}"'.strip()
                                await interaction.channel.send(
                                    f"❌ Incorrect, {game.player.display_name}! The correct answer was: "
                                    f"**__{final_correct_answer_full}__**. Your final score is **{'-' if game.score < 0 else ''}${abs(game.score)}**."
                                )
                        except asyncio.TimeoutError:
                            game.score -= game.current_wager # No score change on timeout
                            final_correct_answer_full = f'"{final_determined_prefix} {final_jeopardy_question["answer"]}"'.strip()
                            await interaction.channel.send(
                                f"⏰ Time's up! You didn't answer in time. The correct answer was: "
                                f"**__{final_correct_answer_full}__**. Your final score is **{'-' if game.score < 0 else ''}${abs(game.score)}**."
                            )
                        
                    # End the Final Jeopardy Game
                    await interaction.channel.send(f"Thank you for playing Jeopardy, {game.player.display_name}! The game is now over.")
                    if game.channel_id in active_jeopardy_games:
                        del active_jeopardy_games[game.channel_id]
                    view.stop()
                    return


            # If the current phase is not completed, rebuild the board
            if not current_phase_completed:
                await view.rebuild_board(interaction)


class JeopardyGameView(discord.ui.View):
    """
    The main view for the Jeopardy game, holding the category/value selectors.
    It dynamically updates the board based on answered questions.
    """
    def __init__(self, game: 'NewJeopardyGame', bot_instance: commands.Bot):
        super().__init__(timeout=1800) # 30 minutes timeout
        self.game = game
        self.bot_instance = bot_instance
        self._selected_category = None
        self._selected_value = None

    def add_board_components(self):
        """Adds the category and value dropdowns to the view."""
        self.clear_items()
        
        # Determine which data set to use based on game phase
        categories_to_show = []
        if self.game.game_phase == "NORMAL_JEOPARDY":
            categories_to_show = self.game.normal_jeopardy_data.get("normal_jeopardy", [])
        elif self.game.game_phase == "DOUBLE_JEOPARDY":
            categories_to_show = self.game.double_jeopardy_data.get("double_data", [])

        # Create dropdowns for each category
        for i, category_data in enumerate(categories_to_show):
            options = []
            for q_data in category_data["questions"]:
                if not q_data["guessed"]:
                    options.append(discord.SelectOption(label=f"${q_data['value']}", value=str(q_data['value'])))

            # Only add a dropdown if there are available questions in the category
            if options:
                # Add a dropdown for the category's values
                self.add_item(CategoryValueSelect(
                    category_name=category_data["category"],
                    options=options,
                    placeholder=category_data["category"],
                    row=i # Use row for layout
                ))

    async def rebuild_board(self, interaction: discord.Interaction):
        """Rebuilds the board and sends a new message with the updated view."""
        # Acknowledge the interaction first to prevent "Unknown interaction"
        await interaction.response.send_message("The game board is being updated...", ephemeral=True)
        
        self.add_board_components() # Re-create the dropdowns

        # Construct the new board message content
        message_content = f"**{self.game.player.display_name}**'s Score: **{'-' if self.game.score < 0 else ''}${abs(self.game.score)}**\n\n"
        if not self.game.is_game_over():
            message_content += "Select a category and value from the dropdowns below!"
        else:
            message_content += "The game has ended."
            self.stop() # Stop the view since the game is over

        # Send the new board message, replacing the old one
        try:
            if self.game.board_message:
                await self.game.board_message.delete()
                self.game.board_message = None
        except discord.errors.NotFound:
            print("WARNING: Old board message not found during rebuild.")
        except discord.errors.Forbidden:
            print("WARNING: Missing permissions to delete the old board message.")
        
        # Send the new board message and store its reference
        if not self.game.is_game_over():
            new_board_message = await interaction.channel.send(
                content=message_content,
                view=self
            )
            self.game.board_message = new_board_message
        else:
            # If the game is over, we don't need a view. Just send the final message.
            await interaction.channel.send(content=message_content)
        
        # Clear the ephemeral response
        try:
            await interaction.delete_original_response()
        except Exception as e:
            print(f"Error deleting ephemeral response: {e}")

    async def on_timeout(self) -> None:
        """Called when the view times out."""
        # End the game gracefully
        channel = self.game.channel_id
        if channel in active_jeopardy_games:
            del active_jeopardy_games[channel]
            await self.game.board_message.edit(content="Jeopardy game timed out.", view=None)


class NewJeopardyGame:
    """
    Represents a single instance of a Jeopardy game.
    Manages game state, scoring, and data.
    """
    def __init__(self, channel_id: int, player: discord.Member, bot_instance: commands.Bot, db_config: dict):
        self.channel_id = channel_id
        self.player = player
        self.score = 0
        self.normal_jeopardy_data = {} # Holds normal jeopardy questions
        self.double_jeopardy_data = {} # Holds double jeopardy questions
        self.final_jeopardy_data = {} # Holds final jeopardy question
        self.current_question = None
        self.current_wager = 0 # Stores the current question's value or a daily double wager
        self.bot_instance = bot_instance
        self.game_phase = "NORMAL_JEOPARDY"
        self.board_message = None # Store the message with the board view
        self.db_config = db_config # Store database configuration

    def is_all_questions_guessed(self, game_phase: str) -> bool:
        """Checks if all questions in a given game phase have been guessed."""
        questions_data = []
        if game_phase == "normal_jeopardy":
            questions_data = self.normal_jeopardy_data.get("normal_jeopardy", [])
        elif game_phase == "double_jeopardy":
            questions_data = self.double_jeopardy_data.get("double_data", [])

        if not questions_data:
            return True # No questions to guess, so it's "completed"
        
        for category in questions_data:
            for question in category["questions"]:
                if not question.get("guessed", False):
                    return False
        return True

    def is_game_over(self):
        """Checks if all phases are complete."""
        return self.is_all_questions_guessed("normal_jeopardy") and self.is_all_questions_guessed("double_jeopardy")

    async def fetch_and_parse_jeopardy_data(self):
        """
        Fetches Jeopardy data from a remote API and parses it into game state.
        This function now uses aiohttp for asynchronous fetching.
        It also handles parsing of JSON and random selection of categories.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Use a specific URL that provides a good mix of jeopardy questions
                # This example uses a mock API endpoint, replace with a real one
                # if available.
                api_url = "https://example-jeopardy-api.com/random?count=100" # Placeholder URL

                async with session.get(api_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Randomly select a set of categories and questions
                        # For simplicity, we'll assume the API returns enough data.
                        
                        # Set up Normal Jeopardy
                        normal_categories = random.sample(data, 5) # Get 5 random categories
                        self.normal_jeopardy_data["normal_jeopardy"] = []
                        for cat in normal_categories:
                            questions_in_cat = [
                                {"question": q["question"], "answer": q["answer"], "value": q["value"], "guessed": False}
                                for q in cat["questions"]
                            ]
                            # Make sure there are at least 5 questions of increasing value
                            # This is a simplification; a real API would provide this structure.
                            questions_in_cat.sort(key=lambda x: x['value'])
                            self.normal_jeopardy_data["normal_jeopardy"].append({
                                "category": cat["category"],
                                "questions": questions_in_cat
                            })
                            # Randomly assign a Daily Double to one normal jeopardy question
                            random_cat_index = random.randint(0, len(self.normal_jeopardy_data["normal_jeopardy"]) - 1)
                            random_q_index = random.randint(0, len(self.normal_jeopardy_data["normal_jeopardy"][random_cat_index]["questions"]) - 1)
                            self.normal_jeopardy_data["normal_jeopardy"][random_cat_index]["questions"][random_q_index]["daily_double"] = True

                        # Set up Double Jeopardy
                        double_categories = random.sample(data, 5) # Get 5 more random categories
                        self.double_jeopardy_data["double_data"] = []
                        for cat in double_categories:
                            questions_in_cat = [
                                {"question": q["question"], "answer": q["answer"], "value": q["value"] * 2, "guessed": False}
                                for q in cat["questions"]
                            ]
                            questions_in_cat.sort(key=lambda x: x['value'])
                            self.double_jeopardy_data["double_data"].append({
                                "category": cat["category"],
                                "questions": questions_in_cat
                            })
                            # Randomly assign two Daily Doubles to double jeopardy questions
                            random_cat_index_1 = random.randint(0, len(self.double_jeopardy_data["double_data"]) - 1)
                            random_q_index_1 = random.randint(0, len(self.double_jeopardy_data["double_data"][random_cat_index_1]["questions"]) - 1)
                            self.double_jeopardy_data["double_data"][random_cat_index_1]["questions"][random_q_index_1]["daily_double"] = True

                            random_cat_index_2 = random.randint(0, len(self.double_jeopardy_data["double_data"]) - 1)
                            random_q_index_2 = random.randint(0, len(self.double_jeopardy_data["double_data"][random_cat_index_2]["questions"]) - 1)
                            self.double_jeopardy_data["double_data"][random_cat_index_2]["questions"][random_q_index_2]["daily_double"] = True

                        # Set up Final Jeopardy
                        final_category = random.choice(data)
                        final_question = random.choice(final_category["questions"])
                        self.final_jeopardy_data["final_question"] = {
                            "category": final_category["category"],
                            "question": final_question["question"],
                            "answer": final_question["answer"]
                        }

                        return True
                    else:
                        print(f"API call failed with status: {response.status}")
                        return False
        except Exception as e:
            print(f"An error occurred while fetching Jeopardy data: {e}")
            return False


# --- Entry Point for game_main.py ---
async def start(interaction: discord.Interaction, bot_instance: commands.Bot):
    """
    This function serves as the entry point for the Jeopardy game
    when called by game_main.py.
    """
    await interaction.response.defer(ephemeral=True) # Acknowledge the interaction

    if interaction.channel.id in active_jeopardy_games:
        await interaction.followup.send(
            "A Jeopardy game is already active in this channel! Please finish it or wait.",
            ephemeral=True
        )
        return
    
    await interaction.followup.send("Setting up Jeopardy game...", ephemeral=True)

    # Load database configuration from environment variables
    # The 'DB_NAME' key has been removed as it is not used in the connection.
    db_config = {
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_USER": os.getenv("DB_USER"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
    }
    
    jeopardy_game = NewJeopardyGame(interaction.channel.id, interaction.user, bot_instance, db_config)
    
    success = await jeopardy_game.fetch_and_parse_jeopardy_data()

    if success:
        active_jeopardy_games[interaction.channel.id] = jeopardy_game
        
        jeopardy_view = JeopardyGameView(jeopardy_game, bot_instance) # Pass bot_instance
        jeopardy_view.add_board_components()
        
        game_message = await interaction.channel.send(
            content=(
                f"**{jeopardy_game.player.display_name}**'s Score: **{'-' if jeopardy_game.score < 0 else ''}${abs(jeopardy_game.score)}**\n\n"
                "Select a category and value from the dropdowns below!"
            ),
            view=jeopardy_view
        )
        jeopardy_game.board_message = game_message

    else:
        await interaction.followup.send("Failed to start the Jeopardy game. Please try again later.", ephemeral=True)
