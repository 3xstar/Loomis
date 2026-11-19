from aiogram import BaseMiddleware
from sqlalchemy import select

from backend.database.db import async_session_factory
from backend.database.models import User


class AccountMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        sender = data.get("event_from_user")
        async with async_session_factory() as session:
            data["session"] = session
            data["account"] = None if sender is None else await session.scalar(
                select(User).where(User.telegram_id == sender.id)
            )
            return await handler(event, data)
