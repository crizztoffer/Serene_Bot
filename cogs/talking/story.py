import os
import json
import aiohttp
import discord
from discord import app_commands
import random

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

    # Extract verb form hints to guide Gemini
    verb_context = {
        key: value["verb_form"]
        for key, value in story_parts.items()
        if key in ["first", "second", "third", "forth", "fifth"]
    }

    # Prompt Gemini
    gemini_fills = {}
    try:
        gemini_prompt = f"""
        You are crafting a surreal, humorous short story with five parts.

        Each entry below has a required word type:
        - "infinitive": a present tense verb (e.g., "explode", "scream").
        - "past_tense": also return a present tense verb (we convert it).
        - "none": provide a strange or unusual noun.

        All words MUST be creative, unexpected, and **different from each other**.

        Input:
        {json.dumps(verb_context, indent=2)}

        Return a JSON object like:
        {{
            "first": "lizard-witch",
            "second": "disintegrate",
            "third": "sky-yogurt",
            "forth": "giggle",
            "fifth": "portal-taco"
        }}
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

                        # Check for duplicates and warn
                        if len(set(gemini_fills.values())) < 5:
                            print("Duplicate words detected in Gemini response.")

    except Exception as e:
        print(f"Gemini API error: {e}")

    # Default fallbacks if Gemini fails
    default_nouns = ["pickle-satyr", "orb-crab", "lava-toaster", "slug-pyramid"]
    default_verbs = ["vibrate", "launch", "implode", "hiccup"]

    # Fill values with fallbacks if needed
    n1 = gemini_fills.get("first", random.choice(default_nouns))
    v1 = gemini_fills.get("second", random.choice(default_verbs))
    n2 = gemini_fills.get("third", random.choice(default_nouns))
    v2_raw = gemini_fills.get("forth", random.choice(default_verbs))
    v2 = to_past_tense(v2_raw)
    n3 = gemini_fills.get("fifth", random.choice(default_nouns))

    # Build the story
    full_story = (
        story_parts["first"]["sentence"] + n1 +
        story_parts["second"]["sentence"] + v1 +
        story_parts["third"]["sentence"] + n2 +
        story_parts["forth"]["sentence"] + v2 +
        story_parts["fifth"]["sentence"] + n3
    )

    await interaction.followup.send(
        f"**{player_name} asked for a story**\n"
        f"**Serene says:** {full_story}"
    )
