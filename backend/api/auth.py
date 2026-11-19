import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_session
from backend.database.models import LinkToken, User, utc_now
from backend.settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                          max_length=255, pattern=r"^[\w.@-]+$")]
bearer = HTTPBearer(auto_error=False)


class Credentials(BaseModel):
    username: Username
    password: str = Field(min_length=1, max_length=1024)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    telegram_id: int | None


def _derive(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


async def create_account(
    session: AsyncSession, username: str, password: str, telegram_id: int | None = None
) -> User:
    salt = secrets.token_hex(16)
    digest = await asyncio.to_thread(_derive, password, salt)
    user = User(username=username, login_name=username.casefold(), telegram_id=telegram_id,
                password_hash=f"{salt}:{digest}")
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Имя пользователя или Telegram уже заняты.") from None
    return user


def _sign(payload: str) -> str:
    return hmac.digest(settings.secret_key.encode(), payload.encode(), "sha256").hex()


def _access_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + 86_400}"
    return f"{payload}.{_sign(payload)}"


async def user_for_token(session: AsyncSession, token: str | None) -> User | None:
    if not token or len(token) > 128:
        return None
    try:
        user_id, expires, signature = token.split(".")
        if hmac.compare_digest(_sign(f"{user_id}.{expires}"), signature) and int(expires) > time.time():
            return await session.get(User, int(user_id))
    except ValueError:
        pass
    return None


async def current_user(
    session: Session, auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> User:
    user = await user_for_token(session, auth.credentials if auth else None)
    if user is None:
        raise HTTPException(401, "Требуется авторизация.", headers={"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(current_user)]


@router.post("/register", response_model=UserView, status_code=201)
async def register(data: Credentials, session: Session):
    if len(data.password) < 8:
        raise HTTPException(422, "Пароль должен содержать не менее 8 символов.")
    return await create_account(session, data.username, data.password)


@router.post("/login")
async def login(data: Credentials, session: Session):
    user = await session.scalar(select(User).where(User.login_name == data.username.casefold()))
    salt, expected = user.password_hash.split(":") if user and user.password_hash else ("0" * 32, "")
    digest = await asyncio.to_thread(_derive, data.password, salt)
    if user is None or not hmac.compare_digest(digest, expected):
        raise HTTPException(401, "Неверное имя пользователя или пароль.")
    return {"access_token": _access_token(user.id), "token_type": "bearer"}


@router.get("/me", response_model=UserView)
async def me(user: CurrentUser):
    return user


def _code_digest(code: str) -> str:
    return _sign(f"telegram-link:{code}")


@router.post("/generate-link-code")
async def generate_link_code(session: Session, user: CurrentUser):
    if user.telegram_id is not None:
        raise HTTPException(409, "Telegram уже привязан.")
    user_id = user.id
    for _ in range(10):
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = utc_now()
        await session.execute(delete(LinkToken).where(LinkToken.expires_at <= now))
        session.add(LinkToken(token=_code_digest(code), user_id=user_id,
                              expires_at=now + timedelta(minutes=5)))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        else:
            return {"code": code, "expires_in": 300}
    raise HTTPException(503, "Не удалось создать код. Повторите запрос.")


async def link_telegram(session: AsyncSession, code: str, telegram_id: int) -> bool:
    try:
        owner_id = await session.scalar(
            update(LinkToken)
            .where(LinkToken.token == _code_digest(code), LinkToken.used.is_(False),
                   LinkToken.expires_at > utc_now())
            .values(used=True).returning(LinkToken.user_id)
        )
        if owner_id is not None:
            linked = await session.scalar(
                update(User).where(User.id == owner_id, User.telegram_id.is_(None))
                .values(telegram_id=telegram_id).returning(User.id)
            )
            if linked is not None:
                await session.commit()
                return True
    except IntegrityError:
        pass
    await session.rollback()
    return False
