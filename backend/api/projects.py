import asyncio
import os
import re
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BeforeValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, computed_field, field_serializer
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from backend.api.auth import CurrentUser, Session
from backend.api.ws import manager
from backend.core.context_manager import build_spec
from backend.core.doc_parser import parse_document
from backend.core.github import create_github_repo
from backend.core.media import MAX_MEDIA_BYTES, make_media, media_spec_content
from backend.core.model_performance import model_key
from backend.core.orchestrator import count_tokens, history_visible, message_event, orchestrator, probe_agent
from backend.database.models import DEFAULT_PROJECTS_DIR, AgentConfig, AgentMessage, ModelLeaderboard, Project, ProjectMedia, ResponseCache, SpecItem, TaskLog, User, UserModelPreset, utc_now
from backend.settings import ROOT
from worker.adapters import CLI_PROVIDERS
from worker.git_bridge import GitBridge

router = APIRouter(prefix="/api/projects", tags=["projects"])
hub_router = APIRouter(prefix="/api/hub", tags=["hub"])
ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
GitRepoURL = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500,
                                               pattern=r"^[^\x00-\x1f\x7f]+$")]
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100, pattern=r"^[^\r\n]+$")]
ProviderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
AutonomyMode = Annotated[Literal["autonomous", "consultative", "step_by_step"],
                         BeforeValidator(lambda value: "consultative" if value == "with_questions" else value)]
FailoverMode = Literal["auto", "manual"]
CommitMode = Literal["sequential", "single_preset"]
AutoPushMode = Literal["after_each", "manual_finish"]
VerbosityLevel = Literal["concise", "balanced", "detailed"]


class ProjectCreate(BaseModel):
    name: ProjectName
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)
    git_repo_url: GitRepoURL | None = None

    test_command: str | None = None
    meeting_interval_minutes: int = Field(default=15, ge=1, le=1440)
    deadline_minutes: int = Field(default=10, ge=1, le=1440)
    autonomy_mode: AutonomyMode = "autonomous"
    failover_mode: FailoverMode = "auto"
    verbosity_level: VerbosityLevel = "concise"
    commit_mode: CommitMode = "sequential"
    auto_push_mode: AutoPushMode = "after_each"


class ProjectCreateRequest(ProjectCreate):
    github_visibility: Literal["private", "public"] | None = None


class ProjectPatch(BaseModel):
    name: ProjectName | None = None
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)
    git_repo_url: GitRepoURL | None = None
    github_visibility: Literal["private", "public"] | None = None

    test_command: str | None = None
    meeting_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    deadline_minutes: int | None = Field(default=None, ge=1, le=1440)
    autonomy_mode: AutonomyMode | None = None
    failover_mode: FailoverMode | None = None
    verbosity_level: VerbosityLevel | None = None
    commit_mode: CommitMode | None = None
    auto_push_mode: AutoPushMode | None = None


class ProjectView(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    status: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("workspace_path")
    def absolute_workspace(self, value: str | None) -> str:
        return os.path.abspath(value) if value else default_workspace(self)


class ProjectDetail(ProjectView):
    has_plan: bool


AgentRole = Literal["conversational", "image_gen"]


class AgentCreate(BaseModel):
    agent_role_type: AgentRole = "conversational"
    name: AgentName
    provider: ProviderName
    model: ProjectName
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


class AgentPatch(BaseModel):
    agent_role_type: AgentRole | None = None
    name: AgentName | None = None
    provider: ProviderName | None = None
    model: ProjectName | None = None
    settings: dict | None = None
    enabled: bool | None = None


class AgentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    created_at: datetime
    name: str
    provider: str
    model: str
    settings: dict
    agent_role_type: AgentRole
    enabled: bool
    total_tokens_used: int
    traffic_state: str
    last_error: str | None
    can_retry: bool

    @field_serializer("settings")
    def public_settings(self, settings: dict) -> dict:
        public = {name: value for name, value in settings.items()
                  if name not in {"api_key", "apiKey", "supports_vision", "supports_audio"}}
        public["has_api_key"] = bool(clean_api_key(settings.get("api_key") or settings.get("apiKey")))
        return public


class SpecText(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SpecItemView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str


def default_workspace(project: Project | ProjectView) -> str:
    slug = re.sub(r"[^\w-]+", "-", project.name.lower()).strip("-_")[:40] or "project"
    return str(ROOT / "workspaces" / f"{project.id}-{slug}")


def new_workspace(name: str, projects_dir: str | None = None) -> str:
    folder = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name).strip(" .")[:120].rstrip(" .") or "project"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", folder.split(".")[0], re.IGNORECASE):
        folder = "_" + folder
    return str(Path(projects_dir or DEFAULT_PROJECTS_DIR) / folder)


def require_github(user: User) -> None:
    if not (user.github_token and user.github_username):
        raise HTTPException(422, "Сначала привяжите GitHub в общих настройках.")


async def available_workspace(session: AsyncSession, project: Project, path: str) -> str:
    absolute_path = os.path.realpath(path)
    used = await session.scalars(select(Project).where(Project.id != project.id))
    if any(os.path.normcase(os.path.realpath(other.workspace_path or default_workspace(other))) ==
           os.path.normcase(absolute_path) for other in used):
        raise HTTPException(409, "Эта папка уже используется. Выберите другую папку проекта.")
    return absolute_path


async def link_workspace(path: str, project_id: int, repo_url: str) -> None:
    try:
        parsed = urlsplit(repo_url)
        if parsed.scheme.lower() in {"http", "https"} and (parsed.username is not None or parsed.password is not None):
            raise HTTPException(422, "Укажите адрес Git без учётных данных.")
        await GitBridge(Path(path), project_id).link_origin(repo_url)
    except (OSError, RuntimeError, TimeoutError, ValueError):
        raise HTTPException(422, "Не удалось настроить Git. Проверьте репозиторий и права доступа к папке.") from None


async def prepare_workspace(path: str, project_id: int, repo_url: str | None) -> str:
    try:
        absolute_path = path if os.path.isabs(path) else os.path.abspath(path)
        await asyncio.to_thread(os.makedirs, absolute_path, exist_ok=True)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Не удалось создать папку проекта. Проверьте путь и права доступа.") from exc
    if repo_url:
        await link_workspace(absolute_path, project_id, repo_url)
    return absolute_path


async def create_project(session: AsyncSession, owner_id: int, data: ProjectCreate) -> Project:
    fields = data.model_dump()
    visibility = fields.pop("github_visibility", None)
    project = Project(owner_id=owner_id, **fields)
    if visibility and project.git_repo_url:
        raise HTTPException(422, "Выберите адрес существующего репозитория или автоматическое создание.")
    owner = await session.get(User, owner_id)
    if visibility:
        require_github(owner)
    requested_path = project.workspace_path or new_workspace(project.name, owner.default_projects_dir)
    session.add(project)
    await session.flush()
    try:
        path = await available_workspace(session, project, requested_path)
        project.workspace_path = await prepare_workspace(
            path, project.id, project.git_repo_url,
        )
        if visibility:
            project.git_repo_url = await provision_project_repo(session, project, project.workspace_path,
                                                               project.name, visibility)
    except HTTPException:
        await session.rollback()
        raise
    await session.commit()
    return project


async def provision_project_repo(session, project, path, name, visibility) -> str:
    owner = await session.get(User, project.owner_id)
    try:
        await GitBridge(Path(path), project.id).initialize()
    except (OSError, RuntimeError, TimeoutError):
        raise HTTPException(422, "Не удалось подготовить Git в папке проекта.") from None
    url = await create_github_repo(owner, name, visibility)
    try:
        await link_workspace(path, project.id, url)
    except HTTPException:
        raise HTTPException(422, f"GitHub создал репозиторий {url}. Проверьте папку и привяжите этот адрес вручную.") from None
    return url


async def owned_project(project_id: int, session: Session, user: CurrentUser) -> Project:
    project = await session.scalar(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    if project is None:
        raise HTTPException(404, "Проект не найден.")
    return project


OwnedProject = Annotated[Project, Depends(owned_project)]


@router.get("", response_model=list[ProjectView])
async def list_projects(session: Session, user: CurrentUser):
    return list(await session.scalars(
        select(Project).where(Project.owner_id == user.id).order_by(Project.id)
    ))


@router.post("", response_model=ProjectView, status_code=201)
async def add_project(data: ProjectCreateRequest, session: Session, user: CurrentUser):
    if data.git_repo_url:
        require_github(user)
    return await create_project(session, user.id, data)


@router.get("/dashboard-usage")
async def dashboard_usage(session: Session, user: CurrentUser):
    project_ids = select(Project.id).where(Project.owner_id == user.id)
    logs = (select(TaskLog.agent_id, func.sum(TaskLog.tokens).label("tokens"))
            .where(TaskLog.project_id.in_(project_ids)).group_by(TaskLog.agent_id).subquery())
    logged = func.coalesce(logs.c.tokens, 0)
    agent_tokens = await session.scalar(select(func.coalesce(func.sum(case(
        (AgentConfig.total_tokens_used > logged, AgentConfig.total_tokens_used), else_=logged,
    )), 0)).outerjoin(logs, logs.c.agent_id == AgentConfig.id).where(AgentConfig.project_id.in_(project_ids)))
    unassigned_tokens = await session.scalar(select(logged).where(logs.c.agent_id.is_(None))) or 0
    tokens_saved = await session.scalar(select(func.coalesce(func.sum(ResponseCache.tokens_saved), 0)))
    return {"total_tokens": agent_tokens + unassigned_tokens, "tokens_saved": tokens_saved}


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project: OwnedProject):
    return ProjectDetail(**ProjectView.model_validate(project).model_dump(), has_plan=bool(project.plan_content))


@router.post("/{project_id}/open-folder", status_code=204)
async def open_project_folder(project: OwnedProject):
    path = Path(project.workspace_path or default_workspace(project)).absolute()
    if not await asyncio.to_thread(path.is_dir):
        raise HTTPException(404, "Папка проекта не найдена.")
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(os.startfile, str(path))
        else:
            command = "open" if sys.platform == "darwin" else "xdg-open"
            await asyncio.to_thread(subprocess.run, [command, str(path)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(500, "Не удалось открыть папку проекта.") from exc


@router.get("/{project_id}/spec-items", response_model=list[SpecItemView])
async def list_spec_items(project: OwnedProject, session: Session):
    return list(await session.scalars(
        select(SpecItem).where(SpecItem.project_id == project.id).order_by(SpecItem.position, SpecItem.id)
    ))


async def read_spec_item(request: Request) -> tuple[str, str]:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type == "application/json":
        try:
            return "Текст", SpecText.model_validate_json(await request.body()).text
        except ValidationError as exc:
            raise HTTPException(422, "Введите текст ТЗ.") from exc
    if media_type != "multipart/form-data":
        raise HTTPException(415, "Передайте JSON или документ.")
    async with request.form(max_files=1, max_fields=0) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise HTTPException(422, "Прикрепите документ в поле file.")
        title = Path(upload.filename.replace("\\", "/")).name
        suffix = Path(title).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md"}:
            raise HTTPException(415, "Поддерживаются документы .pdf, .docx, .txt и .md.")
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"upload{suffix}"
            with path.open("wb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    stream.write(chunk)
            try:
                content = await parse_document(path)
            except Exception as exc:
                raise HTTPException(422, "Не удалось прочитать документ.") from exc
        if not content:
            raise HTTPException(422, "В документе не найден текст.")
        return title[:500], content


async def require_draft(session: Session, project: Project) -> None:
    await session.refresh(project)
    if project.status != "draft":
        raise HTTPException(409, "Буфер ТЗ доступен в черновике.")


@router.post("/{project_id}/spec-items", response_model=SpecItemView, status_code=201)
async def add_spec_item(request: Request, project: OwnedProject, session: Session):
    await require_draft(session, project)
    title, content = await read_spec_item(request)
    async with orchestrator.runtime(project.id).boundary:
        await require_draft(session, project)
        position = await session.scalar(
            select(func.max(SpecItem.position)).where(SpecItem.project_id == project.id)
        )
        item = SpecItem(project_id=project.id, title=title, content=content, position=(position or 0) + 1)
        session.add(item)
        await session.commit()
    return item


@router.patch("/{project_id}", response_model=ProjectView)
async def update_project(data: ProjectPatch, project: OwnedProject, session: Session, user: CurrentUser):
    if data.github_visibility or data.git_repo_url:
        require_github(user)
    return await edit_project(data, project, session)


async def edit_project(data: ProjectPatch, project: Project, session: AsyncSession):
    updates = data.model_dump(exclude_unset=True)
    visibility = updates.pop("github_visibility", None)
    if visibility and updates.get("git_repo_url"):
        raise HTTPException(422, "Выберите адрес существующего репозитория или автоматическое создание.")
    required = {
        "name", "deadline_minutes", "meeting_interval_minutes", "autonomy_mode", "failover_mode",
        "verbosity_level", "commit_mode", "auto_push_mode",
    }
    if any(field in required and value is None for field, value in updates.items()):
        raise HTTPException(422, "Обязательные настройки проекта не могут быть пустыми.")
    configure_workspace = bool(visibility or updates.keys() & {"workspace_path", "git_repo_url"})
    boundary = orchestrator.runtime(project.id).boundary if configure_workspace else nullcontext()
    async with boundary:
        await session.refresh(project)
        if project.status in {"running", "council"} and "workspace_path" in updates:
            raise HTTPException(409, "Сначала приостановите проект.")
        if configure_workspace:
            path = updates.get("workspace_path", project.workspace_path) or default_workspace(project)
            if "workspace_path" in updates:
                path = await available_workspace(session, project, path)
            repo_url = None if visibility else updates.get("git_repo_url", project.git_repo_url)
            updates["workspace_path"] = await prepare_workspace(path, project.id, repo_url)
            if visibility:
                updates["git_repo_url"] = await provision_project_repo(
                    session, project, updates["workspace_path"], updates.get("name", project.name), visibility,
                )
        workspace_changed = updates.get("workspace_path", project.workspace_path) != project.workspace_path
        for field, value in updates.items():
            setattr(project, field, value)
        if workspace_changed:
            orchestrator.invalidate_plan(project)
        await session.commit()
        await session.refresh(project)
        if workspace_changed:
            await orchestrator.plan_invalidated(project)
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(project: OwnedProject, session: Session):
    await orchestrator.stop(project.id)
    await session.delete(project)
    await session.commit()
    await manager.close_project(project.id)


async def owned_agent(agent_id: int, project: OwnedProject, session: Session) -> AgentConfig:
    agent = await session.scalar(
        select(AgentConfig).where(AgentConfig.id == agent_id, AgentConfig.project_id == project.id)
    )
    if agent is None:
        raise HTTPException(404, "Агент не найден.")
    return agent


OwnedAgent = Annotated[AgentConfig, Depends(owned_agent)]


@router.get("/{project_id}/agents", response_model=list[AgentView])
async def list_agents(project: OwnedProject, session: Session):
    return list(await session.scalars(
        select(AgentConfig).where(AgentConfig.project_id == project.id).order_by(AgentConfig.id)
    ))


async def save_agent(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Не удалось сохранить агента. Проверьте имя и проект.") from None


def setting_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def clean_api_key(value: object) -> str:
    return re.sub(r"^(?:Bearer(?:\s+|$))+", "", setting_text(value), flags=re.IGNORECASE).strip()


def invalid_agent_field(field: str, message: str) -> None:
    raise HTTPException(422, [{"loc": ["body", field], "msg": message}])


def validate_agent_settings(provider: str, settings: dict, model: str) -> dict:
    config = settings.copy()
    config.pop("has_api_key", None)
    config.pop("supports_vision", None)
    config.pop("supports_audio", None)
    if provider.lower() in CLI_PROVIDERS:
        command = setting_text(config.get("command"))
        if not command:
            invalid_agent_field("command", "Введите команду запуска.")
        config["command"] = command
        return config

    base_url = setting_text(config.pop("base_url", config.pop("baseUrl", None)))
    api_key = clean_api_key(config.pop("api_key", config.pop("apiKey", None)))
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        valid_url = (
            base_url.startswith(("http://", "https://"))
            and parsed.hostname
            and (port is None or port > 0)
            and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in base_url)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        invalid_agent_field("base_url", "Введите корректный Base URL с http:// или https://.")
    if not api_key:
        invalid_agent_field("api_key", "Введите API Key.")
    config["base_url"] = base_url
    config["api_key"] = api_key
    config["model"] = model
    return config


async def test_config(project_id, agent):
    try:
        return await probe_agent(project_id, agent)
    except TimeoutError as exc:
        seconds = 90 if agent.agent_role_type == "image_gen" else 15
        raise HTTPException(400, f"Таймаут проверки агента ({seconds} секунд).") from exc
    except Exception as exc:
        raise HTTPException(400, str(exc) or type(exc).__name__) from exc


class ExistingAgentTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int


class AgentCandidateTest(AgentCreate):
    name: AgentName = "Проверка"
    agent_id: int | None = None


@router.post("/{project_id}/agents", response_model=AgentView, status_code=201)
async def add_agent(data: AgentCreate, project: OwnedProject, session: Session):
    agent = AgentConfig(project_id=project.id, **data.model_dump())
    agent.settings = validate_agent_settings(agent.provider, agent.settings, agent.model)
    return await persist_new_agent(agent, project, session)


async def persist_new_agent(agent, project, session):
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        tokens = await test_config(project.id, agent)
        agent.tested_at = utc_now()
        session.add(agent)
        agent.total_tokens_used = tokens
        await count_tokens(session, project.owner_id, agent, tokens)
        if agent.enabled:
            orchestrator.invalidate_plan(project)
        await save_agent(session)
        if agent.enabled:
            await orchestrator.plan_invalidated(project)
        await orchestrator.publish_traffic(agent)
    return agent


@router.post("/{project_id}/agents/test")
async def test_agent(data: AgentCandidateTest | ExistingAgentTest, project: OwnedProject, session: Session):
    if isinstance(data, AgentCandidateTest):
        agent = AgentConfig(project_id=project.id, **data.model_dump(exclude={"agent_id"}))
        existing_settings = {}
        if data.agent_id is not None:
            existing = await owned_agent(data.agent_id, project, session)
            existing_settings = existing.settings
        agent.settings = merged_agent_settings(agent.provider, agent.model, existing_settings, agent.settings)
        if data.agent_id is not None and existing.provider == agent.provider and existing.model == agent.model:
            agent.id = existing.id
            agent.preset_id = existing.preset_id
        tokens = await test_config(project.id, agent)
        await count_tokens(session, project.owner_id, agent, tokens)
        await session.commit()
        return {"status": "success"}
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        agent = await owned_agent(data.agent_id, project, session)
        try:
            tokens = await test_config(project.id, agent)
        except HTTPException as exc:
            if agent.traffic_state != "asking":
                await orchestrator.traffic(agent, "error", exc.detail)
            raise
        if agent.traffic_state != "asking":
            agent.traffic_state = "waiting"
        agent.tested_at = utc_now()
        agent.cooldown_until = None
        await count_tokens(session, project.owner_id, agent, tokens)
        await session.commit()
        await orchestrator.publish_traffic(agent)
    return {"status": "success"}


async def require_agent_changes(project, session):
    await session.refresh(project)
    if project.status == "council":
        raise HTTPException(409, "Дождитесь завершения консилиума.")


@router.get("/{project_id}/agents/{agent_id}", response_model=AgentView)
async def get_agent(agent: OwnedAgent):
    return agent


@router.patch("/{project_id}/agents/{agent_id}", response_model=AgentView)
async def edit_agent(data: AgentPatch, project: OwnedProject, agent: OwnedAgent, session: Session):
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        agent = await owned_agent(agent.id, project, session)
        await session.refresh(agent)
        return await update_agent(data, project, agent, session)


async def update_agent(data, project, agent, session):
    updates = data.model_dump(exclude_unset=True)
    if any(value is None for value in updates.values()):
        raise HTTPException(422, "Поля агента не могут быть пустыми.")
    provider = updates.get("provider", agent.provider)
    if len(updates.get("name", agent.name).strip()) < 2:
        invalid_agent_field("name", "Имя агента должно содержать не менее двух символов.")
    model = updates.get("model", agent.model)
    if not model.strip():
        invalid_agent_field("model", "Введите имя модели.")
    updates["settings"] = merged_agent_settings(provider, model, agent.settings, updates.get("settings", {}))
    candidate = AgentConfig(
        name=updates.get("name", agent.name), provider=provider, model=model, settings=updates["settings"],
        agent_role_type=updates.get("agent_role_type", agent.agent_role_type),
    )
    same_model = (provider, model) == (agent.provider, agent.model)
    if same_model:
        candidate.preset_id = agent.preset_id
    tokens = await test_config(project.id, candidate)
    await count_tokens(session, project.owner_id, candidate, tokens)
    if not same_model:
        agent.preset_id = None
    agent.total_tokens_used += tokens
    active_roster_changed = (agent.enabled or updates.get("enabled", False)) and any(
        getattr(agent, field) != value for field, value in updates.items()
    )
    if active_roster_changed:
        orchestrator.invalidate_plan(project)
    for field, value in updates.items():
        setattr(agent, field, value)
    agent.tested_at = utc_now()
    agent.cooldown_until = None
    if agent.traffic_state != "asking" or active_roster_changed:
        agent.traffic_state = "waiting"
    await save_agent(session)
    if active_roster_changed:
        await orchestrator.plan_invalidated(project)
    await orchestrator.publish_traffic(agent)
    return agent


def merged_agent_settings(provider, model, existing, supplied):
    settings = {**existing, **supplied}
    for alias, field in (("baseUrl", "base_url"), ("apiKey", "api_key")):
        if alias in supplied and field not in supplied:
            settings[field] = supplied[alias]
    return validate_agent_settings(provider, settings, model)


@router.delete("/{project_id}/agents/{agent_id}", status_code=204)
async def delete_agent(project: OwnedProject, agent: OwnedAgent, session: Session):
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        agent = await owned_agent(agent.id, project, session)
        await session.refresh(agent)
        if agent.enabled:
            orchestrator.invalidate_plan(project)
        for model in (AgentMessage, TaskLog):
            legacy = model.sender_name == agent.name
            if model is AgentMessage:
                legacy = legacy | (model.agent_id.is_(None) & (model.target_name == f"@{agent.name}"))
            await session.execute(delete(model).where(
                model.project_id == project.id, or_(model.agent_id == agent.id, legacy)
            ))
        await session.delete(agent)
        await session.commit()
        if agent.enabled:
            await orchestrator.plan_invalidated(project)



class ClearView(BaseModel):
    channel: Literal["team_chat", "direct", "terminal"]
    agent_id: int | None = None


@router.get("/{project_id}/history")
async def project_history(project: OwnedProject, session: Session):
    messages = (await session.execute(
        select(AgentMessage, AgentConfig.name).outerjoin(AgentConfig, AgentMessage.agent_id == AgentConfig.id)
        .where(AgentMessage.project_id == project.id, AgentMessage.is_ui_cleared.is_(False),
               history_visible(project, AgentMessage)).order_by(AgentMessage.id)
    )).all()
    logs = list(await session.scalars(select(TaskLog).where(
        TaskLog.project_id == project.id, TaskLog.is_ui_cleared.is_(False), history_visible(project, TaskLog)
    ).order_by(TaskLog.id)))
    return {"messages": [message_event(row, name) for row, name in messages], "task_logs": logs}


@router.post("/{project_id}/clear-view", status_code=204)
async def clear_view(project: OwnedProject, data: ClearView, session: Session):
    agent = await owned_agent(data.agent_id, project, session) if data.agent_id is not None else None
    for model in ((AgentMessage, TaskLog) if data.channel == "terminal" else (AgentMessage,)):
        filters = [model.project_id == project.id, model.is_ui_cleared.is_(False)]
        if model is AgentMessage:
            filters.append(model.channel == data.channel)
        if agent is not None:
            belongs_to_agent = model.agent_id == agent.id
            if model is AgentMessage and data.channel == "direct":
                belongs_to_agent = or_(belongs_to_agent, model.agent_id.is_(None) & (model.target_name == f"@{agent.name}"))
            filters.append(belongs_to_agent)
        await session.execute(update(model).where(*filters).values(is_ui_cleared=True))
    await session.commit()
    await manager.broadcast(project.id, {"type": "history_changed", "project_id": project.id})


@router.get("/{project_id}/commits")
async def project_commits(project: OwnedProject):
    try:
        return await orchestrator.commits(project.id)
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/commits/{commit_hash}/rollback")
async def rollback_commit(project: OwnedProject, commit_hash: str):
    try:
        return await orchestrator.rollback_commit(project.id, commit_hash)
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/push", status_code=204)
async def push_project(project: OwnedProject):
    try:
        await orchestrator.push_project(project.id)
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/agents/{agent_id}/retry", status_code=202)
async def retry_agent(project: OwnedProject, agent: OwnedAgent):
    try:
        return {"status": await orchestrator.retry(project.id, agent.id)}
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(409, str(exc)) from exc


class ProjectAction(BaseModel):
    action: Literal["build_spec", "council", "regenerate_plan", "approve", "pause", "resume", "rollback"]


class AgentQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=32000)


class ChatMessage(AgentQuestion):
    target_agent_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)


class CouncilFeedback(BaseModel):
    target_agent_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)
    feedback: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)]


class ApprovalDecision(BaseModel):
    approved: bool


@router.post("/{project_id}/actions")
async def project_action(project: OwnedProject, data: ProjectAction, session: Session):
    try:
        if data.action == "build_spec":
            async with orchestrator.runtime(project.id).boundary:
                await require_draft(session, project)
                has_items = await session.scalar(select(SpecItem.id).where(SpecItem.project_id == project.id).limit(1))
                if has_items is None:
                    raise HTTPException(409, "Добавьте хотя бы одну запись в буфер ТЗ.")
                if not project.workspace_path:
                    project.workspace_path = default_workspace(project)
                await build_spec(session, project)
                project.status = "planning"
                await session.commit()
                await manager.publish_status(project.id, project.status)
            return {"status": project.status}
        return {"status": await orchestrator.action(project.id, data.action)}
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/agents/{agent_id}/question")
async def agent_question(project: OwnedProject, agent: OwnedAgent, data: ChatMessage):
    try:
        if data.target_agent_ids is not None:
            return {"responses": await orchestrator.chat(project.id, data.question, data.target_agent_ids)}
        return await orchestrator.question(project.id, agent.id, data.question)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/council/feedback")
async def council_feedback(project: OwnedProject, data: CouncilFeedback):
    try:
        responses = await orchestrator.chat(project.id, data.feedback, media=None)
        updated, _ = await orchestrator.load(project.id)
        return {"status": updated.status, "responses": responses}
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/approvals/{key}")
async def command_approval(project: OwnedProject, key: str, data: ApprovalDecision):
    try:
        orchestrator.decide(project.id, key, data.approved)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"approved": data.approved}


class PresetConfig(BaseModel):
    model: ProjectName
    base_url: str | None = None
    api_key: str | None = None
    command_template: str | None = None
    question_command: str | None = None


class PresetCreate(BaseModel):
    agent_role_type: AgentRole = "conversational"
    name: AgentName
    adapter_type: ProviderName
    config_json: PresetConfig

    def agent_settings(self):
        if self.agent_role_type == "image_gen" and self.adapter_type.lower() in CLI_PROVIDERS:
            invalid_agent_field("agent_role_type", "Генератор изображений требует OpenAI-совместимый API.")
        config = self.config_json.model_dump(exclude_none=True)
        if self.adapter_type.lower() in CLI_PROVIDERS:
            if command := config.pop("command_template", None):
                config["command"] = command
            elif self.adapter_type.lower() == "codex":
                config["command"] = "codex exec --json --sandbox workspace-write -"
        return validate_agent_settings(self.adapter_type, config, self.config_json.model)


class PresetPatch(BaseModel):
    agent_role_type: AgentRole | None = None
    name: AgentName | None = None
    adapter_type: ProviderName | None = None
    config_json: dict | None = None


class PresetView(PresetCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    created_at: datetime
    total_tokens_used: int

    @field_serializer("config_json")
    def public_config(self, config: PresetConfig):
        return {
            **config.model_dump(exclude={"api_key"}, exclude_none=True),
            "has_api_key": bool(clean_api_key(config.api_key)),
        }

    @computed_field
    @property
    def model_key(self) -> str:
        return model_key(self.adapter_type, self.config_json.model, self.config_json.model_dump())


class LeaderboardEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_key: str
    model_name: str
    success_count: int
    error_count: int
    avg_latency_ms: float
    reliability_score: float
    updated_at: datetime


@hub_router.get("/leaderboard", response_model=list[LeaderboardEntry])
async def get_leaderboard(session: Session, user: CurrentUser):
    row = ModelLeaderboard
    return list(await session.scalars(select(row).order_by(
        row.reliability_score.desc(), (row.success_count == 0), row.avg_latency_ms, row.model_key,
    )))


@hub_router.get("/models", response_model=list[PresetView])
async def list_presets(session: Session, user: CurrentUser):
    return list(await session.scalars(select(UserModelPreset).where(
        UserModelPreset.owner_id == user.id
    ).order_by(UserModelPreset.id)))


@hub_router.post("/models", response_model=PresetView, status_code=201)
async def create_preset(data: PresetCreate, session: Session, user: CurrentUser):
    data.agent_settings()
    preset = UserModelPreset(owner_id=user.id, **data.model_dump())
    session.add(preset)
    await session.commit()
    return preset


@hub_router.patch("/models/{preset_id}", response_model=PresetView)
async def edit_preset(preset_id: int, data: PresetPatch, session: Session, user: CurrentUser):
    preset = await session.scalar(select(UserModelPreset).where(
        UserModelPreset.owner_id == user.id, UserModelPreset.id == preset_id
    ).with_for_update())
    if preset is None:
        raise HTTPException(404, "Пресет не найден.")
    try:
        candidate = PresetCreate(
            name=data.name or preset.name,
            agent_role_type=data.agent_role_type or preset.agent_role_type,
            adapter_type=data.adapter_type or preset.adapter_type,
            config_json={**preset.config_json, **(data.config_json or {})},
        )
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_context=False, include_input=False)) from exc
    candidate.agent_settings()
    preset.name = candidate.name
    preset.agent_role_type = candidate.agent_role_type
    preset.adapter_type = candidate.adapter_type
    preset.config_json = candidate.config_json.model_dump()
    await session.commit()
    return preset


@hub_router.delete("/models/{preset_id}", status_code=204)
async def delete_preset(preset_id: int, session: Session, user: CurrentUser):
    preset = await session.scalar(select(UserModelPreset).where(
        UserModelPreset.owner_id == user.id, UserModelPreset.id == preset_id
    ))
    if preset is None:
        raise HTTPException(404, "Пресет не найден.")
    await session.delete(preset)
    await session.commit()


class HubImport(BaseModel):
    preset_ids: list[int] = Field(min_length=1, max_length=100)


@router.post("/{project_id}/agents/from-hub", response_model=list[AgentView], status_code=201)
async def import_presets(data: HubImport, project: OwnedProject, session: Session):
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        ids = list(dict.fromkeys(data.preset_ids))
        presets = {row.id: row for row in await session.scalars(select(UserModelPreset).where(
            UserModelPreset.owner_id == project.owner_id, UserModelPreset.id.in_(ids)
        ))}
        if len(presets) != len(ids):
            raise HTTPException(404, "Один или несколько пресетов не найдены.")
        agents = []
        names = set(await session.scalars(select(AgentConfig.name).where(AgentConfig.project_id == project.id)))
        for preset_id in ids:
            preset = PresetView.model_validate(presets[preset_id])
            if preset.name in names:
                raise HTTPException(409, f"Агент {preset.name} уже существует.")
            names.add(preset.name)
            agents.append(AgentConfig(
                project_id=project.id, name=preset.name, provider=preset.adapter_type,
                model=preset.config_json.model, settings=preset.agent_settings(), preset_id=preset_id,
                agent_role_type=preset.agent_role_type,
            ))
        for agent in agents:
            agent.total_tokens_used = await test_config(project.id, agent)
            agent.tested_at = utc_now()
        for agent in agents:
            await count_tokens(session, project.owner_id, agent, agent.total_tokens_used)
        session.add_all(agents)
        orchestrator.invalidate_plan(project)
        await save_agent(session)
        await orchestrator.plan_invalidated(project)
        for agent in agents:
            await orchestrator.publish_traffic(agent)
    return agents


@router.get("/{project_id}/stats")
async def project_stats(project: OwnedProject, session: Session):
    logs = (select(
        TaskLog.agent_id, func.sum(TaskLog.tokens).label("tokens"),
        func.sum(case((or_(
            TaskLog.operation == "completed_step",
            (TaskLog.operation == "execute") & (TaskLog.status == "success"),
        ), 1), else_=0)).label("completed"),
    ).where(TaskLog.project_id == project.id, history_visible(project, TaskLog))
        .group_by(TaskLog.agent_id).subquery())
    logged_tokens = func.coalesce(logs.c.tokens, 0)
    rows = (await session.execute(select(
        AgentConfig.id, AgentConfig.name,
        case((AgentConfig.total_tokens_used > logged_tokens, AgentConfig.total_tokens_used), else_=logged_tokens),
        func.coalesce(logs.c.completed, 0),
    ).outerjoin(logs, logs.c.agent_id == AgentConfig.id).where(
        AgentConfig.project_id == project.id
    ).order_by(AgentConfig.id))).all()
    unassigned_tokens = await session.scalar(select(func.coalesce(func.sum(logs.c.tokens), 0)).where(
        logs.c.agent_id.is_(None)
    ))
    total = unassigned_tokens + sum(row[2] for row in rows)
    completed = sum(row[3] for row in rows)
    return {
        "total_tokens": total,
        "agents": [
            {"agent_id": agent_id, "name": name, "tokens": tokens, "completed_tasks": tasks,
             "contribution_percent": round(100 * tasks / completed, 2) if completed else 0}
            for agent_id, name, tokens, tasks in rows
        ],
    }


@router.post("/{project_id}/chat")
async def project_chat(project: OwnedProject, data: ChatMessage):
    try:
        return {"responses": await orchestrator.chat(project.id, data.question, data.target_agent_ids)}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


async def store_spec_media(session, project, media):
    await require_draft(session, project)
    content = await media_spec_content(media)
    async with orchestrator.runtime(project.id).boundary:
        await require_draft(session, project)
        position = await session.scalar(select(func.max(SpecItem.position)).where(SpecItem.project_id == project.id))
        item = SpecItem(project_id=project.id, title=media.filename, content=content, position=(position or 0) + 1)
        session.add(item)
        await session.flush()
        media.spec_item_id = item.id
        session.add(media)
        await session.commit()
    await manager.publish_log(project.id, "Добавлен медиафайл в буфер ТЗ.")
    return item


@router.post("/{project_id}/media", status_code=201)
async def upload_media(request: Request, project: OwnedProject, session: Session):
    async with request.form(max_files=1, max_fields=104) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise HTTPException(422, "Прикрепите медиафайл в поле file.")
        channel = form.get("channel", "spec" if project.status == "draft" else "team_chat")
        caption = form.get("caption", "")
        raw_ids = form.getlist("target_agent_ids")
        if not isinstance(caption, str) or len(caption) > 32000:
            raise HTTPException(422, "Подпись должна содержать до 32000 символов.")
        try:
            targets = [int(value) for value in raw_ids] if raw_ids else None
            media = make_media(project.id, upload.filename, await upload.read(MAX_MEDIA_BYTES + 1))
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
    if channel == "spec":
        try:
            item = await store_spec_media(session, project, media)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, "Не удалось обработать медиафайл.") from exc
        return {"id": media.id, "spec_item_id": item.id}
    if channel not in {"team_chat", "direct"} or (channel == "direct" and (not targets or len(targets) != 1)):
        raise HTTPException(422, "Укажите channel=spec, team_chat или direct с одним target_agent_ids.")
    prompt = f"{caption.strip() or 'Проанализируй вложение.'}\n\nФайл: {media.filename}"
    try:
        if channel == "direct" and targets and len(targets) == 1:
            result = await orchestrator.question(project.id, targets[0], prompt, media=media)
        else:
            result = {"responses": await orchestrator.chat(project.id, prompt, targets, media=media)}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": media.id, **result}


@router.get("/{project_id}/media/{media_id}")
async def get_media(media_id: int, project: OwnedProject, session: Session):
    media = await session.scalar(select(ProjectMedia).where(
        ProjectMedia.id == media_id, ProjectMedia.project_id == project.id
    ))
    if media is None:
        raise HTTPException(404, "Медиафайл не найден.")
    return Response(media.data, media_type=media.mime_type, headers={"X-Content-Type-Options": "nosniff"})
