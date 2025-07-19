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
    #     conn = await aiomysql.connect(...)
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
                        # Do not reset wager for other deletion errors either

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
                        game.current_wager = 0 # Wager 0 if timeout
                    except Exception as e:
                        print(f"Error getting Final Jeopardy wager: {e}")
                        await interaction.channel.send("An error occurred while getting your wager. Defaulting to $0.", delete_after=5)
                        game.current_wager = 0

                    # Present Final Jeopardy Question
                    final_question_data = game.final_jeopardy_data.get("final_jeopardy")
                    if final_question_data:
                        await interaction.channel.send(
                            f"Your wager: **${game.current_wager}**.\n\n"
                            f"**Final Jeopardy Category:** {final_question_data['category']}\n\n"
                            f"**The Clue:** {final_question_data['question']}"
                        )

                        def check_final_answer(m: discord.Message):
                            # No prefix required for Final Jeopardy answers
                            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

                        try:
                            final_user_answer_msg = await view.bot_instance.wait_for('message', check=check_final_answer, timeout=60.0) # Longer timeout for answer
                            final_user_raw_answer = final_user_answer_msg.content.lower().strip()

                            final_correct_answer_raw_lower = final_question_data['answer'].lower()
                            final_correct_answer_for_comparison = re.sub(r'\s*\(.*\)', '', final_correct_answer_raw_lower).strip()

                            final_is_correct = False
                            if final_user_raw_answer == final_correct_answer_for_comparison:
                                final_is_correct = True
                            else:
                                final_user_words = set(re.findall(r'\b\w+\b', final_user_raw_answer))
                                final_correct_words_full = set(re.findall(r'\b\w+\b', final_correct_answer_for_comparison))
                                
                                # For Final Jeopardy, all words in the correct answer are "significant"
                                final_significant_correct_words = list(final_correct_words_full) # Convert to list for iteration

                                for user_word in final_user_words:
                                    for sig_correct_word in final_significant_correct_words:
                                        similarity = calculate_word_similarity(user_word, sig_correct_word)
                                        if similarity >= 70.0:
                                            final_is_correct = True
                                            break
                                    if final_is_correct:
                                        break
                            
                            if final_is_correct:
                                game.score += game.current_wager
                                await interaction.channel.send(
                                    f"✅ Correct, {game.player.display_name}! You answered correctly and gained **${game.current_wager}**."
                                )
                            else:
                                game.score -= game.current_wager
                                await interaction.channel.send(
                                    f"❌ Incorrect, {game.player.display_name}! The correct answer was: "
                                    f"**__{final_question_data['answer']}__**. You lost **${game.current_wager}**."
                                )
                        except asyncio.TimeoutError:
                            await interaction.channel.send(
                                f"⏰ Time's up, {game.player.display_name}! You didn't answer in time for Final Jeopardy. "
                                f"The correct answer was: **__{final_question_data['answer']}__**."
                            )
                        except Exception as e:
                            print(f"Error waiting for Final Jeopardy answer: {e}")
                            await interaction.channel.send("An unexpected error occurred while waiting for your Final Jeopardy answer.")
                    else:
                        await interaction.channel.send("Could not load Final Jeopardy question data.")
                    
                    # End of Final Jeopardy
                    await interaction.channel.send(
                        f"Final Score for {game.player.display_name}: **{'-' if game.score < 0 else ''}${abs(game.score)}**.\n"
                        "Thank you for playing Jeopardy!"
                    )
                    # Add kekchipz based on final score if greater than 0
                    if game.score > 0:
                        await update_user_kekchipz(interaction.guild.id, interaction.user.id, game.score)

                    if game.channel_id in active_jeopardy_games:
                        del active_jeopardy_games[game.channel_id]
                    view.stop() # Stop the current view's timeout
                    return # Exit if Final Jeopardy is reached, as no more dropdowns are needed

                # Stop the current view before sending a new one
                view.stop()

                # Send a NEW message with the dropdowns for the next phase, or the current phase if not completed
                new_jeopardy_view = JeopardyGameView(game, view.bot_instance) # Pass bot_instance
                new_jeopardy_view.add_board_components() # Rebuilds the view with updated options (guessed questions removed)

                # Determine the content for the new board message based on the game phase
                board_message_content = ""
                if game.game_phase == "NORMAL_JEOPARDY":
                    board_message_content = (
                        f"**{game.player.display_name}**'s Score: **{'-' if game.score < 0 else ''}${abs(game.score)}**\n\n"
                        "Select a category and value from the dropdowns below!"
                    )
                elif game.game_phase == "DOUBLE_JEOPARDY":
                    board_message_content = (
                        f"**{game.player.display_name}**'s Score: **{'-' if game.score < 0 else ''}${abs(game.score)}**\n\n"
                        "**Double Jeopardy!** Select a category and value from the dropdowns below!"
                    )
                
                if board_message_content: # Only send if there's content (i.e., not Final Jeopardy yet)
                    game.board_message = await interaction.channel.send(
                        content=board_message_content,
                        view=new_jeopardy_view
                    )
                else:
                    # If we reached Final Jeopardy and no board message is sent, clean up view
                    if new_jeopardy_view.children: # If there are still components, disable them
                        for item in new_jeopardy_view.children:
                            item.disabled = True
                        await interaction.channel.send("Game concluded. No more questions.", view=new_jeopardy_view)
                    else:
                        await interaction.channel.send("Game concluded. No more questions.")

        else:
            # If for some reason the question is not found or already guessed (race condition)
            await interaction.response.send_message(
                f"Question '{self.category_name}' for ${selected_value} not found or already picked. Please select another.",
                ephemeral=True
            )


class JeopardyGameView(discord.ui.View):
    """The Discord UI View that holds the interactive Jeopardy board dropdowns."""
    def __init__(self, game: 'NewJeopardyGame', bot_instance: commands.Bot):
        # Increased timeout to 15 minutes (900 seconds)
        super().__init__(timeout=900)
        self.game = game # Reference to the NewJeopardyGame instance
        self.bot_instance = bot_instance # Store the bot instance
        self._selected_category = None # Stores the category selected by the user
        self._selected_value = None # Stores the value selected by the user

    def add_board_components(self):
        """
        Dynamically adds dropdowns (selects) for categories to the view.
        Each dropdown is placed on its own row, up to a maximum of 5 rows (0-4).
        """
        self.clear_items()  # Clear existing items before rebuilding the board

        # Determine which data set to use based on current game phase
        categories_to_process = []
        if self.game.game_phase == "NORMAL_JEOPARDY":
            categories_to_process = self.game.normal_jeopardy_data.get("normal_jeopardy", [])
        elif self.game.game_phase == "DOUBLE_JEOPARDY":
            categories_to_process = self.game.double_jeopardy_data.get("double_data", []) # Corrected key
        else:
            # No dropdowns for Final Jeopardy or other phases
            return

        # Iterate through categories and assign each to a new row, limiting to 5 rows for Discord UI
        for i, category_data in enumerate(categories_to_process):
            if i >= 5: # Discord UI has a maximum of 5 rows (0-4) for components
                break

            category_name = category_data["category"]
            options = [
                discord.SelectOption(label=f"${q['value']}", value=str(q['value']))
                for q in category_data["questions"] if not q["guessed"] # Only show unguessed questions
            ]

            if options: # Only add a dropdown if there are available questions in the category
                # Place each category's dropdown on its own row (i.e., row=0, row=1, row=2, etc.)
                self.add_item(CategoryValueSelect(
                    category_name,
                    options,
                    f"Pick for {category_name}",
                    row=i
                ))

    async def on_timeout(self):
        """Called when the view times out due to inactivity."""
        if self.game.board_message:
            try:
                # Added try-except for NotFound error
                await self.game.board_message.edit(content="Jeopardy game timed out due to inactivity.", view=None)
            except discord.errors.NotFound:
                print("WARNING: Board message not found during timeout, likely already deleted.")
            except Exception as e:
                print(f"WARNING: An error occurred editing board message on timeout: {e}")
        
        # Changed self.game.channel.id to self.game.channel_id
        if self.game.channel_id in active_jeopardy_games:
            # Clean up the game state
            del active_jeopardy_games[self.game.channel_id]
        print(f"Jeopardy game in channel {self.game.channel_id} timed out.")


# --- New Jeopardy Game Class ---
class NewJeopardyGame:
    """
    A class for the new Jeopardy game.
    Manages game state, player score, and Jeopardy data.
    """
    def __init__(self, channel_id: int, player: discord.User, bot_instance: commands.Bot):
        self.channel_id = channel_id
        self.player = player
        self.bot_instance = bot_instance # Store the bot instance
        self.score = 0 # Initialize player score
        self.normal_jeopardy_data = None
        self.double_jeopardy_data = None
        self.final_jeopardy_data = None
        self.jeopardy_data_url = "https://serenekeks.com/serene_bot_games.php"
        self.board_message = None # To store the message containing the board UI
        self.current_question = None # Stores the question currently being presented
        self.current_wager = 0 # Stores the wager for Daily Double/Final Jeopardy
        self.game_phase = "NORMAL_JEOPARDY" # Tracks the current phase of the game

    async def fetch_and_parse_jeopardy_data(self) -> bool:
        """
        Fetches the full Jeopardy JSON data from the backend URL.
        Parses the JSON and separates it into three distinct data structures:
        normal_jeopardy, double_jeopardy, and final_jeopardy, storing them
        as attributes of this class.
        Initializes 'guessed' status for all questions.
        Returns True if data is successfully fetched and parsed, False otherwise.
        """
        try:
            # Construct the URL with the 'jeopardy' parameter
            params = {"jeopardy": "true"}
            encoded_params = urllib.parse.urlencode(params)
            full_url = f"{self.jeopardy_data_url}?{encoded_params}"

            async with aiohttp.ClientSession() as session:
                async with session.get(full_url) as response:
                    if response.status == 200:
                        full_data = await response.json()
                        
                        # Initialize 'guessed' status for all questions and add category name
                        for category_type in ["normal_jeopardy", "double_jeopardy"]:
                            if category_type in full_data:
                                for category in full_data[category_type]:
                                    for question_data in category["questions"]:
                                        question_data["guessed"] = False
                                        question_data["category"] = category["category"] # Store category name in question
                        if "final_jeopardy" in full_data:
                            full_data["final_jeopardy"]["guessed"] = False
                            full_data["final_jeopardy"]["category"] = full_data["final_jeopardy"].get("category", "Final Jeopardy")

                        self.normal_jeopardy_data = {"normal_jeopardy": full_data.get("normal_jeopardy", [])}
                        self.double_jeopardy_data = {"double_data": full_data.get("double_jeopardy", [])} # Fixed typo here
                        self.final_jeopardy_data = {"final_jeopardy": full_data.get("final_jeopardy", {})}
                        
                        print(f"Jeopardy data fetched and parsed for channel {self.channel_id}")
                        return True
                    else:
                        print(f"Error fetching Jeopardy data: HTTP Status {response.status}")
                        return False
        except Exception as e:
            print(f"Error loading Jeopardy data: {e}")
            return False

    def is_all_questions_guessed(self, phase_type: str) -> bool:
        """
        Checks if all questions in a given phase (normal_jeopardy or double_jeopardy)
        have been guessed.
        """
        data_to_check = []
        if phase_type == "normal_jeopardy":
            data_to_check = self.normal_jeopardy_data.get("normal_jeopardy", [])
        elif phase_type == "double_jeopardy":
            data_to_check = self.double_jeopardy_data.get("double_data", []) # Corrected key
        else:
            return False # Invalid phase type

        if not data_to_check: # If there's no data for this phase, consider it "completed"
            return True

        for category in data_to_check:
            for question_data in category["questions"]:
                if not question_data["guessed"]:
                    return False # Found an unguessed question
        return True # All questions are guessed


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
    
    jeopardy_game = NewJeopardyGame(interaction.channel.id, interaction.user, bot_instance)
    
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
        await interaction.followup.send(
            "Failed to load Jeopardy game data. Please try again later.",
            ephemeral=True
        )
        return

