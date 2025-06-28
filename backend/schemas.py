from pydantic import BaseModel, Field
import uuid as py_uuid
from typing import List, Optional

# User Schemas
class UserCreate(BaseModel):
    username: str
    password: str

class UserBase(BaseModel):
    id: py_uuid.UUID
    username: str
    avatar_url: str
    
    class Config:
        from_attributes = True

class UserProfile(UserBase):
    current_chips: int
    total_hands_played: int
    total_won: int
    total_lost: int

# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserProfile

# Game Schemas
class GameCreate(BaseModel):
    max_players: int = 6
    buy_in: int = 1000

class PlayerState(BaseModel):
    user_id: py_uuid.UUID
    username: str
    seat_number: int
    hole_cards: List[str]
    chips_in_hand: int
    current_bet: int
    folded_status: bool
    all_in_status: bool
    last_action: Optional[str]

class GameState(BaseModel):
    id: py_uuid.UUID
    status: str
    max_players: int
    current_pot: int
    community_cards: List[str]
    current_bet: int
    betting_round: str
    current_turn_player_id: Optional[py_uuid.UUID]
    dealer_id: Optional[py_uuid.UUID]
    small_blind_id: Optional[py_uuid.UUID]
    big_blind_id: Optional[py_uuid.UUID]
    min_raise: int
    players: List[PlayerState]
    last_event: Optional[dict] = None # For pop-up messages like winner announcements
    showdown_active: bool = False

class GameInfo(BaseModel):
    id: py_uuid.UUID
    max_players: int
    buy_in: int
    player_count: int
    
    class Config:
        from_attributes = True

# WebSocket Message Schemas
class PlayerAction(BaseModel):
    type: str  # e.g., 'fold', 'call', 'raise', 'check', 'all_in'
    amount: Optional[int] = 0

class WSMessage(BaseModel):
    type: str # 'game_action', 'chat_message', etc.
    action: Optional[PlayerAction] = None
    message: Optional[str] = None
