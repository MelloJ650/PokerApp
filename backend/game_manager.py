from fastapi import WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.orm import selectinload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import logging
import uuid as py_uuid
import asyncio
from typing import Dict, List, Optional

from models import Game, GamePlayer, GameStatus
from schemas import PlayerState, GameState, PlayerAction
from poker_logic import Deck, determine_winners

logging.basicConfig(level=logging.INFO)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[py_uuid.UUID, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, game_id: py_uuid.UUID):
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = []
        self.active_connections[game_id].append(websocket)

    def disconnect(self, websocket: WebSocket, game_id: py_uuid.UUID):
        if game_id in self.active_connections:
            self.active_connections[game_id].remove(websocket)
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]

    async def broadcast(self, message: dict, game_id: py_uuid.UUID):
        if game_id in self.active_connections:
            for connection in self.active_connections[game_id]:
                await connection.send_json(message)

lobby_manager = ConnectionManager()
game_managers: Dict[py_uuid.UUID, ConnectionManager] = {}

async def get_game_with_players(db: AsyncSession, game_id: py_uuid.UUID) -> Optional[Game]:
    """Utility to fetch a game with its players and their user info eagerly loaded."""
    result = await db.execute(
        select(Game)
        .where(Game.id == game_id)
        .options(selectinload(Game.players).selectinload(GamePlayer.user))
    )
    return result.scalars().first()

async def format_game_state(game: Game, last_event: Optional[dict] = None) -> dict:
    """Formats the game model into a Pydantic schema for sending to clients."""
    players_state = []
    for p in game.players:
        # By default, don't reveal hole cards
        cards_to_show = ["", ""]
        if game.betting_round == "SHOWDOWN" or p.all_in_status:
            cards_to_show = p.hole_cards if not p.folded_status else ["", ""]

        players_state.append(PlayerState(
            user_id=p.user_id,
            username=p.user.username,
            seat_number=p.seat_number,
            hole_cards=cards_to_show, # Important: controlled reveal
            chips_in_hand=p.chips_in_hand,
            current_bet=p.current_bet,
            folded_status=p.folded_status,
            all_in_status=p.all_in_status,
            last_action=p.last_action
        ))
        
    game_state = GameState(
        id=game.id,
        status=game.status.value,
        max_players=game.max_players,
        current_pot=game.current_pot,
        community_cards=game.community_cards,
        current_bet=game.current_bet,
        betting_round=game.betting_round,
        current_turn_player_id=game.current_turn_player_id,
        dealer_id=game.dealer_id,
        small_blind_id=game.small_blind_id,
        big_blind_id=game.big_blind_id,
        min_raise=game.min_raise,
        players=players_state,
        last_event=last_event,
        showdown_active=(game.betting_round == "SHOWDOWN")
    )
    return game_state.dict()
    
async def broadcast_game_state(game_id: py_uuid.UUID, db: AsyncSession, last_event: Optional[dict] = None):
    """Fetches the latest game state and broadcasts it to all players in the game."""
    game = await get_game_with_players(db, game_id)
    if not game: return

    if game.id not in game_managers:
        game_managers[game.id] = ConnectionManager()
        
    # For each player, we need to customize the state to show their own cards
    for player_connection in game_managers[game.id].active_connections:
        current_user_id = player_connection.scope.get("user").id
        
        # Create a copy of the state to modify
        state_for_player = await format_game_state(game, last_event)
        
        # Find the current player in the state and reveal their cards
        for p_state in state_for_player['players']:
            if p_state['user_id'] == current_user_id:
                player_db_obj = next((p for p in game.players if p.user_id == current_user_id), None)
                if player_db_obj:
                    p_state['hole_cards'] = player_db_obj.hole_cards
                break
        
        await player_connection.send_json({"type": "game_state", "state": state_for_player})


async def start_new_hand(game_id: py_uuid.UUID, db: AsyncSession):
    game = await get_game_with_players(db, game_id)
    if not game or len(game.players) < 2:
        game.status = GameStatus.WAITING
        await db.commit()
        await broadcast_game_state(game_id, db)
        return
    
    # Reset players for new hand
    for p in game.players:
        p.hole_cards = []
        p.current_bet = 0
        p.folded_status = False
        p.all_in_status = False
        p.is_active_in_hand = True
        p.last_action = None

    # Rotate dealer button
    player_ids = [p.user_id for p in sorted(game.players, key=lambda p: p.seat_number)]
    if game.dealer_id and game.dealer_id in player_ids:
        current_dealer_index = player_ids.index(game.dealer_id)
        game.dealer_id = player_ids[(current_dealer_index + 1) % len(player_ids)]
    else:
        game.dealer_id = player_ids[0]

    # Assign blinds
    dealer_idx = player_ids.index(game.dealer_id)
    game.small_blind_id = player_ids[(dealer_idx + 1) % len(player_ids)]
    game.big_blind_id = player_ids[(dealer_idx + 2) % len(player_ids)]

    # Collect blinds
    game.current_pot = 0
    game.current_bet = game.big_blind_amount
    game.min_raise = game.big_blind_amount

    sb_player = next(p for p in game.players if p.user_id == game.small_blind_id)
    sb_amount = min(sb_player.chips_in_hand, game.small_blind_amount)
    sb_player.chips_in_hand -= sb_amount
    sb_player.current_bet = sb_amount
    sb_player.last_action = f"POSTS SB {sb_amount}"
    game.current_pot += sb_amount
    
    bb_player = next(p for p in game.players if p.user_id == game.big_blind_id)
    bb_amount = min(bb_player.chips_in_hand, game.big_blind_amount)
    bb_player.chips_in_hand -= bb_amount
    bb_player.current_bet = bb_amount
    bb_player.last_action = f"POSTS BB {bb_amount}"
    game.current_pot += bb_amount

    # Deal cards
    deck = Deck()
    for p in game.players:
        p.hole_cards = [str(c) for c in deck.deal(2)]

    # Set first turn (player after big blind)
    bb_idx = player_ids.index(game.big_blind_id)
    game.current_turn_player_id = player_ids[(bb_idx + 1) % len(player_ids)]
    game.last_raiser_id = game.big_blind_id

    game.betting_round = "PRE_FLOP"
    game.community_cards = []
    
    await db.commit()
    await broadcast_game_state(game_id, db)

async def advance_game_state(game_id: py_uuid.UUID, db: AsyncSession):
    game = await get_game_with_players(db, game_id)
    active_players = [p for p in game.players if p.is_active_in_hand and not p.all_in_status]
    
    # Check if betting round is over
    all_bets_equal = len(set(p.current_bet for p in active_players)) <= 1
    last_raiser = next(p for p in game.players if p.user_id == game.last_raiser_id)
    turn_is_on_last_raiser = game.current_turn_player_id == game.last_raiser_id
    
    if (all_bets_equal and turn_is_on_last_raiser) or len(active_players) <= 1:
        # Proceed to next round
        game.current_pot += sum(p.current_bet for p in game.players)
        for p in game.players:
            p.current_bet = 0
            p.last_action = None # Clear actions for next round
        
        game.current_bet = 0
        game.min_raise = game.big_blind_amount # Reset min raise
        
        # Find first active player to act after the button
        player_ids = [p.user_id for p in sorted(game.players, key=lambda x: x.seat_number)]
        dealer_idx = player_ids.index(game.dealer_id) if game.dealer_id in player_ids else -1
        
        next_actor_found = False
        for i in range(1, len(player_ids) + 1):
            next_player_id = player_ids[(dealer_idx + i) % len(player_ids)]
            next_player = next((p for p in game.players if p.user_id == next_player_id), None)
            if next_player and next_player.is_active_in_hand and not next_player.all_in_status:
                game.current_turn_player_id = next_player_id
                game.last_raiser_id = next_player_id
                next_actor_found = True
                break
        
        if not next_actor_found: # All remaining players are all-in
            # Skip directly to showdown
            while game.betting_round != "SHOWDOWN":
                await advance_game_state(game_id, db)
            return

        deck = Deck()
        # Burn one, then deal
        deck.deal(1)

        if game.betting_round == "PRE_FLOP":
            game.betting_round = "FLOP"
            game.community_cards = [str(c) for c in deck.deal(3)]
        elif game.betting_round == "FLOP":
            game.betting_round = "TURN"
            game.community_cards.append(str(deck.deal(1)))
        elif game.betting_round == "TURN":
            game.betting_round = "RIVER"
            game.community_cards.append(str(deck.deal(1)))
        elif game.betting_round == "RIVER":
            game.betting_round = "SHOWDOWN"
    else:
        # Move to next player
        player_ids = [p.user_id for p in sorted(game.players, key=lambda p: p.seat_number)]
        current_idx = player_ids.index(game.current_turn_player_id)
        
        for i in range(1, len(player_ids) + 1):
            next_player_id = player_ids[(current_idx + i) % len(player_ids)]
            next_player = next((p for p in game.players if p.user_id == next_player_id), None)
            if next_player and next_player.is_active_in_hand and not next_player.all_in_status:
                game.current_turn_player_id = next_player_id
                break
                
    await db.commit()
    
    # If showdown, determine winner
    if game.betting_round == "SHOWDOWN":
        await handle_showdown(game_id, db)
    else:
        await broadcast_game_state(game_id, db)


async def handle_showdown(game_id: py_uuid.UUID, db: AsyncSession):
    game = await get_game_with_players(db, game_id)
    
    # Add all bets to pot before determining winner
    game.current_pot += sum(p.current_bet for p in game.players)
    for p in game.players:
        p.current_bet = 0

    winners_data = determine_winners(game)
    
    # Update player chips
    for winner_info in winners_data:
        winner_player = next((p for p in game.players if p.user_id == py_uuid.UUID(winner_info["user_id"])), None)
        if winner_player:
            winner_player.chips_in_hand += winner_info["amount_won"]
    
    # Create a nice message
    winner_summary = ", ".join([f"{w['username']} wins {w['amount_won']} with {w['hand_info']}" for w in winners_data])
    last_event = {"title": "Hand Over!", "message": winner_summary}
    
    await db.commit()
    await broadcast_game_state(game_id, db, last_event)
    
    # Pause for a few seconds before starting the next hand
    await asyncio.sleep(8)
    await start_new_hand(game_id, db)

async def handle_player_action(game_id: py_uuid.UUID, user_id: py_uuid.UUID, action: PlayerAction, db: AsyncSession):
    game = await get_game_with_players(db, game_id)
    player = next((p for p in game.players if p.user_id == user_id), None)
    
    # Validations
    if not game or not player:
        raise HTTPException(status_code=404, detail="Game or player not found")
    if game.current_turn_player_id != user_id:
        raise ValueError("Not your turn")
        
    action_type = action.type.lower()
    
    if action_type == "fold":
        player.folded_status = True
        player.is_active_in_hand = False
        player.last_action = "FOLD"
    
    elif action_type == "check":
        if player.current_bet < game.current_bet:
            raise ValueError("Cannot check, must call or raise")
        player.last_action = "CHECK"
        
    elif action_type == "call":
        amount_to_call = game.current_bet - player.current_bet
        if amount_to_call > player.chips_in_hand: # Player goes all-in to call
             player.current_bet += player.chips_in_hand
             player.chips_in_hand = 0
             player.all_in_status = True
             player.last_action = "ALL-IN (CALL)"
        else:
            player.chips_in_hand -= amount_to_call
            player.current_bet += amount_to_call
            player.last_action = f"CALL {amount_to_call}"
            
    elif action_type == "raise":
        raise_amount = action.amount
        total_bet = raise_amount
        if total_bet < game.current_bet + game.min_raise:
            raise ValueError(f"Must raise by at least {game.min_raise}")
        if total_bet > player.chips_in_hand + player.current_bet:
            raise ValueError("Not enough chips to raise")
        
        amount_from_stack = total_bet - player.current_bet
        player.chips_in_hand -= amount_from_stack
        
        game.min_raise = total_bet - game.current_bet
        game.current_bet = total_bet
        player.current_bet = total_bet
        game.last_raiser_id = player.user_id
        player.last_action = f"RAISE TO {total_bet}"
        
    elif action_type == "all_in":
        total_bet = player.chips_in_hand + player.current_bet
        player.chips_in_hand = 0
        player.all_in_status = True
        
        if total_bet > game.current_bet: # This is a raise
            game.min_raise = max(game.min_raise, total_bet - game.current_bet)
            game.current_bet = total_bet
            game.last_raiser_id = player.user_id
            
        player.current_bet = total_bet
        player.last_action = "ALL-IN"
        
    else:
        raise ValueError("Invalid action type")

    await db.commit()
    await advance_game_state(game_id, db)
