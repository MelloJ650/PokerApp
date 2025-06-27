import random
from collections import Counter
from itertools import combinations
from typing import List, Tuple, Dict
import asyncio
from enum import Enum

from models import Game, HandType

class Card:
    RANKS = '23456789TJQKA'
    SUITS = 'HDCS'

    def __init__(self, card_str: str):
        self.rank_str = card_str[0]
        self.suit = card_str[1]
        self.rank_val = self.RANKS.index(self.rank_str)

    def __repr__(self):
        return f"{self.rank_str}{self.suit}"

class Deck:
    def __init__(self):
        self.cards = [Card(r + s) for r in Card.RANKS for s in Card.SUITS]
        self.shuffle()

    def shuffle(self):
        random.shuffle(self.cards)

    def deal(self, n=1):
        if len(self.cards) < n:
            raise ValueError("Not enough cards in deck to deal.")
        if n == 1:
            return self.cards.pop()
        return [self.cards.pop() for _ in range(n)]

def evaluate_hand(seven_cards: List[Card]) -> Tuple[HandType, List[int], str]:
    """
    Evaluates the best 5-card hand from a list of 7 cards.
    Returns:
        - HandType enum member for the hand rank.
        - A list of integers representing the hand's value for tie-breaking.
        - A string describing the hand (e.g., "Ace High Flush").
    """
    best_rank = -1
    best_value = []
    
    # Iterate through all 21 combinations of 5 cards from 7
    for combo in combinations(seven_cards, 5):
        hand = sorted(combo, key=lambda c: c.rank_val, reverse=True)
        ranks = [c.rank_val for c in hand]
        suits = [c.suit for c in hand]
        
        is_flush = len(set(suits)) == 1
        is_straight = (ranks[0] - ranks[4] == 4 and len(set(ranks)) == 5) or ranks == [12, 3, 2, 1, 0] # Ace-low
        
        # This value list is crucial for tie-breaking.
        # It represents the ranks of the important cards, then kickers.
        hand_value = ranks[:]
        if ranks == [12, 3, 2, 1, 0]: # Handle A-5 straight value
             hand_value = [3, 2, 1, 0, -1] # 5-high straight
             
        rank_counts = Counter(ranks)
        counts = sorted(rank_counts.values(), reverse=True)
        sorted_by_count = sorted(rank_counts.keys(), key=lambda r: (rank_counts[r], r), reverse=True)

        current_rank = 0 # 0: High Card
        
        if is_straight and is_flush:
            current_rank = 9 if ranks == [12, 11, 10, 9, 8] else 8 # Royal/Straight Flush
            hand_value = [ranks[0]]
        elif counts[0] == 4:
            current_rank = 7 # Four of a Kind
            hand_value = [sorted_by_count[0], sorted_by_count[1]]
        elif counts == [3, 2]:
            current_rank = 6 # Full House
            hand_value = [sorted_by_count[0], sorted_by_count[1]]
        elif is_flush:
            current_rank = 5 # Flush
            # value is already all 5 ranks
        elif is_straight:
            current_rank = 4 # Straight
            hand_value = [hand_value[0]] # Highest card in straight
        elif counts[0] == 3:
            current_rank = 3 # Three of a Kind
            hand_value = [sorted_by_count[0]] + sorted_by_count[1:]
        elif counts == [2, 2, 1]:
            current_rank = 2 # Two Pair
            hand_value = sorted(sorted_by_count[:2], reverse=True) + [sorted_by_count[2]]
        elif counts[0] == 2:
            current_rank = 1 # One Pair
            hand_value = [sorted_by_count[0]] + sorted_by_count[1:]

        if current_rank > best_rank or (current_rank == best_rank and hand_value > best_value):
            best_rank = current_rank
            best_value = hand_value

    hand_type_map = {
        9: HandType.ROYAL_FLUSH, 8: HandType.STRAIGHT_FLUSH, 7: HandType.FOUR_OF_A_KIND,
        6: HandType.FULL_HOUSE, 5: HandType.FLUSH, 4: HandType.STRAIGHT, 3: HandType.THREE_OF_A_KIND,
        2: HandType.TWO_PAIR, 1: HandType.ONE_PAIR, 0: HandType.HIGH_CARD
    }

    # Generate descriptive name
    hand_name = hand_type_map[best_rank].value
    
    return hand_type_map[best_rank], best_value, hand_name

def determine_winners(game: Game) -> List[Dict]:
    """
    Determines the winner(s) of the hand and calculates pot distribution, including side pots.
    """
    community_cards = [Card(c) for c in game.community_cards]
    active_players = [p for p in game.players if not p.folded_status]

    if not active_players:
        return []

    if len(active_players) == 1:
        winner = active_players[0]
        return [{"user_id": str(winner.user_id), "amount_won": game.current_pot, "hand_info": "All others folded"}]

    # Evaluate hands for all players remaining in the showdown
    player_evals = []
    for player in active_players:
        hole_cards = [Card(c) for c in player.hole_cards]
        hand_type, value, name = evaluate_hand(hole_cards + community_cards)
        player_evals.append({
            "player": player,
            "hand_type": hand_type,
            "value": value,
            "name": name,
            "total_bet": sum(p.current_bet for p in game.players if p.user_id == player.user_id) # Should be sum of bets over all rounds
        })
    
    # Sort players by hand strength (strongest first)
    player_evals.sort(key=lambda x: (x["hand_type"].value, x["value"]), reverse=True)

    # Pot distribution logic (handles side pots)
    pots = []
    sorted_bettors = sorted([p for p in game.players if p.current_bet > 0], key=lambda p: p.current_bet)
    
    last_bet_level = 0
    for bettor in sorted_bettors:
        if bettor.current_bet > last_bet_level:
            pot_level = bettor.current_bet - last_bet_level
            pot_amount = 0
            for p in game.players:
                contribution = min(max(0, p.current_bet - last_bet_level), pot_level)
                pot_amount += contribution
            
            eligible_players = [p.user_id for p in game.players if p.current_bet >= bettor.current_bet and not p.folded_status]
            pots.append({"amount": pot_amount, "eligible": eligible_players})
            last_bet_level = bettor.current_bet

    # Handle main pot for players who didn't go all-in
    main_pot_amount = game.current_pot - sum(p['amount'] for p in pots)
    if main_pot_amount > 0:
        pots.append({
            "amount": main_pot_amount,
            "eligible": [p.user_id for p in active_players]
        })

    winnings = Counter()
    for pot in pots:
        eligible_winners = [p for p in player_evals if p['player'].user_id in pot['eligible']]
        
        if not eligible_winners:
            continue
            
        best_hand_in_pot = eligible_winners[0]
        winners_of_pot = [p for p in eligible_winners if p['hand_type'] == best_hand_in_pot['hand_type'] and p['value'] == best_hand_in_pot['value']]
        
        win_amount_per_player = pot['amount'] // len(winners_of_pot)
        for winner in winners_of_pot:
            winnings[winner['player'].user_id] += win_amount_per_player

    # Distribute remainder chips (if any)
    remainder = game.current_pot - sum(winnings.values())
    if remainder > 0 and winners_of_pot:
        # Give remainder to first winner in order (e.g., closest to dealer)
        winnings[winners_of_pot[0]['player'].user_id] += remainder

    # Format final results
    winning_players = [p for p in player_evals if p['player'].user_id in winnings]
    return [{"user_id": str(p['player'].user_id), "username": p['player'].user.username, "amount_won": winnings[p['player'].user_id], "hand_info": p['name']} for p in winning_players]
