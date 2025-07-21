import discord
from discord import ui
import asyncio
import math
import aiomysql # Import aiomysql for database interaction

class TicTacToeButton(ui.Button):
    def __init__(self, row, col, label="⬜"):
        super().__init__(style=discord.ButtonStyle.secondary, label=label, row=row)
        self.row_idx = row
        self.col_idx = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        if view.current_turn != "X":
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        if self.label != "⬜":
            await interaction.response.send_message("This cell is already taken.", ephemeral=True)
            return

        self.label = "❌"
        self.disabled = True
        view.board[self.row_idx][self.col_idx] = "X"
        view.current_turn = "O"

        if view.check_winner("X"):
            await interaction.response.edit_message(view=view)
            await interaction.followup.send("You win! 🎉", ephemeral=True)
            await view.give_kekchipz_reward(interaction.user.id, "win") # Reward for winning
            view.disable_all_buttons()
            return

        if view.is_full():
            await interaction.response.edit_message(view=view)
            await interaction.followup.send("It's a tie!", ephemeral=True)
            await view.give_kekchipz_reward(interaction.user.id, "tie") # Reward for tying
            view.disable_all_buttons()
            return

        await interaction.response.edit_message(view=view)
        await view.bot_move(interaction)


class TicTacToeView(ui.View):
    def __init__(self, interaction: discord.Interaction, db_config: dict):
        super().__init__(timeout=300)
        self.interaction = interaction
        self.db_config = db_config # Store database configuration
        self.board = [["" for _ in range(3)] for _ in range(3)]
        self.current_turn = "X"
        self.add_buttons()

    def add_buttons(self):
        for i in range(3):
            for j in range(3):
                self.add_item(TicTacToeButton(row=i, col=j))

    def disable_all_buttons(self):
        for item in self.children:
            item.disabled = True

    def is_full(self):
        return all(cell != "" for row in self.board for cell in row)

    def check_winner(self, player):
        # Check rows, columns, and diagonals
        return any(
            all(self.board[i][j] == player for j in range(3)) or
            all(self.board[j][i] == player for j in range(3))
            for i in range(3)
        ) or all(self.board[i][i] == player for i in range(3)) or \
               all(self.board[i][2 - i] == player for i in range(3))

    async def give_kekchipz_reward(self, discord_id: int, outcome: str):
        """
        Awards kekchipz based on game outcome and updates the database.
        """
        reward_amount = 0
        if outcome == "win":
            reward_amount = 50
        elif outcome == "tie":
            reward_amount = 25
        elif outcome == "lose":
            reward_amount = 5
        
        if reward_amount > 0:
            conn = None
            try:
                conn = await aiomysql.connect(
                    host=self.db_config['host'],
                    user=self.db_config['user'],
                    password=self.db_config['password'],
                    db="serene_users", # Assuming this is the database name
                    charset='utf8mb4',
                    autocommit=True
                )
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE discord_users SET kekchipz = kekchipz + %s WHERE channel_id = %s AND discord_id = %s",
                        (reward_amount, str(self.interaction.guild.id), str(discord_id))
                    )
                    print(f"User {discord_id} in guild {self.interaction.guild.id} awarded {reward_amount} kekchipz for {outcome}.")
            except Exception as e:
                print(f"Database error while awarding kekchipz: {e}")
            finally:
                if conn:
                    await conn.ensure_closed()

    async def bot_move(self, interaction: discord.Interaction):
        await asyncio.sleep(1)

        row, col = self.best_move()
        self.board[row][col] = "O"

        for item in self.children:
            if isinstance(item, TicTacToeButton) and item.row_idx == row and item.col_idx == col:
                item.label = "⭕"
                item.disabled = True

        if self.check_winner("O"):
            await interaction.edit_original_response(view=self)
            await interaction.followup.send("Serene wins! 😈", ephemeral=True)
            await self.give_kekchipz_reward(interaction.user.id, "lose") # User loses, give 5 kekchipz
            self.disable_all_buttons()
            return

        if self.is_full():
            await interaction.edit_original_response(view=self)
            await interaction.followup.send("It's a tie!", ephemeral=True)
            await self.give_kekchipz_reward(interaction.user.id, "tie") # Reward for tying
            self.disable_all_buttons()
            return

        self.current_turn = "X"
        await interaction.edit_original_response(view=self)

    def best_move(self):
        best_score = -math.inf
        move = None

        for i in range(3):
            for j in range(3):
                if self.board[i][j] == "":
                    self.board[i][j] = "O"
                    score = self.minimax(False)
                    self.board[i][j] = ""
                    if score > best_score:
                        best_score = score
                        move = (i, j)

        return move

    def minimax(self, is_maximizing):
        if self.check_winner("O"):
            return 1
        if self.check_winner("X"):
            return -1
        if self.is_full():
            return 0

        if is_maximizing:
            best_score = -math.inf
            for i in range(3):
                for j in range(3):
                    if self.board[i][j] == "":
                        self.board[i][j] = "O"
                        score = self.minimax(False)
                        self.board[i][j] = ""
                        best_score = max(score, best_score)
            return best_score
        else:
            best_score = math.inf
            for i in range(3):
                for j in range(3):
                    if self.board[i][j] == "":
                        self.board[i][j] = "X"
                        score = self.minimax(True)
                        self.board[i][j] = ""
                        best_score = min(score, best_score)
            return best_score


async def start(interaction: discord.Interaction, bot):
    # Prepare database configuration to pass to the TicTacToeView
    db_config = {
        'host': bot.db_host,
        'user': bot.db_user,
        'password': bot.db_password
    }
    await interaction.response.send_message("Tic-Tac-Toe vs. Serene! ❌ goes first.", view=TicTacToeView(interaction, db_config))
