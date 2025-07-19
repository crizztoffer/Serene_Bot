import os
import json
import aiohttp
import discord
from discord import app_commands

# --- Helper Function ---

def to_past_tense(verb):
    irregular_verbs = {
        "go": "went", "come": "came", "see": "saw", "say": "said", "make": "made",
        "take": "took", "know": "knew", "get": "got", "give": "gave", "find": "found",
        "think": "thought", "told": "told", "become": "became", "show": "showed",
        "leave": "left", "feel": "felt", "put": "put", "bring": "brought", "begin": "began",
        "run": "ran", "eat": "ate", "sing": "sang", "drink": "drank", "swim": "swam",
        "break": "broke", "choose": "chose", "drive": "drove", "fall": "fell", "fly": "flew",
        "forget": "forgot", "hold": "held", "read": "read", "ride": "rode", "speak": "spoke",
        "stand": "stood", "steal": "stole", "strike": "struck", "write": "wrote",
        "burst": "burst", "hit": "hit", "cut": "cut", "cost": "cost", "let": "let",
        "shut": "shut", "spread": "spread", "shit": "shit", "bust": "busted", "burp": "burped",
        "rocket": "rocketed", "cross": "crossed", "whisper": "whispered", "piss": "pissed",
        "flip": "flipped", "reverse": "reversed", "waffle-spank": "waffle-spanked",
        "kiss": "kissed", "spin": "spun", "vomit": "vomitted", "sand-blast": "sand-blasted",
        "slip": "slipped"
    }
    if verb in irregular_verbs:
        return irregular_verbs[verb]
    elif verb.endswith('e'):
        return verb + 'd'
    elif verb.endswith('y') and verb[-2] not in 'aeiou':
        return verb[:-1] + 'ied'
    else:
        return verb + 'ed'

# --- Slash Command ---

@app_commands.command(name="story", description="Generate a story with surreal nouns and verbs.")
async def command(interaction: discord.Interaction):
    await interaction.response.defer()

    php_backend_url = "https://serenekeks.com/serene_bot_2.php"
    player_name = interaction.user.display_name

    story_parts = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(php_backend_url) as response:
                if response.status == 200:
                    story_parts = await response.json()
    except Exception as e:
        print(f"PHP fetch error: {e}")
        await interaction.followup.send("Failed to fetch story structure.")
        return

    # Extract verb form hints to guide Gemini more intelligently
    verb_context = {
        key: value["verb_form"]
        for key, value in story_parts.items()
        if key in ["first", "second", "third", "forth", "fifth"]
    }

    # Ask Gemini for appropriate verbs/nouns based on the structure
    nouns = ["creature", "thing"]
    verbs = ["wiggle", "crash"]

    try:
        gemini_prompt = f"""
        Based on the following verb usage requirements, generate fitting noun and verb words.
        The goal is to complete a 5-part surreal story. For each entry in the input below, return a word that fits its verb_form:

        {json.dumps(verb_context, indent=2)}

        - If "verb_form" is "infinitive", provide a verb in base form (like "eat", "fly").
        - If "verb_form" is "past_tense", provide a verb in base form (we will convert it).
        - If "verb_form" is "none", provide a creative and unusual noun.

        Return a JSON object with keys matching the inputs and values being the words.
        Example: {{"first": "dragon", "second": "sing", "third": "monster", "forth": "dance", "fifth": "portal"}}
        """

        payload = {
            "contents": [{"role": "user", "parts": [{"text": gemini_prompt}]}]
        }

        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
                    headers={"Content-Type": "application/json"},
                    json=payload
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        parts = data["candidates"][0]["content"]["parts"]
                        gemini_fills = json.loads(parts[0]["text"])

                        # Fallback safety
                        nouns = [gemini_fills.get("first", "creature"), gemini_fills.get("third", "thing")]
                        verbs = [
                            gemini_fills.get("second", "run"),
                            gemini_fills.get("forth", "explode")
                        ]
    except Exception as e:
        print(f"Gemini API error: {e}")

    # Final verb handling
    v1 = verbs[0]
    v2 = to_past_tense(verbs[1])
    n1 = nouns[0]
    n2 = nouns[1]

    # Build the story
    full_story = (
        story_parts["first"]["sentence"] + n1 +
        story_parts["second"]["sentence"] + v1 +
        story_parts["third"]["sentence"] + n2 +
        story_parts["forth"]["sentence"] + v2 +
        story_parts["fifth"]["sentence"]
    )

    await interaction.followup.send(
        f"**{player_name} asked for a story**\n"
        f"**Serene says:** {full_story}"
    )
