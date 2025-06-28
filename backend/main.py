from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import uuid as py_uuid
import asyncio
from typing import List
from contextlib import asynccontextmanager
from sqlalchemy.orm import selectinload

from database import engine, get_db, Base
from models import User, Game, GamePlayer, GameStatus
from schemas import UserBase, UserCreate, UserProfile, Token, GameInfo, GameCreate, WSMessage
from security import get_user, verify_password, get_password_hash, create_access_token, get_current_user, get_current_user_from_query
from game_manager import lobby_manager, game_managers, get_game_with_players, broadcast_game_state, start_new_hand, advance_game_state, handle_player_action, ConnectionManager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        # await conn.run_sync(Base.metadata.drop_all) # Use for testing to clear db
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Shutdown
    await engine.dispose()

app = FastAPI(title="Real-Time Poker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=FileResponse)
async def read_index():
    return FileResponse("../frontend/index.html")

# --- REST API Endpoints ---

@app.post("/api/register", response_model=UserBase, status_code=status.HTTP_201_CREATED)
async def register_user(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    db_user = await get_user(db, user_in.username)
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    hashed_password = get_password_hash(user_in.password)
    new_user = User(username=user_in.username, password_hash=hashed_password)
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user

@app.post("/api/login", response_model=Token)
async def login_for_access_token(user_creds: UserCreate, db: AsyncSession = Depends(get_db)):
    user = await get_user(db, user_creds.username)
    if not user or not verify_password(user_creds.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    access_token = create_access_token(data={"sub": user.username})
    user_profile = UserProfile.from_orm(user)
    return {"access_token": access_token, "token_type": "bearer", "user": user_profile}

@app.get("/api/profile/{user_id}", response_model=UserProfile)
async def read_user_profile(user_id: py_uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this profile")
    return current_user

@app.get("/api/games", response_model=List[GameInfo])
async def get_active_games(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Game)
        .where(Game.status == GameStatus.WAITING)
        .options(selectinload(Game.players))
    )
    games = result.scalars().all()
    # Manually construct GameInfo to include player_count
    game_infos = []
    for game in games:
        game_infos.append(GameInfo(
            id=game.id,
            max_players=game.max_players,
            buy_in=game.buy_in,
            player_count=len(game.players)
        ))
    return game_infos

@app.post("/api/games", response_model=GameInfo, status_code=status.HTTP_201_CREATED)
async def create_new_game(game_in: GameCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    new_game = Game(
        max_players=game_in.max_players,
        buy_in=game_in.buy_in,
        small_blind_amount=game_in.buy_in // 100,
        big_blind_amount=game_in.buy_in // 50
    )
    db.add(new_game)
    await db.commit()
    await db.refresh(new_game)
    
    # Automatically broadcast to lobby
    games = await get_active_games(db)
    # Convert GameInfo objects to JSON-serializable dictionaries
    games_dict = []
    for game in games:
        games_dict.append({
            "id": str(game.id),
            "max_players": game.max_players,
            "buy_in": game.buy_in,
            "player_count": game.player_count
        })
    await lobby_manager.broadcast({"type": "game_list", "games": games_dict}, None)
    
    return GameInfo(
        id=new_game.id,
        max_players=new_game.max_players,
        buy_in=new_game.buy_in,
        player_count=0
    )


# --- WebSocket Endpoints ---

@app.websocket("/ws/lobby")
async def websocket_lobby_endpoint(websocket: WebSocket, token: str, db: AsyncSession = Depends(get_db)):
    try:
        user = await get_current_user_from_query(token, db)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
        
    await lobby_manager.connect(websocket, None) # Use None as a "global" room ID for lobby
    
    try:
        # Send initial game list
        games = await get_active_games(db)
        # Convert GameInfo objects to JSON-serializable dictionaries
        games_dict = []
        for game in games:
            games_dict.append({
                "id": str(game.id),
                "max_players": game.max_players,
                "buy_in": game.buy_in,
                "player_count": game.player_count
            })
        await websocket.send_json({"type": "game_list", "games": games_dict})
        
        while True:
            # Lobby is mostly for receiving updates, so we just wait
            await websocket.receive_text()
    except WebSocketDisconnect:
        lobby_manager.disconnect(websocket, None)


@app.websocket("/ws/game/{game_id}")
async def websocket_game_endpoint(websocket: WebSocket, game_id: py_uuid.UUID, token: str, db: AsyncSession = Depends(get_db)):
    try:
        user = await get_current_user_from_query(token, db)
        websocket.scope["user"] = user # Attach user to the connection scope
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    game = await get_game_with_players(db, game_id)
    if not game:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Game not found")
        return

    if game.id not in game_managers:
        game_managers[game_id] = ConnectionManager()

    await game_managers[game_id].connect(websocket, game_id)

    # Handle player joining logic
    player_in_game = any(p.user_id == user.id for p in game.players)
    if not player_in_game and len(game.players) < game.max_players and game.status == GameStatus.WAITING:
        if user.current_chips < game.buy_in:
             await websocket.send_json({"type": "error", "message": "Not enough chips to join."})
             game_managers[game_id].disconnect(websocket, game_id)
             return
        
        user.current_chips -= game.buy_in
        new_player = GamePlayer(
            game_id=game_id,
            user_id=user.id,
            seat_number=len(game.players) + 1,
            chips_in_hand=game.buy_in
        )
        db.add(new_player)
        await db.commit()
        
        # If table is now full enough, start game
        if len(game.players) + 1 >= 2 and game.status == GameStatus.WAITING:
             game.status = GameStatus.IN_PROGRESS
             await db.commit()
             # Wait a moment before starting to allow connections to establish
             await asyncio.sleep(1)
             await start_new_hand(game_id, db)
        else:
            await broadcast_game_state(game_id, db)
    
    # Send initial state
    await broadcast_game_state(game_id, db)

    try:
        while True:
            data = await websocket.receive_json()
            message = WSMessage(**data)
            
            if message.type == 'game_action':
                try:
                    await handle_player_action(game_id, user.id, message.action, db)
                except ValueError as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        game_managers[game_id].disconnect(websocket, game_id)
        # Handle player leaving logic (e.g., fold their hand)
        player = next((p for p in game.players if p.user_id == user.id), None)
        if player and game.status == GameStatus.IN_PROGRESS:
            player.folded_status = True
            player.is_active_in_hand = False
            player.last_action = "DISCONNECTED"
            # If it was their turn, advance the game
            if game.current_turn_player_id == user.id:
                await advance_game_state(game_id, db)
            else:
                await broadcast_game_state(game_id, db)
