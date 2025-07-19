import math

class TicTacToe:
    def __init__(self):
        self.board = [' ' for _ in range(9)]
        self.current_winner = None

    def available_moves(self):
        return [i for i, spot in enumerate(self.board) if spot == ' ']

    def empty_squares(self):
        return ' ' in self.board

    def make_move(self, square, player):
        if self.board[square] == ' ':
            self.board[square] = player
            if self.check_winner(square, player):
                self.current_winner = player
            return True
        return False

    def check_winner(self, square, player):
        row_ind = square // 3
        row = self.board[row_ind*3 : (row_ind+1)*3]
        if all(s == player for s in row):
            return True

        col_ind = square % 3
        column = [self.board[col_ind + i*3] for i in range(3)]
        if all(s == player for s in column):
            return True

        if square % 2 == 0:
            diagonal1 = [self.board[i] for i in [0, 4, 8]]
            diagonal2 = [self.board[i] for i in [2, 4, 6]]
            if all(s == player for s in diagonal1) or all(s == player for s in diagonal2):
                return True

        return False

    def get_board_as_string(self):
        board_str = ""
        for row in [self.board[i*3:(i+1)*3] for i in range(3)]:
            board_str += "| " + " | ".join(row) + " |\n"
        return board_str

class DummyPlayer:
    def __init__(self, symbol):
        self.symbol = symbol

    def get_move(self, game):
        return game.available_moves()[0]  # Always pick first available

def play_game(game_instance, x_player, o_player):
    current_player = 'X'

    while game_instance.empty_squares():
        if current_player == 'O':
            square = o_player.get_move(game_instance)
        else:
            square = x_player.get_move(game_instance)

        if game_instance.make_move(square, current_player):
            if game_instance.current_winner:
                return current_player

            current_player = 'O' if current_player == 'X' else 'X'
        else:
            break

    return "Tie"

# Entry point for the Discord bot
async def start(interaction):
    game = TicTacToe()
    x_player = DummyPlayer('X')
    o_player = DummyPlayer('O')
    winner = play_game(game, x_player, o_player)
    board_display = game.get_board_as_string()

    await interaction.response.send_message(
        f"🎮 Tic-Tac-Toe Game Result:\n```{board_display}```\nWinner: **{winner}**"
    )
