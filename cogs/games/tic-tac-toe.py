import discord
import random

class TicTacToeView(discord.ui.View):
    def __init__(self, player: discord.User):
        super().__init__(timeout=60)
        self.board = [" "] * 9
        self.player = player
        self.current_turn = "X"  # Player always starts as X
        self.message = None

        for i in range(9):
            self.add_item(TicTacToeButton(i))

    async def handle_turn(self, interaction: discord.Interaction, index: int):
        if interaction.user != self.player:
            await interaction.response.send_message("This game isn't for you!", ephemeral=True)
            return

        if self.board[index] != " ":
            await interaction.response.send_message("That spot is already taken!", ephemeral=True)
            return

        self.board[index] = "X"
        self.children[index].label = "X"
        self.children[index].disabled = True

        winner = self.check_winner()
        if winner or " " not in self.board:
            await self.end_game(interaction, winner)
            return

        # Serene's turn
        await self.serene_move()
        winner = self.check_winner()
        if winner or " " not in self.board:
            await self.end_game(interaction, winner)
        else:
            await interaction.response.edit_message(view=self)

    async def serene_move(self):
        empty = [i for i, val in enumerate(self.board) if val == " "]
        move = random.choice(empty)
        self.board[move] = "O"
        self.children[move].label = "O"
        self.children[move].disabled = True

    def check_winner(self):
        wins = [
            [0, 1, 2], [3, 4, 5], [6, 7, 8],  # rows
            [0, 3, 6], [1, 4, 7], [2, 5, 8],  # cols
            [0, 4, 8], [2, 4, 6]              # diags
        ]
        for a, b, c in wins:
            if self.board[a] == self.board[b] == self.board[c] != " ":
                return self.board[a]
        return None

    async def end_game(self, interaction: discord.Interaction, winner: str):
        for child in self.children:
            child.disabled = True

        if winner == "X":
            content = f"🎉 {self.player.mention} wins!"
        elif winner == "O":
            content = f"🤖 Serene wins!"
        else:
            content = "It's a tie!"

        await interaction.response.edit_message(content=content, view=self)
        self.stop()

class TicTacToeButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=" ", row=index // 3)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        await view.handle_turn(interaction, self.index)

# Required entry point for dynamic loading
async def start(interaction: discord.Interaction, bot):
    view = TicTacToeView(interaction.user)
    await interaction.response.send_message(f"{interaction.user.mention} vs 🤖 Serene - Tic-Tac-Toe!", view=view)
