import os
import urllib.parse
import json
import aiohttp
import discord
from discord.ext import commands

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

# --- Command Registration ---

async def start(interaction: discord.Interaction, bot: commands.Bot):
    serene_group = bot.tree.get_command("serene")

    if serene_group is None:
        print("Error: '/serene' command group not found. Cannot register 'story' command.")
        await interaction.followup.send("Bot setup error: '/serene' command group not found.", ephemeral=True)
        return

    @serene_group.command(name="story", description="Generate a story with surreal nouns and verbs.")
    async def story_command(interaction: discord.Interaction):
        await interaction.response.defer()

        php_backend_url = "https://serenekeks.com/serene_bot_2.php"
        player_name = interaction.user.display_name

        nouns = ["dragon", "wizard", "monster"]
        verbs = ["fly", "vanish"]

        php_structure = {
            "first": "There once was a ",
            "second": " who loved to ",
            "third": ". But then one night, there came a shock… for a ",
            "forth": " came barreling towards them before they ",
            "fifth": " and lived happily ever after."
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(php_backend_url) as response:
                    if response.status == 200:
                        php_structure = await response.json()
        except Exception as e:
            print(f"PHP fetch error: {e}")

        try:
            gemini_prompt = """
            Return a JSON object with:
            - 'nouns': 3 imaginative, simple lowercase nouns
            - 'verbs': 2 action verbs in BASE form (will be conjugated to past tense as needed)
            Output format: {"nouns": [...], "verbs": [...]}
            """

            payload = {
                "contents": [{"role": "user", "parts": [{"text": gemini_prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": {
                        "type": "OBJECT",
                        "properties": {
                            "nouns": {"type": "ARRAY", "items": {"type": "STRING"}},
                            "verbs": {"type": "ARRAY", "items": {"type": "STRING"}}
                        }
                    }
                }
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
                            parsed = json.loads(parts[0]["text"])
                            nouns = (parsed.get("nouns", nouns))[:3]
                            verbs = (parsed.get("verbs", verbs))[:2]
        except Exception as e:
            print(f"Gemini API error: {e}")

        v1 = verbs[0]
        v2 = to_past_tense(verbs[1])

        full_story = (
            php_structure["first"] + nouns[0] +
            php_structure["second"] + v1 +
            php_structure["third"] + nouns[1] +
            php_structure["forth"] + v2 +
            php_structure["fifth"]
        )

        await interaction.followup.send(
            f"**{player_name} asked for a story**\n"
            f"**Serene says:** {full_story}"
        )

    bot.tree.add_command(story_command)
    print("'/serene story' command registered.")
