import discord
import random

class TicTacToeButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="⬜", row=index // 3)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        await view.handle_turn(interaction, self.index)

class TicTacToeView(discord.ui.View):
    def __init__(self, player: discord.User):
        super().__init__(timeout=60)
        self.player = player
        self.board = ["⬜"] * 9
        self.current_player = "X"  # player goes first
        self.winner = None

        for i in range(9):
            self.add_item(TicTacToeButton(i))

    async def handle_turn(self, interaction: discord.Interaction, index: int):
        if interaction.user != self.player:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return

        if self.board[index] != "⬜":
            await interaction.response.send_message("That space is already taken!", ephemeral=True)
            return

        self.board[index] = "❌"
        self.children[index].label = "❌"
        self.children[index].disabled = True

        if self.check_winner("❌"):
            self.winner = self.player
            await interaction.response.edit_message(content=f"{self.player.mention} wins!", view=self)
            self.disable_all_buttons()
            return

        if self.check_tie():
            await interaction.response.edit_message(content="It's a tie!", view=self)
            self.disable_all_buttons()
            return

        await interaction.response.edit_message(content="Serene is thinking...", view=self)
        await self.serene_turn(interaction)

    async def serene_turn(self, interaction: discord.Interaction):
        available = [i for i in range(9) if self.board[i] == "⬜"]
        move = random.choice(available)

        self.board[move] = "⭕"
        self.children[move].label = "⭕"
        self.children[move].disabled = True

        if self.check_winner("⭕"):
            self.winner = "Serene"
            await interaction.edit_original_response(content="Serene wins!", view=self)
            self.disable_all_buttons()
            return

        if self.check_tie():
            await interaction.edit_original_response(content="It's a tie!", view=self)
            self.disable_all_buttons()
            return

        await interaction.edit_original_response(content=f"{self.player.mention}, your turn!", view=self)

    def check_winner(self, symbol: str):
        combos = [
            [0, 1, 2], [3, 4, 5], [6, 7, 8],  # rows
            [0, 3, 6], [1, 4, 7], [2, 5, 8],  # cols
            [0, 4, 8], [2, 4, 6]              # diags
        ]
        return any(all(self.board[i] == symbol for i in combo) for combo in combos)

    def check_tie(self):
        return all(space != "⬜" for space in self.board)

    def disable_all_buttons(self):
        for child in self.children:
            child.disabled = True

async def start(interaction: discord.Interaction, bot):
    view = TicTacToeView(interaction.user)
    await interaction.response.send_message(f"{interaction.user.mention} vs. Serene — Tic-Tac-Toe begins!", view=view)
