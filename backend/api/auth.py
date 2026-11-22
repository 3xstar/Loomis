import asyncio
import hashlib
import hmac
import os
import secrets
import time
from datetime import timedelta
from typing import Annotated

import httpx
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, computed_field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.bot.keyboards import magic_login_keyboard
from backend.core.github import bind_github
from backend.database.db import get_session
from backend.database.models import DEFAULT_PROJECTS_DIR, LinkToken, MagicLoginRequest, User, utc_now
from backend.settings import ROOT, settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                          max_length=255, pattern=r"^[\w.@-]+$")]
bearer = HTTPBearer(auto_error=False)
load_dotenv(ROOT / ".env")


class Credentials(BaseModel):
    username: Username
    password: str = Field(min_length=1, max_length=1024)


class MagicLoginStart(BaseModel):
    username: Username


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    telegram_id: int | None
    tg_logs_enabled: bool
    github_username: str | None
    github_avatar_url: str | None
    telegram_username: str | None
    telegram_avatar_url: str | None
    default_projects_dir: str

    @field_validator("default_projects_dir", mode="before")
    @classmethod
    def resolve_default_projects_dir(cls, value: str | None) -> str:
        return value or DEFAULT_PROJECTS_DIR

    @computed_field
    @property
    def workspaces_root(self) -> str:
        return str(ROOT / "workspaces")


def _derive(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


async def set_password(session: AsyncSession, user: User, password: str) -> None:
    salt = secrets.token_hex(16)
    digest = await asyncio.to_thread(_derive, password, salt)
    user.password_hash = f"{salt}:{digest}"
    await session.commit()


async def create_account(
    session: AsyncSession, username: str, password: str, telegram_id: int | None = None
) -> User:
    user = User(username=username, login_name=username.casefold(), telegram_id=telegram_id)
    session.add(user)
    try:
        await set_password(session, user, password)
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


@router.post("/magic-login/request", status_code=202)
async def request_magic_login(data: MagicLoginStart, session: Session):
    from backend.core.orchestrator import orchestrator

    user = await session.scalar(
        select(User).where(User.login_name == data.username.casefold()).with_for_update()
    )
    if user is None or user.telegram_id is None:
        raise HTTPException(404, "Аккаунт с привязанным Telegram не найден.")
    bot = orchestrator.bot
    if bot is None:
        raise HTTPException(503, "Telegram-бот недоступен.")

    request_id = secrets.token_urlsafe(32)
    chat_id, username = user.telegram_id, user.username or data.username
    await session.execute(delete(MagicLoginRequest).where(
        MagicLoginRequest.user_id == user.id, MagicLoginRequest.status == "pending"
    ))
    session.add(MagicLoginRequest(request_id=request_id, user_id=user.id,
                                  expires_at=utc_now() + timedelta(minutes=5)))
    await session.commit()

    try:
        await bot.send_message(
            chat_id,
            f"🔐 Запрос входа в Loomis Desktop\nАккаунт: {username}\nЕсли это вы, подтвердите вход.",
            reply_markup=magic_login_keyboard(request_id),
        )
    except TelegramAPIError:
        await session.execute(delete(MagicLoginRequest).where(
            MagicLoginRequest.request_id == request_id, MagicLoginRequest.status == "pending"
        ))
        await session.commit()
        raise HTTPException(503, "Telegram-бот недоступен.") from None
    return {"request_id": request_id, "expires_in": 300}


@router.get("/magic-login/status/{request_id}")
async def magic_login_status(request_id: str, session: Session):
    now = utc_now()
    result = (await session.execute(
        select(MagicLoginRequest.status, User.id, User.username, MagicLoginRequest.expires_at <= now)
        .join(User, User.id == MagicLoginRequest.user_id)
        .where(MagicLoginRequest.request_id == request_id)
    )).one_or_none()
    if result is None:
        raise HTTPException(404, "Запрос входа не найден.")

    status, user_id, username, expired = result
    if expired and status != "rejected":
        status = "rejected"
        await session.execute(update(MagicLoginRequest).where(
            MagicLoginRequest.request_id == request_id, MagicLoginRequest.status != "rejected"
        ).values(status=status, resolved_at=func.coalesce(MagicLoginRequest.resolved_at, now)))
        await session.commit()
    return {
        "status": status,
        "username": username,
        "access_token": _access_token(user_id) if status == "approved" else None,
        "token_type": "bearer" if status == "approved" else None,
    }


@router.get("/me", response_model=UserView)
async def me(user: CurrentUser):
    return user


class GitHubCredentials(BaseModel):
    token: SecretStr


class GitHubDevicePoll(BaseModel):
    device_code: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    expires_at: int
    signature: Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")]


def _github_client_id() -> str:
    client_id = os.getenv("GITHUB_CLIENT_ID", "").strip()
    if not client_id:
        raise HTTPException(503, "Укажите GITHUB_CLIENT_ID в .env и перезапустите Loomis.")
    return client_id


async def _github_oauth(path: str, data: dict[str, str]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post("https://github.com/login/" + path, data=data,
                                         headers={"Accept": "application/json"})
            result = response.json()
            if isinstance(result, dict) and (response.is_success or result.get("error")):
                return result
    except (httpx.RequestError, ValueError):
        pass
    raise HTTPException(502, "GitHub OAuth недоступен. Повторите попытку.")


def _device_signature(user_id: int, client_id: str, device_code: str, expires_at: int) -> str:
    return _sign(f"github-device:{user_id}:{client_id}:{device_code}:{expires_at}")


@router.post("/github/device/start")
async def start_github_device(user: CurrentUser):
    client_id = _github_client_id()
    result = await _github_oauth("device/code", {"client_id": client_id, "scope": "repo"})
    if result.get("error") == "device_flow_disabled":
        raise HTTPException(503, "Включите Device Flow в настройках GitHub OAuth App.")
    device_code, user_code = result.get("device_code"), result.get("user_code")
    expires_in, interval = result.get("expires_in"), result.get("interval")
    if not (isinstance(device_code, str) and 0 < len(device_code) <= 255
            and isinstance(user_code, str) and user_code
            and type(expires_in) is int and expires_in > 0
            and type(interval) is int and interval > 0):
        raise HTTPException(502, "GitHub не выдал код. Проверьте GITHUB_CLIENT_ID и настройки Device Flow.")
    expires_at = int(time.time()) + expires_in
    return {
        "device_code": device_code, "user_code": user_code, "expires_in": expires_in,
        "interval": interval, "expires_at": expires_at,
        "signature": _device_signature(user.id, client_id, device_code, expires_at),
    }


@router.post("/github/device/poll")
async def poll_github_device(data: GitHubDevicePoll, session: Session, user: CurrentUser):
    client_id = _github_client_id()
    signature = _device_signature(user.id, client_id, data.device_code, data.expires_at)
    if not hmac.compare_digest(signature, data.signature):
        raise HTTPException(400, "Запрос входа через GitHub недействителен.")
    if time.time() >= data.expires_at:
        return {"status": "expired_token"}
    result = await _github_oauth("oauth/access_token", {
        "client_id": client_id, "device_code": data.device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    error = result.get("error")
    if error in ("authorization_pending", "slow_down", "expired_token", "access_denied"):
        interval = result.get("interval")
        return {"status": error, "interval": interval if type(interval) is int and interval > 0 else None}
    token = result.get("access_token")
    if error or not isinstance(token, str) or not token.strip():
        raise HTTPException(502, "GitHub не выдал токен. Начните вход заново.")
    await bind_github(session, user, token, device_flow=True)
    return {"status": "authorized", "user": UserView.model_validate(user)}


@router.post("/github", response_model=UserView)
async def connect_github(data: GitHubCredentials, session: Session, user: CurrentUser):
    await bind_github(session, user, data.token.get_secret_value())
    return user


@router.delete("/github", response_model=UserView)
async def disconnect_github(session: Session, user: CurrentUser):
    user.github_token = None
    user.github_username = None
    user.github_avatar_url = None
    await session.commit()
    return user


class UserPreferences(BaseModel):
    tg_logs_enabled: bool | None = None
    default_projects_dir: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024,
                               pattern=r"^[^\x00-\x1f]+$")
    ] | None = None


@router.patch("/me", response_model=UserView)
async def update_preferences(data: UserPreferences, session: Session, user: CurrentUser):
    if data.default_projects_dir is not None:
        path = os.path.abspath(data.default_projects_dir)
        if len(path) > 1024 or (os.path.exists(path) and not os.path.isdir(path)):
            raise HTTPException(422, "Укажите допустимый путь к папке проектов.")
        user.default_projects_dir = path
    if data.tg_logs_enabled is not None:
        user.tg_logs_enabled = data.tg_logs_enabled
    await session.commit()
    return user


def _code_digest(code: str) -> str:
    return _sign(f"telegram-link:{code}")


@router.post("/generate-link-code")
async def generate_link_code(session: Session, user: CurrentUser):
    from backend.core.orchestrator import orchestrator

    if user.telegram_id is not None:
        raise HTTPException(409, "Telegram уже привязан.")
    bot = orchestrator.bot
    if bot is None:
        raise HTTPException(503, "Telegram-бот недоступен. Проверьте BOT_TOKEN в .env.")
    try:
        bot_username = (await bot.get_me()).username
    except TelegramAPIError:
        bot_username = None
    if not bot_username:
        raise HTTPException(503, "Не удалось получить имя Telegram-бота.")
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
            return {"code": code, "expires_in": 300,
                    "deep_link": f"https://t.me/{bot_username}?start=link_{code}"}
    raise HTTPException(503, "Не удалось создать код. Повторите запрос.")


@router.post("/telegram/unlink", response_model=UserView)
async def disconnect_telegram(session: Session, user: CurrentUser):
    if user.telegram_id is not None and not user.password_hash:
        raise HTTPException(409, "Задайте пароль в Telegram перед отвязкой аккаунта.")
    user.telegram_id = None
    user.telegram_username = None
    user.telegram_avatar_url = None
    await session.flush()
    await session.execute(delete(LinkToken).where(LinkToken.user_id == user.id))
    await session.execute(delete(MagicLoginRequest).where(MagicLoginRequest.user_id == user.id))
    await session.commit()
    return user


async def link_telegram(session: AsyncSession, code: str, telegram_id: int,
                        telegram_username: str | None = None) -> bool:
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
                .values(telegram_id=telegram_id, telegram_username=telegram_username,
                        telegram_avatar_url=None).returning(User.id)
            )
            if linked is not None:
                await session.commit()
                return True
    except IntegrityError:
        pass
    await session.rollback()
    return False
