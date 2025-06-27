# FILE: backend/database.py
# ------------------------

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# Use an environment variable for the database URL, with a sensible default for local development.
# The user must replace this with their actual PostgreSQL connection string.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://jmello@localhost:5432/pokerdb")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    """Dependency to get a database session."""
    async with async_session() as session:
        yield session