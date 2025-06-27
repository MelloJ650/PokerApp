import uuid
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, ForeignKey, JSON, DateTime, Enum as EnumDB
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum

from database import Base

class GameStatus(enum.Enum):
    WAITING = "WAITING"
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"

class HandType(enum.Enum):
    HIGH_CARD = "High Card"
    ONE_PAIR = "One Pair"
    TWO_PAIR = "Two Pair"
    THREE_OF_A_KIND = "Three of a Kind"
    STRAIGHT = "Straight"
    FLUSH = "Flush"
    FULL_HOUSE = "Full House"
    FOUR_OF_A_KIND = "Four of a Kind"
    STRAIGHT_FLUSH = "Straight Flush"
    ROYAL_FLUSH = "Royal Flush"

class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    avatar_url = Column(String, default="[https://placehold.co/100x100](https://placehold.co/100x100)")
    current_chips = Column(BigInteger, default=10000)
    total_hands_played = Column(Integer, default=0)
    total_won = Column(BigInteger, default=0)
    total_lost = Column(BigInteger, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    games_played = relationship("GamePlayer", back_populates="user")
    
class Game(Base):
    __tablename__ = "games"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(EnumDB(GameStatus), default=GameStatus.WAITING)
    max_players = Column(Integer, default=6)
    buy_in = Column(Integer, default=1000)
    current_pot = Column(BigInteger, default=0)
    community_cards = Column(JSON, default=list) # e.g., ["AS", "KD", "5C", "TH", "2D"]
    current_bet = Column(BigInteger, default=0)
    last_raiser_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    min_raise = Column(BigInteger, default=0)
    betting_round = Column(String, default="PRE_FLOP") # PRE_FLOP, FLOP, TURN, RIVER, SHOWDOWN
    
    current_turn_player_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    small_blind_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    big_blind_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    small_blind_amount = Column(Integer, default=50)
    big_blind_amount = Column(Integer, default=100)

    players = relationship("GamePlayer", back_populates="game", cascade="all, delete-orphan")
    history = relationship("GameHistory", back_populates="game", cascade="all, delete-orphan")
    
class GamePlayer(Base):
    __tablename__ = "game_players"
    game_id = Column(UUID(as_uuid=True), ForeignKey("games.id"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    
    seat_number = Column(Integer, nullable=False)
    hole_cards = Column(JSON, default=list) # e.g., ["AH", "KH"]
    current_bet = Column(BigInteger, default=0)
    chips_in_hand = Column(BigInteger, nullable=False)
    folded_status = Column(Boolean, default=False)
    all_in_status = Column(Boolean, default=False)
    is_active_in_hand = Column(Boolean, default=True) # Player is still in the current hand
    last_action = Column(String, nullable=True) # e.g., "FOLD", "CALL", "RAISE 200"
    
    user = relationship("User", back_populates="games_played")
    game = relationship("Game", back_populates="players")

class GameHistory(Base):
    __tablename__ = "game_history"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id = Column(UUID(as_uuid=True), ForeignKey("games.id"))
    winning_hand_type = Column(EnumDB(HandType), nullable=True)
    final_pot = Column(BigInteger)
    actions_log = Column(JSON) # Log of actions for this hand
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    winners = Column(JSON, default=list) # List of {user_id, amount_won, winning_hand}
    
    game = relationship("Game", back_populates="history")