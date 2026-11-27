import asyncio
import hashlib
import hmac
import os
import secrets
import time
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

import httpx
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, computed_field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.github import bind_github, encrypt_github_token, github_profile, github_token
from backend.database.db import get_session
from backend.database.models import DEFAULT_PROJECTS_DIR, GitHubSignUp, LinkToken, TelegramSignIn, User, utc_now
from backend.settings import ROOT, settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                          max_length=255, pattern=r"^[\w.@-]+$")]
bearer = HTTPBearer(auto_error=False)
load_dotenv(ROOT / ".env")


def _load_secret_key() -> str:
    if settings.secret_key.strip():
        return settings.secret_key
    path = ROOT / ".secret_key"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        with NamedTemporaryFile(dir=ROOT, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(secrets.token_hex(32).encode("ascii"))
                stream.close()
                with suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                stream.close()
                temporary.unlink(missing_ok=True)
        return path.read_text(encoding="ascii").strip()


SECRET_KEY = _load_secret_key()
if not SECRET_KEY:
    raise RuntimeError("Loomis .secret_key is empty")


class Credentials(BaseModel):
    username: Username
    password: str = Field(min_length=1, max_length=1024)


class SignUpCredentials(Credentials):
    password: str = Field(min_length=4, max_length=1024)


class AuthSecret(BaseModel):
    key: Annotated[str, StringConstraints(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]


class GitHubRegistration(SignUpCredentials, AuthSecret):
    pass


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    telegram_id: int | None
    tg_logs_enabled: bool
    language: Literal["ru", "en"]
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


async def set_password(user: User, password: str) -> None:
    salt = secrets.token_hex(16)
    digest = await asyncio.to_thread(_derive, password, salt)
    user.password_hash = f"{salt}:{digest}"


async def save_account(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Логин, Telegram или GitHub уже привязан к аккаунту.") from None


async def create_account(session: AsyncSession, credentials: SignUpCredentials, **fields) -> User:
    user = User(username=credentials.username, login_name=credentials.username.casefold(), **fields)
    await set_password(user, credentials.password)
    session.add(user)
    return user


def _sign(payload: str) -> str:
    return hmac.digest(SECRET_KEY.encode(), payload.encode(), "sha256").hex()


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
async def register(data: SignUpCredentials, session: Session):
    user = await create_account(session, data)
    await save_account(session)
    return user


async def authenticate(session: AsyncSession, data: Credentials) -> User:
    user = await session.scalar(select(User).where(User.login_name == data.username.casefold()))
    salt, expected = user.password_hash.split(":") if user and user.password_hash else ("0" * 32, "")
    digest = await asyncio.to_thread(_derive, data.password, salt)
    if user is None or not hmac.compare_digest(digest, expected):
        raise HTTPException(401, "Неверное имя пользователя или пароль.")
    return user


def _login_result(user_id: int) -> dict:
    return {"access_token": _access_token(user_id), "token_type": "bearer"}


@router.post("/login")
async def login(data: Credentials, session: Session):
    return _login_result((await authenticate(session, data)).id)


def _telegram_code(code: str) -> str:
    return _sign(f"telegram-auth:{code}")


@router.post("/telegram/login/start", status_code=202)
async def start_telegram_login(session: Session):
    bot_username = await _telegram_bot_username()
    code, poll_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = utc_now()
    await session.execute(delete(TelegramSignIn).where(TelegramSignIn.expires_at <= now))
    session.add(TelegramSignIn(code=_telegram_code(code), poll_key=_sign(f"telegram-poll:{poll_key}"),
                               expires_at=now + timedelta(minutes=5)))
    await session.commit()
    return {"key": poll_key, "expires_in": 300, "deep_link": f"https://t.me/{bot_username}?start=auth_{code}"}


async def claim_telegram_login(session: AsyncSession, code: str, telegram_id: int) -> bool:
    claimed = await session.scalar(update(TelegramSignIn).where(
        TelegramSignIn.code == _telegram_code(code), TelegramSignIn.status == "pending",
        TelegramSignIn.expires_at > utc_now(),
        (TelegramSignIn.telegram_id.is_(None) | (TelegramSignIn.telegram_id == telegram_id)),
    ).values(telegram_id=telegram_id).returning(TelegramSignIn.code))
    await session.commit()
    return claimed is not None


async def resolve_telegram_login(
    session: AsyncSession, code: str, telegram_id: int, user_id: int | None
) -> bool:
    resolved = await session.scalar(update(TelegramSignIn).where(
        TelegramSignIn.code == _telegram_code(code), TelegramSignIn.status == "pending",
        TelegramSignIn.expires_at > utc_now(), TelegramSignIn.telegram_id == telegram_id,
    ).values(status="approved" if user_id is not None else "rejected", user_id=user_id)
        .returning(TelegramSignIn.code))
    return resolved is not None


@router.post("/telegram/login/poll")
async def poll_telegram_login(data: AuthSecret, session: Session):
    key = _sign(f"telegram-poll:{data.key}")
    row = (await session.execute(select(TelegramSignIn.status).where(
        TelegramSignIn.poll_key == key, TelegramSignIn.expires_at > utc_now(),
    ))).one_or_none()
    if row is None:
        return {"status": "expired_token"}
    if row.status != "approved":
        return {"status": row.status}
    linked_user = select(User.id).where(
        User.id == TelegramSignIn.user_id, User.telegram_id == TelegramSignIn.telegram_id,
    ).exists()
    user_id = await session.scalar(delete(TelegramSignIn).where(
        TelegramSignIn.poll_key == key, TelegramSignIn.status == "approved",
        TelegramSignIn.expires_at > utc_now(), linked_user,
    ).returning(TelegramSignIn.user_id))
    await session.commit()
    if user_id is None:
        return {"status": "rejected"}
    return {"status": "authorized", **_login_result(user_id)}


async def bind_telegram_account(session: AsyncSession, user: User, telegram_id: int, username: str) -> None:
    linked = await session.scalar(update(User).where(
        User.id == user.id, User.telegram_id.is_(None) | (User.telegram_id == telegram_id),
    ).values(telegram_id=telegram_id, telegram_username=username).returning(User.id))
    if linked is None:
        raise HTTPException(409, "К этому аккаунту уже привязан другой Telegram.")


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


def _device_signature(owner: str, client_id: str, device_code: str, expires_at: int) -> str:
    return _sign(f"github-device:{owner}:{client_id}:{device_code}:{expires_at}")


@router.post("/github/device/start")
async def start_github_device(user: CurrentUser):
    return await request_github_device(str(user.id), repository_access=True)


async def request_github_device(owner: str, *, repository_access: bool = False):
    client_id = _github_client_id()
    result = await _github_oauth("device/code", {
        "client_id": client_id, "scope": "repo delete_repo" if repository_access else "",
    })
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
        "signature": _device_signature(owner, client_id, device_code, expires_at),
    }


@router.post("/github/device/poll")
async def poll_github_device(data: GitHubDevicePoll, session: Session, user: CurrentUser):
    result = await exchange_github_device(data, str(user.id))
    if isinstance(result, dict):
        return result
    await bind_github(session, user, result, device_flow=True)
    return {"status": "authorized", "user": UserView.model_validate(user)}


async def exchange_github_device(data: GitHubDevicePoll, owner: str) -> dict | str:
    client_id = _github_client_id()
    signature = _device_signature(owner, client_id, data.device_code, data.expires_at)
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
    return token


async def account_for_github(session: AsyncSession, account_id: int, username: str) -> User | None:
    user = await session.scalar(select(User).where(User.github_account_id == account_id).with_for_update())
    if user is not None:
        return user
    legacy = (await session.scalars(select(User).where(
        User.github_account_id.is_(None), func.lower(User.github_username) == username.lower(),
    ).with_for_update())).all()
    if not legacy:
        return None
    if len(legacy) == 1 and legacy[0].github_token:
        user = legacy[0]
        try:
            verified_id, _, _ = await github_profile(github_token(user.github_token))
        except (HTTPException, ValueError):
            pass
        else:
            if verified_id == account_id:
                user.github_account_id = account_id
                return user
    raise HTTPException(409, "Войдите по логину и паролю и перепривяжите GitHub в настройках.")


@router.post("/github/login/start")
async def start_github_login():
    return await request_github_device("desktop", repository_access=True)


@router.post("/github/login/poll")
async def poll_github_login(data: GitHubDevicePoll, session: Session):
    result = await exchange_github_device(data, "desktop")
    if isinstance(result, dict):
        return result
    account_id, username, avatar_url = await github_profile(result)
    user = await account_for_github(session, account_id, username)
    if user is not None:
        user.github_username, user.github_avatar_url = username, avatar_url
        await save_account(session)
        return {"status": "authorized", **_login_result(user.id)}

    key = secrets.token_urlsafe(32)
    now = utc_now()
    await session.execute(delete(GitHubSignUp).where(GitHubSignUp.expires_at <= now))
    session.add(GitHubSignUp(ticket=_sign(f"github-signup:{key}"), account_id=account_id,
                            username=username, avatar_url=avatar_url, encrypted_token=encrypt_github_token(result),
                            expires_at=now + timedelta(minutes=10)))
    await session.commit()
    return {"status": "signup_required", "key": key}


@router.post("/github/login/register", status_code=201)
async def register_github(data: GitHubRegistration, session: Session):
    ticket = (await session.execute(delete(GitHubSignUp).where(
        GitHubSignUp.ticket == _sign(f"github-signup:{data.key}"), GitHubSignUp.expires_at > utc_now(),
    ).returning(GitHubSignUp.account_id, GitHubSignUp.username, GitHubSignUp.avatar_url,
                GitHubSignUp.encrypted_token))).one_or_none()
    if ticket is None:
        raise HTTPException(410, "Регистрация через GitHub истекла. Начните вход заново.")
    user = await create_account(session, data, github_account_id=ticket.account_id,
                                github_username=ticket.username, github_avatar_url=ticket.avatar_url,
                                github_token=ticket.encrypted_token)
    await save_account(session)
    return _login_result(user.id)


@router.post("/github", response_model=UserView)
async def connect_github(data: GitHubCredentials, session: Session, user: CurrentUser):
    await bind_github(session, user, data.token.get_secret_value())
    return user


@router.delete("/github", response_model=UserView)
async def disconnect_github(session: Session, user: CurrentUser):
    user.github_token = None
    user.github_account_id = None
    user.github_username = None
    user.github_avatar_url = None
    await session.commit()
    return user


class UserPreferences(BaseModel):
    language: Literal["ru", "en"] | None = None
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
    if data.language is not None:
        user.language = data.language
    await session.commit()
    return user


def _code_digest(code: str) -> str:
    return _sign(f"telegram-link:{code}")


async def _telegram_bot_username() -> str:
    from backend.core.orchestrator import orchestrator

    bot = orchestrator.bot
    if bot is None:
        raise HTTPException(503, "Telegram-бот недоступен. Проверьте BOT_TOKEN в .env.")
    try:
        bot_username = (await bot.get_me()).username
    except TelegramAPIError:
        bot_username = None
    if not bot_username:
        raise HTTPException(503, "Не удалось получить имя Telegram-бота.")
    return bot_username


@router.post("/generate-link-code")
async def generate_link_code(session: Session, user: CurrentUser):
    if user.telegram_id is not None:
        raise HTTPException(409, "Telegram уже привязан.")
    bot_username = await _telegram_bot_username()
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
    await session.execute(delete(TelegramSignIn).where(TelegramSignIn.user_id == user.id))
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
