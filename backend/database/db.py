"""Async SQLAlchemy engine and session configuration."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.settings import settings

DATABASE_URL = settings.database_url


class Base(DeclarativeBase):
    """Base class for Loomis ORM models."""


sqlite = DATABASE_URL.startswith("sqlite")
engine = create_async_engine(DATABASE_URL, connect_args={"timeout": 30} if sqlite else {})
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _configure_sqlite(connection, _record) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


if sqlite:
    event.listen(engine.sync_engine, "connect", _configure_sqlite)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
