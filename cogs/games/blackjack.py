import discord
from discord.ext import commands
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

class Blackjack(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.deck = []
        self.player_hand = []
        self.dealer_hand = []
        self.game_in_progress = False

    def create_deck(self):
        suits = ['♠', '♥', '♦', '♣']
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        self.deck = [f'{rank}{suit}' for suit in suits for rank in ranks]

    def shuffle_deck(self):
        import random
        random.shuffle(self.deck)

    def draw_card(self):
        return self.deck.pop()

    def calculate_hand_value(self, hand):
        value = 0
        aces = 0
        for card in hand:
            rank = card[:-1]
            if rank in ['J', 'Q', 'K']:
                value += 10
            elif rank == 'A':
                value += 11
                aces += 1
            else:
                value += int(rank)
        while value > 21 and aces:
            value -= 10
            aces -= 1
        return value

    def create_blackjack_image(self, player_hand, dealer_hand, hide_dealer_card=True):
        # Simplified: create an image showing cards as text for demonstration
        width, height = 400, 200
        img = Image.new('RGBA', (width, height), (34, 139, 34, 255))  # green felt background
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()

        # Dealer's cards
        draw.text((10, 10), "Dealer's Hand:", fill='white', font=font)
        for i, card in enumerate(dealer_hand):
            text = '??' if (i == 0 and hide_dealer_card) else card
            draw.text((10 + i*40, 30), text, fill='white', font=font)

        # Player's cards
        draw.text((10, 100), "Your Hand:", fill='white', font=font)
        for i, card in enumerate(player_hand):
            draw.text((10 + i*40, 120), card, fill='white', font=font)

        return img

    @commands.command(name="blackjack")
    async def start_blackjack(self, ctx):
        if self.game_in_progress:
            await ctx.send("A game is already in progress!")
            return

        self.game_in_progress = True
        self.create_deck()
        self.shuffle_deck()
        self.player_hand = [self.draw_card(), self.draw_card()]
        self.dealer_hand = [self.draw_card(), self.draw_card()]

        img = self.create_blackjack_image(self.player_hand, self.dealer_hand)
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        file = discord.File(fp=buffer, filename='blackjack.png')

        await ctx.send(f"Game started! Your cards:", file=file)
        await ctx.send("Type `!hit` to draw a card or `!stand` to hold.")

    @commands.command()
    async def hit(self, ctx):
        if not self.game_in_progress:
            await ctx.send("No game in progress. Start one with `!blackjack`.")
            return

        self.player_hand.append(self.draw_card())
        player_value = self.calculate_hand_value(self.player_hand)
        dealer_value = self.calculate_hand_value(self.dealer_hand)

        img = self.create_blackjack_image(self.player_hand, self.dealer_hand)
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        file = discord.File(fp=buffer, filename='blackjack.png')

        if player_value > 21:
            self.game_in_progress = False
            await ctx.send("You busted! Dealer wins.", file=file)
        else:
            await ctx.send(f"You drew a card. Your total is {player_value}.", file=file)
            await ctx.send("Type `!hit` to draw again or `!stand` to hold.")

    @commands.command()
    async def stand(self, ctx):
        if not self.game_in_progress:
            await ctx.send("No game in progress. Start one with `!blackjack`.")
            return

        player_value = self.calculate_hand_value(self.player_hand)
        dealer_value = self.calculate_hand_value(self.dealer_hand)

        # Dealer draws cards until 17 or higher
        while dealer_value < 17:
            self.dealer_hand.append(self.draw_card())
            dealer_value = self.calculate_hand_value(self.dealer_hand)

        img = self.create_blackjack_image(self.player_hand, self.dealer_hand, hide_dealer_card=False)
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        file = discord.File(fp=buffer, filename='blackjack.png')

        result = ""
        if dealer_value > 21:
            result = "Dealer busts! You win!"
        elif dealer_value > player_value:
            result = "Dealer wins!"
        elif dealer_value < player_value:
            result = "You win!"
        else:
            result = "It's a tie!"

        self.game_in_progress = False
        await ctx.send(result, file=file)

async def setup(bot):
    await bot.add_cog(Blackjack(bot))
