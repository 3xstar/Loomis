import os
import re
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.database.models import User

from backend.settings import ROOT
from worker.git_bridge import is_github_repo


def _cipher() -> Fernet:
    path = ROOT / ".github-token-key"
    try:
        return Fernet(path.read_bytes())
    except FileNotFoundError:
        with NamedTemporaryFile(dir=path.parent, delete=False) as key_file:
            temporary = Path(key_file.name)
            key_file.write(Fernet.generate_key())
        try:
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            temporary.unlink()
        return Fernet(path.read_bytes())


def github_token(encrypted: str) -> str:
    try:
        return _cipher().decrypt(encrypted.encode()).decode()
    except (InvalidToken, OSError, ValueError):
        raise ValueError("Не удалось прочитать токен. Привяжите GitHub заново.") from None


def github_repo_name(repo_url: str | None) -> str | None:
    url = (repo_url or "").strip().rstrip("/").removesuffix(".git")
    return url.split("/", 3)[-1] if is_github_repo(url) else None


async def _github_request(token: str, method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        return await client.request(method, "https://api.github.com" + path, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }, **kwargs)


async def delete_github_repo(token: str, repo_url: str) -> str | None:
    name = github_repo_name(repo_url)
    if not name:
        return "Адрес репозитория GitHub недействителен."
    try:
        response = await _github_request(token, "DELETE", f"/repos/{name}")
    except httpx.RequestError:
        return "Не удалось подтвердить удаление репозитория. Проверьте его в GitHub."
    if response.status_code == 204:
        return None
    if response.status_code == 403:
        return "GitHub запретил удаление. Проверьте право delete_repo или право Administration: write для токена с ограниченным доступом."
    if response.status_code == 404:
        return "Репозиторий GitHub не найден или недоступен для этого токена."
    if response.status_code == 401:
        return "GitHub отклонил токен. Привяжите GitHub заново."
    return "Не удалось подтвердить удаление репозитория. Проверьте его в GitHub."


async def _request(token: str, method: str, path: str, **kwargs) -> dict:
    try:
        response = await _github_request(token, method, path, **kwargs)
    except httpx.RequestError:
        raise HTTPException(502, "GitHub недоступен. Проверьте подключение и повторите попытку.") from None
    if response.status_code in {401, 403, 429}:
        raise HTTPException(422, "GitHub отклонил запрос. Проверьте токен, права и лимит запросов.")
    if response.status_code == 422 and method == "POST":
        raise HTTPException(409, "GitHub отклонил имя репозитория. Проверьте, свободно ли оно.")
    if response.is_success:
        with suppress(ValueError):
            data = response.json()
            if isinstance(data, dict):
                return data
    raise HTTPException(502, "GitHub вернул неожиданный ответ.")


async def github_profile(token: str) -> tuple[int, str, str | None]:
    profile = await _request(token, "GET", "/user")
    account_id, username, avatar_url = profile.get("id"), profile.get("login"), profile.get("avatar_url")
    if (type(account_id) is not int or not 0 < account_id < 2**63
            or not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", username)):
        raise HTTPException(502, "GitHub вернул неожиданный ответ.")
    return account_id, username, avatar_url if isinstance(avatar_url, str) else None


def encrypt_github_token(token: str) -> str:
    try:
        return _cipher().encrypt(token.encode()).decode()
    except (OSError, ValueError):
        raise HTTPException(503, "Не удалось безопасно сохранить токен GitHub.") from None


async def bind_github(session, user, token: str, *, device_flow: bool = False) -> None:
    token = token.strip()
    if not device_flow and not re.fullmatch(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{1,250}", token):
        raise HTTPException(422, "Введите персональный токен GitHub с префиксом ghp_ или github_pat_.")
    account_id, username, avatar_url = await github_profile(token)
    legacy_owner = await session.scalar(select(User.id).where(
        User.id != user.id, User.github_account_id.is_(None), func.lower(User.github_username) == username.lower(),
    ).limit(1))
    if legacy_owner is not None:
        raise HTTPException(409, "Этот GitHub уже привязан к аккаунту Loomis.")
    user.github_account_id, user.github_username, user.github_avatar_url = account_id, username, avatar_url
    user.github_token = encrypt_github_token(token)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Этот GitHub уже привязан к аккаунту Loomis.") from None


async def create_github_repo(user, project_name: str, visibility: str) -> str:
    if not user.github_token:
        raise HTTPException(422, "Сначала привяжите GitHub в общих настройках.")
    try:
        token = github_token(user.github_token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_name).strip(".-")[:100]
    if not name:
        raise HTTPException(422, "Для создания репозитория добавьте в название проекта латинские буквы или цифры.")
    repository = await _request(token, "POST", "/user/repos", json={
        "name": name, "private": visibility == "private",
    })
    url = repository.get("clone_url")
    if not isinstance(url, str) or not is_github_repo(url):
        raise HTTPException(502, "GitHub не вернул адрес созданного репозитория.")
    return url
