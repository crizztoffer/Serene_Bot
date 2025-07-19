# cogs/games/blackjack.py

import discord
from discord import ui
import asyncio, random, io, os, aiohttp
from PIL import Image, ImageDraw, ImageFont
from .blackjack_card_images import create_card_combo_image  # assuming you keep your card-image logic in this helper file

active_blackjack_games = {}

async def update_user_kekchipz(guild_id: int, discord_id: int, amount: int):
    print(f"Simulating update: User {discord_id} in guild {guild_id} kekchipz changed by {amount}.")

async def get_user_kekchipz(guild_id: int, discord_id: int) -> int:
    print(f"Simulating fetch: User {discord_id} kekchipz.")
    return 1000  # placeholder balance

class BlackjackGame:
    def __init__(self, channel_id: int, player: discord.User):
        self.channel_id = channel_id
        self.player = player
        self.reset_game()

    def reset_game(self):
        self.deck = self._create_standard_deck()
        random.shuffle(self.deck)
        self.player_hand = [self.deal_card(), self.deal_card()]
        self.dealer_hand = [self.deal_card(), self.deal_card()]

    def _create_standard_deck(self):
        suits = ['S','D','C','H']
        ranks = {'A':1,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'0':10,'J':10,'Q':10,'K':10}
        deck = []
        for s in suits:
            for r,n in ranks.items():
                card = {'code': r+s, 'cardNumber': n, 'title': f"{r}{s}"}
                deck.append(card)
        return deck

    def deal_card(self):
        return self.deck.pop()

    def calculate_hand_value(self, hand):
        value, aces = 0, 0
        for c in hand:
            v = c['cardNumber']
            if v == 1:
                aces += 1
                v = 11
            value += v
        while value > 21 and aces:
            value -= 10
            aces -= 1
        return value

    async def _create_game_embed_with_images(self, reveal_dealer=False):
        player_codes = [c['code'] for c in self.player_hand]
        dealer_codes = [c['code'] for c in self.dealer_hand] if reveal_dealer else [self.dealer_hand[0]['code'], 'XX']

        pimg = await create_card_combo_image(",".join(player_codes), scale_factor=0.4, overlap_percent=0.4)
        dimg = await create_card_combo_image(",".join(dealer_codes), scale_factor=0.4, overlap_percent=0.4)

        pd = io.BytesIO(); pimg.save(pd,'PNG'); pd.seek(0)
        dd = io.BytesIO(); dimg.save(dd,'PNG'); dd.seek(0)

        pf = discord.File(pd, "player.png")
        df = discord.File(dd, "dealer.png")

        embed = discord.Embed(title="Blackjack", color=discord.Color.dark_green())
        embed.add_field("Your Hand", f"Value: {self.calculate_hand_value(self.player_hand)}", inline=False)
        sv= self.calculate_hand_value(self.dealer_hand) if reveal_dealer else f"{self.calculate_hand_value([self.dealer_hand[0]])} + ?"
        embed.add_field("Serene's Hand", f"Value: {sv}", inline=False)
        embed.set_image(url="attachment://player.png")
        embed.set_thumbnail(url="attachment://dealer.png")
        embed.set_footer(text="Hit or Stay?")
        return embed, pf, df

    async def start_game(self, interaction: discord.Interaction):
        view = BlackjackGameView(self)
        embed, pf, df = await self._create_game_embed_with_images()
        msg = await interaction.response.send_message(embed=embed, view=view, files=[pf, df])
        view.message = msg
        active_blackjack_games[self.channel_id] = view

class BlackjackGameView(ui.View):
    def __init__(self, game: BlackjackGame):
        super().__init__(timeout=300)
        self.game = game; self.message=None; self.play_again_task=None

    @ui.button(label="Hit", style=discord.ButtonStyle.green)
    async def hit(self, i:discord.Interaction, b:ui.Button):
        if i.user != self.game.player: return await i.response.send_message("Not your game", ephemeral=True)
        self.game.player_hand.append(self.game.deal_card())
        v = self.game.calculate_hand_value(self.game.player_hand)
        embed, pf, df = await self.game._create_game_embed_with_images()
        if v>21:
            i.response.edit_message(embed=embed, view=self, attachments=[])
            await update_user_kekchipz(i.guild.id,i.user.id, -50)
            await self.end_game("Bust! Serene wins.")
        else:
            await i.response.edit_message(embed=embed, view=self, attachments=[pf,df])

    @ui.button(label="Stay", style=discord.ButtonStyle.red)
    async def stay(self, i:discord.Interaction, b:ui.Button):
        if i.user != self.game.player: return await i.response.send_message("Not your game", ephemeral=True)
        while self.game.calculate_hand_value(self.game.dealer_hand) < 17:
            self.game.dealer_hand.append(self.game.deal_card())
        pv = self.game.calculate_hand_value(self.game.player_hand)
        dv = self.game.calculate_hand_value(self.game.dealer_hand)
        if dv>21 or pv>dv: res="You win!"; delta=+100
        elif pv<dv: res="Serene wins!"; delta=-50
        else: res="Push."; delta=0
        embed, pf, df = await self.game._create_game_embed_with_images(reveal_dealer=True)
        await i.response.edit_message(embed=embed, view=self, attachments=[pf,df])
        await update_user_kekchipz(i.guild.id,i.user.id,delta)
        await self.end_game(res)

    async def end_game(self, result_text):
        for b in self.children: b.disabled=True
        await self.message.edit(content=result_text, view=self)
        del active_blackjack_games[self.game.channel_id]

    async def on_timeout(self):
        if self.message: await self.message.edit(content="Game timed out.", view=None)
        active_blackjack_games.pop(self.game.channel_id, None)

async def start(interaction: discord.Interaction, bot):
    gid = interaction.channel.id
    if gid in active_blackjack_games:
        return await interaction.response.send_message("Game already in progress!", ephemeral=True)
    game = BlackjackGame(gid, interaction.user)
    await game.start_game(interaction)
