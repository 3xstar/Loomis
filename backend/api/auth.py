import asyncio
import base64
import hashlib
import json
import logging
import smtplib
import hmac
import os
import re
import secrets
import time
import unicodedata
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, StringConstraints, computed_field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.github import bind_github, encrypt_github_token, github_profile, github_token
from backend.database.db import get_session
from backend.database.models import AuthChallenge, DEFAULT_PROJECTS_DIR, GitHubSignUp, LinkToken, TelegramSignIn, User, utc_now
from backend.settings import ROOT, settings

from backend.services.email_service import send_verification, verification_hint

logger = logging.getLogger(__name__)
Session = Annotated[AsyncSession, Depends(get_session)]
USERNAME_ERROR = "Логин должен содержать от 3 до 24 латинских букв, цифр или подчёркиваний и хотя бы одну букву."
EMAIL_ERROR = "Введите корректный адрес электронной почты."
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_username(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", value) or not re.search(r"[A-Za-z]", value):
        raise ValueError(USERNAME_ERROR)
    return value


def validate_email_format(value: str) -> str:
    if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
        raise ValueError(EMAIL_ERROR)
    return value


def validate_login_identifier(value: str) -> str:
    return validate_email_format(value) if "@" in value else value


Username = Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(validate_username)]
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


LoginIdentifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
                            AfterValidator(validate_login_identifier)]
PASSWORD_HINT = 'Придумайте пароль от 10 до 64 символов.'
PASSWORD_ERRORS = {
    'length': ('Пароль должен содержать от 10 до 64 символов.', 'Password must contain 10 to 64 characters.'),
    'common': ('Пароль не должен содержать распространённые пароли или слова из списка запрещённых.', 'Password must not contain common passwords or blocklisted words.'),
    'sequence': ('Пароль не должен содержать простые последовательности (например, 1234 или qwerty).', 'Password must not contain predictable sequences.'),
    'repeat': ('Пароль не должен содержать три одинаковых символа подряд.', 'Password must not contain three identical characters in a row.'),
    'personal': ('Пароль не должен содержать логин или часть Email до знака @.', 'Password must not contain your username or the part of your email before @.'),
    'groups': ('Пароль длиной 10–14 символов должен включать минимум три группы: строчные буквы, заглавные буквы, цифры и спецсимволы.', 'A 10–14 character password must use at least three groups: lowercase letters, uppercase letters, digits, and special characters.'),
    'letter': ('Пароль длиной от 15 символов должен содержать хотя бы одну букву.', 'A password of 15 or more characters must contain at least one letter.'),
}

PASSWORD_BLOCKLIST = "password admin welcome letmein master dragon monkey loomis 1234567890".split()
PASSWORD_SEQUENCE_ROWS = (
    '0123456789',
    'abcdefghijklmnopqrstuvwxyz',
    'абвгдежзийклмнопрстуфхцчшщъыьэюя',
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    'qwertyuiop',
    'asdfghjkl',
    'zxcvbnm',
    'йцукенгшщзхъ',
    'фывапролджэ',
    'ячсмитьбю',
    '!@#$%^&*()',
)
PASSWORD_SEQUENCES = {
    row[index:index + 4]
    for forward in PASSWORD_SEQUENCE_ROWS
    for row in (forward, forward[::-1])
    for index in range(len(row) - 3)
}


class PasswordStrengthError(HTTPException):
    def __init__(self, reason: str):
        russian, self.english = PASSWORD_ERRORS[reason]
        super().__init__(422, russian)


class AuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def localized_handler(request: Request):
            try:
                return await handler(request)
            except PasswordStrengthError as exc:
                preferred = request.headers.get("Accept-Language", "").split(",")[0].split(";")[0].strip()
                if preferred.lower().split("-")[0] == "en":
                    raise HTTPException(exc.status_code, exc.english) from None
                raise

        return localized_handler


router = APIRouter(prefix="/api/auth", tags=["auth"], route_class=AuthRoute)


def validate_password_strength(password: str, username: str | None = None, email: str | None = None) -> None:
    if not 10 <= len(password) <= 64:
        raise PasswordStrengthError("length")
    lowered = "".join(char.lower() for char in password)
    if any(word in lowered for word in PASSWORD_BLOCKLIST):
        raise PasswordStrengthError("common")
    if any(lowered[index:index + 4] in PASSWORD_SEQUENCES for index in range(len(lowered) - 3)):
        raise PasswordStrengthError("sequence")
    if any(a == b == c for a, b, c in zip(lowered, lowered[1:], lowered[2:])):
        raise PasswordStrengthError("repeat")
    for personal in (username, email.rsplit("@", 1)[0] if email else None):
        if personal and "".join(char.lower() for char in personal) in lowered:
            raise PasswordStrengthError("personal")

    categories = {unicodedata.category(char) for char in password}
    if len(password) >= 15:
        if not any(category.startswith("L") for category in categories):
            raise PasswordStrengthError("letter")
    elif sum(("Ll" in categories, bool(categories & {"Lu", "Lt"}), "Nd" in categories,
              any(category.startswith(("P", "S")) for category in categories))) < 3:
        raise PasswordStrengthError("groups")


class Credentials(BaseModel):
    username: LoginIdentifier
    password: str = Field(min_length=1, max_length=1024)


class SignUpCredentials(Credentials):
    username: Username
    password: str


class OAuthRegistration(SignUpCredentials):
    pending_token: str = Field(min_length=1, max_length=4096)


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
    email: str | None
    email_verified: bool
    has_password: bool
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


async def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = await asyncio.to_thread(_derive, password, salt)
    return f"{salt}:{digest}"


async def set_password(user: User, password: str) -> None:
    user.password_hash = await password_hash(password)


async def password_matches(password: str, stored_hash: str | None) -> bool:
    salt, expected = stored_hash.split(":") if stored_hash else ("0" * 32, "")
    actual = await asyncio.to_thread(_derive, password, salt)
    return hmac.compare_digest(actual, expected)


async def change_password(session: AsyncSession, user: User, password: str, previous: str | None) -> None:
    new_hash = await password_hash(password)
    with session.no_autoflush:
        changed = await session.scalar(update(User).where(
            User.id == user.id, User.password_hash == previous,
        ).values(password_hash=new_hash).returning(User.id))
    if changed is None:
        raise HTTPException(409, "Пароль уже изменён. Повторите попытку.")


async def save_account(
    session: AsyncSession, conflict_message: str = "Логин, Email, Telegram или GitHub уже привязан к аккаунту."
) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, conflict_message) from None


async def create_account(session: AsyncSession, credentials: SignUpCredentials, **fields) -> User:
    user = User(username=credentials.username, login_name=credentials.username.casefold(), **fields)
    await set_password(user, credentials.password)
    session.add(user)
    return user


class AccountCredentialsUpdate(BaseModel):
    username: Username | None = None
    password: str | None = None
    old_password: str | None = Field(default=None, max_length=1024)


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


@router.patch("/credentials", response_model=UserView)
async def update_credentials(data: AccountCredentialsUpdate, session: Session, user: CurrentUser):
    if data.password is not None:
        validate_password_strength(data.password, data.username or user.username, user.email)
    if data.username is not None:
        user.username = data.username
        user.login_name = data.username.casefold()
    if data.password is not None:
        previous = user.password_hash
        if previous and not await password_matches(data.old_password or "", previous):
            raise HTTPException(403, "Неверный старый пароль.")
        await change_password(session, user, data.password, previous)
    if data.username is not None:
        await save_account(session, "Этот логин уже занят.")
    else:
        await save_account(session)
    return user


async def authenticate(session: AsyncSession, data: Credentials) -> User:
    login_match = User.login_name == data.username.casefold()
    users = (await session.scalars(select(User).where(
        login_match | (func.lower(User.email) == data.username.lower()),
    ).order_by(func.coalesce(login_match, False).desc(), User.id))).all()
    for user in users:
        if await password_matches(data.password, user.password_hash):
            return user
    if not users:
        await password_matches(data.password, None)
    raise HTTPException(401, "Неверный логин, Email или пароль.")


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


async def confirm_telegram_login_link(session, code, telegram_id, username, account):
    slot = _telegram_code(code)
    status = "approved" if account else "needs_registration"
    values = dict(status=status, telegram_id=telegram_id, user_id=account.id if account else None)
    if account is None:
        values["expires_at"] = utc_now() + timedelta(minutes=15)
    resolved = await session.scalar(update(TelegramSignIn).where(
        TelegramSignIn.code == slot, TelegramSignIn.status == "pending",
        TelegramSignIn.expires_at > utc_now(),
        (TelegramSignIn.telegram_id.is_(None) | (TelegramSignIn.telegram_id == telegram_id)),
    ).values(**values).returning(TelegramSignIn.code))
    if resolved is None:
        return None
    if account is None:
        await issue_oauth_registration(session, {"telegram_id": telegram_id, "telegram_username": username},
                                       slot=slot, expires_at=values["expires_at"])
    await session.commit()
    return status


async def resolve_telegram_login(session: AsyncSession, code: str, telegram_id: int, user_id: int | None) -> bool:
    resolved = await session.scalar(update(TelegramSignIn).where(
        TelegramSignIn.code == _telegram_code(code), TelegramSignIn.status == "pending",
        TelegramSignIn.expires_at > utc_now(), TelegramSignIn.telegram_id == telegram_id,
    ).values(status="rejected" if user_id is None else "approved", user_id=user_id).returning(TelegramSignIn.code))
    return resolved is not None


@router.post("/telegram/login/poll")
async def poll_telegram_login(data: AuthSecret, session: Session):
    key = _sign(f"telegram-poll:{data.key}")
    row = (await session.execute(select(TelegramSignIn.status, TelegramSignIn.code).where(
        TelegramSignIn.poll_key == key, TelegramSignIn.expires_at > utc_now(),
    ))).one_or_none()
    if row is None:
        return {"status": "expired_token"}
    if row.status == "needs_registration":
        pending = await session.scalar(select(AuthChallenge.payload).where(
            AuthChallenge.slot == row.code, AuthChallenge.purpose == "oauth", AuthChallenge.expires_at > utc_now(),
        ))
        if pending is None:
            return {"status": "expired_token"}
        return {"status": row.status, "pending_token": pending["pending_token"]}
    if row.status not in {"approved", "approved_new"}:
        return {"status": row.status}
    linked_user = select(User.id).where(
        User.id == TelegramSignIn.user_id, User.telegram_id == TelegramSignIn.telegram_id,
    ).exists()
    authorized = (await session.execute(delete(TelegramSignIn).where(
        TelegramSignIn.poll_key == key, TelegramSignIn.status.in_(("approved", "approved_new")),
        TelegramSignIn.expires_at > utc_now(), linked_user,
    ).returning(TelegramSignIn.user_id))).one_or_none()
    await session.commit()
    if authorized is None:
        return {"status": "rejected"}
    return {"status": "authorized", **_login_result(authorized.user_id)}


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
    await require_same_github(user, result)
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
    return None


OAUTH_EXPIRED = "Подтверждение регистрации истекло. Начните вход заново."


async def issue_oauth_registration(session, identity, *, slot=None, expires_at=None, **fields):
    now = utc_now()
    expires_at = expires_at or now + timedelta(minutes=15)
    claims = {**identity, "nonce": secrets.token_urlsafe(32), "exp": int(expires_at.timestamp())}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    pending_token = f"{encoded}.{_sign(f'pending-oauth:{encoded}')}"
    key_hash = challenge_key(pending_token)
    await session.execute(delete(AuthChallenge).where(AuthChallenge.expires_at <= now))
    session.add(AuthChallenge(
        slot=slot or key_hash, key_hash=key_hash, purpose="oauth",
        payload={**fields, "pending_token": pending_token},
        attempts=1, approved=True, created_at=now, expires_at=expires_at,
    ))
    return pending_token


async def finish_oauth_registration(session, data):
    validate_password_strength(data.password, data.username)
    try:
        encoded, signature = data.pending_token.split(".")
        if not hmac.compare_digest(_sign(f"pending-oauth:{encoded}"), signature):
            raise ValueError
        identity = json.loads(base64.urlsafe_b64decode(encoded))
        if identity["exp"] <= time.time():
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(410, OAUTH_EXPIRED) from None

    fields = await session.scalar(delete(AuthChallenge).where(
        AuthChallenge.key_hash == challenge_key(data.pending_token),
        AuthChallenge.purpose == "oauth", AuthChallenge.expires_at > utc_now(),
    ).returning(AuthChallenge.payload))
    if fields is None:
        raise HTTPException(410, OAUTH_EXPIRED)
    fields.pop("pending_token")
    if "github_id" in identity:
        fields.update(github_account_id=identity["github_id"], github_username=identity["github_username"])
    if "telegram_id" in identity:
        fields.update(telegram_id=identity["telegram_id"], telegram_username=identity["telegram_username"])
    user = await create_account(session, data, **fields)
    await save_account(session)
    return user


@router.post("/complete-oauth", status_code=201)
async def complete_oauth(data: OAuthRegistration, session: Session):
    return _login_result((await finish_oauth_registration(session, data)).id)


async def github_login_account(session: AsyncSession, token: str, *, telegram_identity=None):
    account_id, username, avatar_url = await github_profile(token)
    user = await account_for_github(session, account_id, username)
    if user is None:
        identity = {"github_id": account_id, "github_username": username, **(telegram_identity or {})}
        pending_token = await issue_oauth_registration(
            session, identity, github_avatar_url=avatar_url, github_token=encrypt_github_token(token),
        )
        return None, pending_token
    user.github_username, user.github_avatar_url = username, avatar_url
    return user, None


@router.post("/github/login/start")
async def start_github_login():
    return await request_github_device("desktop", repository_access=True)


@router.post("/github/login/poll")
async def poll_github_login(data: GitHubDevicePoll, session: Session):
    result = await exchange_github_device(data, "desktop")
    if isinstance(result, dict):
        return result
    user, pending_token = await github_login_account(session, result)
    await save_account(session)
    if user is None:
        return {"status": "needs_registration", "pending_token": pending_token}
    return {"status": "authorized", **_login_result(user.id)}


@router.post("/github/login/register", status_code=201)
async def register_github(data: GitHubRegistration, session: Session):
    validate_password_strength(data.password, data.username)
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
    token = data.token.get_secret_value().strip()
    if re.fullmatch(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{1,250}", token):
        await require_same_github(user, token)
    await bind_github(session, user, token)
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


EmailAddress = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
OTPCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{6}$")]
CHALLENGE_ERROR = "Код неверен, истёк или исчерпаны попытки. Запросите новый код при необходимости."
RECOVERY_ERROR = "Подтверждение недоступно или истекло. Начните заново."


class EmailInput(BaseModel):
    email: Annotated[EmailAddress, AfterValidator(validate_email_format)]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, address: str) -> str:
        address = address.lower()
        local, _, domain = address.partition("@")
        if (len(local) > 64 or ".." in local or local.startswith(".") or local.endswith(".")
                or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", domain)):
            raise ValueError(EMAIL_ERROR)
        return address


class EmailRegistration(SignUpCredentials, EmailInput):
    language: Literal["ru", "en"] = "ru"


class VerifyCode(AuthSecret):
    code: OTPCode


class RecoveryMethod(BaseModel):
    method: Literal["auto", "email", "telegram", "github"] = "auto"


class RecoveryStart(RecoveryMethod):
    identifier: LoginIdentifier

    @field_validator("identifier")
    @classmethod
    def check_recovery_email(cls, value: str, info) -> str:
        return validate_email_format(value) if info.data.get("method") == "email" else value


class RecoveryDevicePoll(GitHubDevicePoll, AuthSecret):
    pass


class RecoveryPassword(AuthSecret):
    password: str


def challenge_key(key: str) -> str:
    return _sign(f"auth-challenge:{key}")


def otp_digest(key: str, code: str) -> str:
    return _sign(f"email-otp:{key}:{code}")


async def issue_challenge(session, purpose, target, payload, *, user=None, address=None, language="ru"):
    key = secrets.token_urlsafe(32)
    key_hash = challenge_key(key)
    code = f"{secrets.randbelow(1_000_000):06d}" if address else None
    now = utc_now()
    slot = _sign(f"auth-slot:{purpose}:{target}")
    values = dict(slot=slot, key_hash=key_hash, purpose=purpose, user_id=user.id if user else None,
                  payload=payload, otp_hash=otp_digest(key, code) if code else None,
                  attempts=5, approved=False, created_at=now, expires_at=now + timedelta(minutes=10))
    await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(AuthChallenge.expires_at <= now))
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
    statement = insert(AuthChallenge).values(**values)
    issued = await session.scalar(statement.on_conflict_do_update(
        index_elements=[AuthChallenge.slot], set_=values,
        where=(AuthChallenge.created_at <= now - timedelta(seconds=60)) &
              AuthChallenge.approved.is_(False),
    ).returning(AuthChallenge.slot))
    if issued is None:
        await session.rollback()
        raise HTTPException(429, "Повторите запрос через минуту.")
    if purpose == "link":
        user.email_otp_hash = values["otp_hash"]
        user.email_otp_expires_at = values["expires_at"]
    await session.commit()
    if code:
        try:
            await send_verification(address, code, purpose, language)
        except (OSError, ValueError, smtplib.SMTPException):
            await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(AuthChallenge.key_hash == key_hash))
            if purpose == "link":
                await session.execute(update(User).where(
                    User.id == user.id, User.email_otp_hash == values["otp_hash"],
                ).values(email_otp_hash=None, email_otp_expires_at=None))
            await session.commit()
            logger.warning("Loomis OTP delivery failed")
            raise HTTPException(502, "Не удалось отправить письмо. Повторите попытку.") from None
    return key


def active_challenge(key, purpose):
    return (AuthChallenge.key_hash == challenge_key(key), AuthChallenge.purpose == purpose,
            AuthChallenge.expires_at > utc_now())


async def accept_otp(session, data, purpose, *conditions, **values):
    filters = (*active_challenge(data.key, purpose), AuthChallenge.approved.is_(False),
               AuthChallenge.attempts > 0, AuthChallenge.otp_hash.is_not(None), *conditions)
    digest = otp_digest(data.key, data.code)
    row = (await session.execute(update(AuthChallenge).execution_options(synchronize_session=False).where(
        *filters, AuthChallenge.otp_hash == digest,
    ).values(approved=True, otp_hash=None, **values).returning(
        AuthChallenge.payload, AuthChallenge.user_id,
    ))).one_or_none()
    if row is None:
        await session.execute(update(AuthChallenge).execution_options(synchronize_session=False).where(
            *filters, AuthChallenge.otp_hash != digest,
        ).values(attempts=AuthChallenge.attempts - 1))
        await session.commit()
        raise HTTPException(400, CHALLENGE_ERROR)
    return row


async def available_email(session, address, user_id=None):
    owner = await session.scalar(select(User.id).where(User.email == address))
    if owner is not None and owner != user_id:
        raise HTTPException(409, "Эта электронная почта уже привязана.")


async def begin_registration(session, data, *, telegram_id=None, telegram_name=None):
    validate_password_strength(data.password, data.username, data.email)
    login_name = data.username.casefold()
    if len(login_name) > 255:
        raise HTTPException(422, "Логин слишком длинный.")
    if await session.scalar(select(User.id).where(User.login_name == login_name)):
        raise HTTPException(409, "Логин уже занят.")
    await available_email(session, data.email)
    # Keep only the derived password in the pending registration.
    payload = dict(username=data.username, login_name=login_name, email=data.email,
                   password_hash=await password_hash(data.password), language=data.language,
                   telegram_id=str(telegram_id) if telegram_id is not None else None, telegram_username=telegram_name)
    return await issue_challenge(session, "register", data.email, payload,
                                 address=data.email, language=data.language)


async def confirm_registration(session, data, *, telegram_id=None, desktop_code=None):
    row = await accept_otp(session, data, "register",
                           AuthChallenge.payload["telegram_id"].as_string().is_(None)
                           if telegram_id is None else
                           AuthChallenge.payload["telegram_id"].as_string() == str(telegram_id))
    user = User(**{**row.payload, "telegram_id": telegram_id}, email_verified=True)
    session.add(user)
    if desktop_code:
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "Логин, Email или Telegram уже привязан к аккаунту.") from None
        if not await resolve_telegram_login(session, desktop_code, telegram_id, user.id):
            await session.rollback()
            raise HTTPException(410, RECOVERY_ERROR)
    await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(AuthChallenge.key_hash == challenge_key(data.key)))
    await save_account(session)
    return user


@router.post("/register", status_code=202)
async def register(data: EmailRegistration, session: Session):
    key = await begin_registration(session, data)
    return email_challenge_response(key)


def email_challenge_response(key: str) -> dict[str, str]:
    result = {"key": key}
    if hint := verification_hint():
        result["hint"] = hint
    return result


@router.post("/register/verify", status_code=201)
async def verify_registration(data: VerifyCode, session: Session):
    user = await confirm_registration(session, data)
    return _login_result(user.id)


@router.post("/email/start", status_code=202)
async def start_email_link(data: EmailInput, session: Session, user: CurrentUser):
    if user.email_verified:
        raise HTTPException(409, "Электронная почта уже подтверждена.")
    await available_email(session, data.email, user.id)
    key = await issue_challenge(session, "link", user.id, {"email": data.email},
                                user=user, address=data.email, language=user.language)
    return email_challenge_response(key)


@router.post("/email/verify", response_model=UserView)
async def verify_email_link(data: VerifyCode, session: Session, user: CurrentUser):
    row = await accept_otp(session, data, "link", AuthChallenge.user_id == user.id)
    try:
        changed = await session.scalar(update(User).execution_options(synchronize_session=False).where(
            User.id == user.id, User.email_verified.is_(False),
            User.email_otp_hash == otp_digest(data.key, data.code),
            User.email_otp_expires_at > utc_now(),
        ).values(email=row.payload["email"], email_verified=True, email_otp_hash=None,
                 email_otp_expires_at=None).returning(User.id))
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Эта электронная почта уже привязана.") from None
    if changed is None:
        await session.rollback()
        raise HTTPException(400, CHALLENGE_ERROR)
    await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(AuthChallenge.key_hash == challenge_key(data.key)))
    await save_account(session)
    await session.refresh(user)
    return user


@router.post("/email/unlink/start", status_code=202)
async def start_email_unlink(session: Session, user: CurrentUser):
    if not user.email_verified or not user.email:
        raise HTTPException(409, RECOVERY_ERROR)
    key = await issue_challenge(session, "unlink", user.id, {"email": user.email},
                                user=user, address=user.email, language=user.language)
    return email_challenge_response(key)


@router.post("/email/unlink/verify", response_model=UserView)
async def verify_email_unlink(data: VerifyCode, session: Session, user: CurrentUser):
    row = await accept_otp(session, data, "unlink", AuthChallenge.user_id == user.id)
    changed = await session.scalar(update(User).where(
        User.id == user.id, User.email == row.payload["email"], User.email_verified.is_(True),
    ).values(email=None, email_verified=False, email_otp_hash=None,
             email_otp_expires_at=None).returning(User.id))
    await discard_challenge(session, data.key)
    await session.commit()
    if changed is None:
        raise HTTPException(410, RECOVERY_ERROR)
    await session.refresh(user)
    return user


async def discard_challenge(session, key):
    await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(
        AuthChallenge.key_hash == challenge_key(key),
    ))


@router.post("/telegram/unlink", status_code=202)
async def start_telegram_unlink(session: Session, user: CurrentUser):
    from backend.core.orchestrator import orchestrator

    if not user.telegram_id:
        raise HTTPException(409, RECOVERY_ERROR)
    if not (user.password_hash or user.github_account_id):
        raise HTTPException(409, "Задайте пароль перед отвязкой аккаунта.")
    if orchestrator.bot is None:
        raise HTTPException(503, "Telegram-бот недоступен.")
    telegram_id, language = user.telegram_id, user.language
    key = await issue_challenge(session, "unlink_tg", user.id, {"telegram_id": str(telegram_id)}, user=user)
    english = language == "en"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm unlink" if english else "Подтвердить отвязку",
                             callback_data=f"unlink_tg:{key}"),
    ]])
    try:
        await orchestrator.bot.send_message(
            telegram_id, "Unlink Telegram from Loomis?" if english else "Отвязать Telegram от Loomis?",
            reply_markup=markup,
        )
    except TelegramAPIError as exc:
        await discard_challenge(session, key)
        await session.commit()
        raise HTTPException(502, "Не удалось отправить запрос в Telegram.") from exc
    return {"key": key}


async def resolve_telegram_unlink(session, telegram_id, key):
    owner_id = await session.scalar(delete(AuthChallenge).execution_options(synchronize_session=False).where(
        *active_challenge(key, "unlink_tg"),
        AuthChallenge.payload["telegram_id"].as_string() == str(telegram_id),
        select(User.id).where(User.id == AuthChallenge.user_id, User.telegram_id == telegram_id).exists(),
    ).returning(AuthChallenge.user_id))
    if owner_id is None:
        return False
    changed = await session.scalar(update(User).where(
        User.id == owner_id, User.telegram_id == telegram_id,
        User.password_hash.is_not(None) | User.github_account_id.is_not(None),
    ).values(telegram_id=None, telegram_username=None, telegram_avatar_url=None).returning(User.id))
    if changed is not None:
        await session.execute(delete(LinkToken).where(LinkToken.user_id == owner_id))
        await session.execute(delete(TelegramSignIn).where(TelegramSignIn.user_id == owner_id))
    await session.commit()
    return changed is not None


@router.post("/telegram/unlink/poll")
async def poll_telegram_unlink(data: AuthSecret, session: Session, user: CurrentUser):
    if user.telegram_id is None:
        return {"status": "authorized", "user": UserView.model_validate(user)}
    pending = await session.scalar(select(AuthChallenge.slot).where(
        *active_challenge(data.key, "unlink_tg"), AuthChallenge.user_id == user.id,
    ))
    return {"status": "pending" if pending else "expired_token"}


class GitHubUnlinkPassword(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


async def linked_github_id(user):
    if user.github_account_id is not None:
        return user.github_account_id
    if user.github_token:
        account_id, _, _ = await github_profile(github_token(user.github_token))
        return account_id
    return None


async def require_same_github(user, token):
    account_id = await linked_github_id(user)
    if account_id is not None:
        candidate_id, _, _ = await github_profile(token)
        if candidate_id != account_id:
            raise HTTPException(409, "Сначала подтвердите отвязку текущего GitHub.")


async def github_unlink_ready(user):
    if not user.github_username or not (user.github_account_id or user.github_token):
        raise HTTPException(409, RECOVERY_ERROR)
    if not user.telegram_id:
        raise HTTPException(409, "Задайте пароль перед отвязкой аккаунта.")
    return await linked_github_id(user)


async def remove_github(session, user):
    changed = await session.scalar(update(User).where(
        User.id == user.id, User.password_hash == user.password_hash,
        User.github_account_id == user.github_account_id, User.github_token == user.github_token,
        User.password_hash.is_not(None) | User.telegram_id.is_not(None),
    ).values(github_token=None, github_account_id=None, github_username=None,
             github_avatar_url=None).returning(User.id))
    if changed is None:
        raise HTTPException(409, RECOVERY_ERROR)
    await session.commit()
    await session.refresh(user)
    return user


@router.delete("/github", response_model=UserView)
async def disconnect_github(data: GitHubUnlinkPassword, session: Session, user: CurrentUser):
    if not user.password_hash or not await password_matches(data.password.get_secret_value(), user.password_hash):
        raise HTTPException(403, "Неверный текущий пароль.")
    return await remove_github(session, user)


@router.post("/github/unlink/start")
async def start_github_unlink(session: Session, user: CurrentUser):
    if user.password_hash:
        raise HTTPException(403, "Введите текущий пароль.")
    account_id = await github_unlink_ready(user)
    if account_id is None:
        raise HTTPException(409, RECOVERY_ERROR)
    return await request_github_device(f"unlink:{user.id}:{account_id}")


@router.post("/github/unlink/poll")
async def poll_github_unlink(data: GitHubDevicePoll, session: Session, user: CurrentUser):
    if user.password_hash:
        raise HTTPException(403, "Введите текущий пароль.")
    account_id = await github_unlink_ready(user)
    if account_id is None:
        raise HTTPException(409, RECOVERY_ERROR)
    result = await exchange_github_device(data, f"unlink:{user.id}:{account_id}")
    if isinstance(result, dict):
        return result
    confirmed_id, _, _ = await github_profile(result)
    if confirmed_id != account_id:
        return {"status": "access_denied"}
    return {"status": "authorized", "user": UserView.model_validate(await remove_github(session, user))}


async def begin_recovery(session, user, identifier, method):
    if user is None:
        name = identifier.casefold()
        if "@" in name:
            user = await session.scalar(select(User).where(User.email == name, User.email_verified.is_(True)))
        if user is None and method != "email":
            user = await session.scalar(select(User).where(User.login_name == name))
    unavailable = "Для восстановления выбранным способом нужна привязанная интеграция."
    if user is None:
        if method in {"auto", "email"}:
            return {"method": "email", "key": secrets.token_urlsafe(32)}
        raise HTTPException(409, unavailable)
    language = user.language
    methods = []
    if user.email_verified and user.email:
        methods.append("email")
    if user.telegram_id:
        methods.append("telegram")
    if user.github_account_id or (user.github_username and user.github_token):
        methods.append("github")
    if not methods or (method != "auto" and method not in methods):
        if method == "email" and identifier:
            return {"method": "email", "key": secrets.token_urlsafe(32)}
        raise HTTPException(409, unavailable)
    selected = methods[0] if method == "auto" else method
    payload = {
        "method": selected,
        "password": _sign(f"recovery-password:{user.password_hash}"),
        "email": user.email if selected == "email" else None,
        "telegram_id": str(user.telegram_id) if selected == "telegram" else None,
        "github_id": user.github_account_id if selected == "github" else None,
    }
    key = await issue_challenge(session, "reset", f"{user.id}:{selected}", payload, user=user,
                                address=payload["email"], language=language)
    try:
        result = email_challenge_response(key) if selected == "email" else {"key": key}
        result["method"] = selected
        if selected == "github":
            result.update(await request_github_device(f"reset:{challenge_key(key)}"))
        elif selected == "telegram":
            await send_recovery_telegram(int(payload["telegram_id"]), key, language)
            result["github_available"] = "github" in methods
        return result
    except (HTTPException, TelegramAPIError) as exc:
        await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(AuthChallenge.key_hash == challenge_key(key)))
        await session.commit()
        if selected == "telegram" and method == "auto" and "github" in methods:
            return await begin_recovery(session, user, identifier, "github")
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "Не удалось отправить запрос в Telegram.") from exc


async def send_recovery_telegram(telegram_id, key, language):
    from backend.bot.i18n import bot_language
    from backend.core.orchestrator import orchestrator

    if orchestrator.bot is None:
        raise HTTPException(503, "Telegram-бот недоступен.")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить сброс", callback_data=f"reset_yes:{key}"),
        InlineKeyboardButton(text="Отклонить сброс", callback_data=f"reset_no:{key}"),
    ]])
    locale = bot_language.set(language)
    try:
        await orchestrator.bot.send_message(telegram_id, "Подтвердить сброс пароля Loomis?", reply_markup=markup)
    finally:
        bot_language.reset(locale)


async def resolve_recovery_telegram(session, telegram_id, key, approved):
    # The callback carries the ticket, but only the bound Telegram identity can approve it.
    linked = select(User.id).where(User.id == AuthChallenge.user_id,
                                   User.telegram_id == telegram_id).exists()
    resolved = await session.scalar(update(AuthChallenge).where(
        *active_challenge(key, "reset"), AuthChallenge.approved.is_(False),
        AuthChallenge.attempts > 0, linked, AuthChallenge.payload["method"].as_string() == "telegram",
        AuthChallenge.payload["telegram_id"].as_string() == str(telegram_id),
    ).values(approved=approved, attempts=0 if not approved else AuthChallenge.attempts).returning(AuthChallenge.slot))
    await session.commit()
    return resolved is not None


@router.post("/recovery/start", status_code=202)
async def start_recovery(data: RecoveryStart, session: Session):
    return await begin_recovery(session, None, data.identifier, data.method)


@router.post("/recovery/account/start", status_code=202)
async def start_account_recovery(data: RecoveryMethod, session: Session, user: CurrentUser):
    return await begin_recovery(session, user, "", data.method)


@router.post("/recovery/verify")
async def verify_recovery(data: VerifyCode, session: Session):
    grant = secrets.token_urlsafe(32)
    await accept_otp(session, data, "reset", AuthChallenge.payload["method"].as_string() == "email",
                     key_hash=challenge_key(grant), expires_at=utc_now() + timedelta(minutes=10))
    await session.commit()
    return {"key": grant}


@router.post("/recovery/poll")
async def poll_recovery(data: AuthSecret, session: Session):
    row = (await session.execute(select(AuthChallenge.approved, AuthChallenge.attempts).where(
        *active_challenge(data.key, "reset"), AuthChallenge.payload["method"].as_string() == "telegram",
    ))).one_or_none()
    if row is None:
        return {"status": "expired_token"}
    if row.attempts == 0:
        return {"status": "access_denied"}
    return {"status": "authorized" if row.approved else "pending"}


@router.post("/recovery/github/poll")
async def poll_github_recovery(data: RecoveryDevicePoll, session: Session):
    filters = (*active_challenge(data.key, "reset"), AuthChallenge.payload["method"].as_string() == "github",
               AuthChallenge.approved.is_(False))
    ticket = await session.scalar(select(AuthChallenge).where(*filters))
    if ticket is None:
        return {"status": "expired_token"}
    result = await exchange_github_device(data, f"reset:{challenge_key(data.key)}")
    if isinstance(result, dict):
        return result
    account_id, username, _ = await github_profile(result)
    user = await account_for_github(session, account_id, username)
    if user is None or user.id != ticket.user_id:
        await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(*filters))
        await session.commit()
        return {"status": "access_denied"}
    grant = secrets.token_urlsafe(32)
    payload = dict(ticket.payload, github_id=account_id)
    changed = await session.scalar(update(AuthChallenge).execution_options(synchronize_session=False).where(*filters).values(
        approved=True, key_hash=challenge_key(grant), payload=payload,
        expires_at=utc_now() + timedelta(minutes=10),
    ).returning(AuthChallenge.slot))
    await session.commit()
    return {"status": "authorized", "key": grant} if changed else {"status": "expired_token"}


async def complete_recovery(session, data, *, telegram_id=None, telegram_name=None, require_user_id=None):
    conditions = [*active_challenge(data.key, "reset"), AuthChallenge.approved.is_(True)]
    if require_user_id is not None:
        conditions.append(AuthChallenge.user_id == require_user_id)
    row = (await session.execute(delete(AuthChallenge).execution_options(synchronize_session=False).where(*conditions).returning(
        AuthChallenge.user_id, AuthChallenge.payload,
    ))).one_or_none()
    if row is None:
        raise HTTPException(410, RECOVERY_ERROR)
    user = await session.get(User, row.user_id, populate_existing=True)
    payload = row.payload
    if user is None or not hmac.compare_digest(payload["password"], _sign(f"recovery-password:{user.password_hash}")):
        await session.commit()
        raise HTTPException(410, RECOVERY_ERROR)
    try:
        validate_password_strength(data.password, user.username, user.email)
    except PasswordStrengthError:
        await session.rollback()
        raise
    new_hash = await password_hash(data.password)
    telegram_proof = int(payload["telegram_id"]) if payload["telegram_id"] else None
    proofs = {
        "email": User.email_verified.is_(True) & (User.email == payload["email"]),
        "telegram": User.telegram_id == telegram_proof,
        "github": User.github_account_id == payload["github_id"],
    }
    filters = [User.id == user.id, User.password_hash == user.password_hash, proofs[payload["method"]]]
    values = {"password_hash": new_hash}
    if telegram_id is not None and user.telegram_id is None:
        filters.append(User.telegram_id.is_(None))
        values.update(telegram_id=telegram_id, telegram_username=telegram_name)
    try:
        changed = await session.scalar(update(User).where(*filters).values(**values).returning(User.id))
        await save_account(session)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Telegram уже привязан к аккаунту.") from None
    if changed is None:
        raise HTTPException(410, RECOVERY_ERROR)
    return changed


@router.post("/recovery/finish")
async def finish_recovery(data: RecoveryPassword, session: Session):
    return _login_result(await complete_recovery(session, data))


@router.post("/recovery/account/finish", response_model=UserView)
async def finish_account_recovery(data: RecoveryPassword, session: Session, user: CurrentUser):
    user_id = await complete_recovery(session, data, require_user_id=user.id)
    return await session.get_one(User, user_id, populate_existing=True)
