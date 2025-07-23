import random

class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank # e.g., "2", "3", ..., "0", "J", "Q", "K", "A"

    def __str__(self):
        # This is the two-character code for display/transfer
        return f"{self.rank}{self.suit[0].upper()}"

    def to_output_format(self):
        """Returns the card in the desired two-character output format."""
        return str(self)

    @staticmethod
    def from_output_format(card_str: str):
        """Reconstructs a Card object from its two-character string format."""
        if len(card_str) < 2:
            raise ValueError(f"Invalid card string format: {card_str}")
        
        rank_char = card_str[:-1]
        suit_char = card_str[-1].lower()

        # Map suit character back to full suit name
        suit_map = {'h': 'Hearts', 'd': 'Diamonds', 'c': 'Clubs', 's': 'Spades'}
        suit = suit_map.get(suit_char)
        if not suit:
            raise ValueError(f"Invalid suit character: {suit_char} in {card_str}")

        return Card(suit, rank_char)

class Deck:
    def __init__(self, cards_data=None):
        """
        Initializes a Deck. If 'cards_data' is provided (from a serialized state,
        expected as a list of two-character strings), it reconstructs the deck.
        Otherwise, it builds a new one.
        """
        if cards_data is None:
            self.cards = []
            self.build()
        else:
            # Reconstruct Card objects from their two-character string representation
            self.cards = [Card.from_output_format(c_str) for c_str in cards_data]

    def build(self):
        """Builds a standard 52-card deck."""
        suits = ["Hearts", "Diamonds", "Clubs", "Spades"]
        # "10" is represented as "0" as per user's requirement for 2-character generation
        ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "0", "J", "Q", "K", "A"]
        self.cards = [Card(suit, rank) for suit in suits for rank in ranks]

    def shuffle(self):
        """Shuffles the deck."""
        random.shuffle(self.cards)

    def deal_card(self):
        """Deals a single card from the top of the deck."""
        if not self.cards:
            # In a real game, you might want to handle this more robustly,
            # e.g., by reshuffling the discard pile if applicable.
            return None
        return self.cards.pop()

    def to_output_format(self):
        """Converts the deck to a list of two-character strings for serialization."""
        return [card.to_output_format() for card in self.cards]
