import re
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_serializer
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from backend.api.auth import CurrentUser, Session
from backend.api.ws import manager
from backend.core.context_manager import build_spec
from backend.core.doc_parser import parse_document
from backend.core.media import MAX_MEDIA_BYTES, make_media, media_spec_content
from backend.core.orchestrator import orchestrator, probe_agent
from backend.database.models import AgentConfig, AgentMessage, Project, ProjectMedia, SpecItem, TaskLog, UserModelPreset, utc_now
from backend.settings import ROOT
from worker.adapters import CLI_PROVIDERS

router = APIRouter(prefix="/api/projects", tags=["projects"])
hub_router = APIRouter(prefix="/api/hub", tags=["hub"])
ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100, pattern=r"^[^\r\n]+$")]
ProviderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
AutonomyMode = Literal["autonomous", "consultative", "step_by_step"]
FailoverMode = Literal["auto", "manual"]
VerbosityLevel = Literal["concise", "balanced", "detailed"]


class ProjectCreate(BaseModel):
    name: ProjectName
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)

    test_command: str | None = None
    meeting_interval_minutes: int = Field(default=15, ge=1, le=1440)
    deadline_minutes: int = Field(default=10, ge=1, le=1440)
    autonomy_mode: AutonomyMode = "autonomous"
    failover_mode: FailoverMode = "auto"
    verbosity_level: VerbosityLevel = "concise"


class ProjectPatch(BaseModel):
    name: ProjectName | None = None
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)

    test_command: str | None = None
    meeting_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    deadline_minutes: int | None = Field(default=None, ge=1, le=1440)
    autonomy_mode: AutonomyMode | None = None
    failover_mode: FailoverMode | None = None
    verbosity_level: VerbosityLevel | None = None


class ProjectView(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    status: str
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: AgentName
    provider: ProviderName
    model: ProjectName
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


class AgentPatch(BaseModel):
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
    enabled: bool
    traffic_state: str
    last_error: str | None

    @field_serializer("settings")
    def public_settings(self, settings: dict) -> dict:
        public = {name: value for name, value in settings.items() if name not in {"api_key", "apiKey"}}
        public["has_api_key"] = bool(clean_api_key(settings.get("api_key") or settings.get("apiKey")))
        return public


class SpecText(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SpecItemView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str


def default_workspace(project: Project) -> str:
    return str(ROOT / "workspaces" / f"{project.id}-{uuid4().hex}")


async def create_project(session: AsyncSession, owner_id: int, data: ProjectCreate) -> Project:
    project = Project(owner_id=owner_id, **data.model_dump())
    session.add(project)
    await session.flush()
    if project.workspace_path is None:
        project.workspace_path = default_workspace(project)
    await session.commit()
    return project


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
async def add_project(data: ProjectCreate, session: Session, user: CurrentUser):
    return await create_project(session, user.id, data)


@router.get("/{project_id}", response_model=ProjectView)
async def get_project(project: OwnedProject):
    return project


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
async def edit_project(data: ProjectPatch, project: OwnedProject, session: Session):
    updates = data.model_dump(exclude_unset=True)
    required = {"name", "deadline_minutes", "meeting_interval_minutes", "autonomy_mode", "failover_mode", "verbosity_level"}
    if any(field in required and value is None for field, value in updates.items()):
        raise HTTPException(422, "Обязательные настройки проекта не могут быть пустыми.")
    async with orchestrator.runtime(project.id).boundary:
        await session.refresh(project)
        if project.status in {"running", "council"} and "workspace_path" in updates:
            raise HTTPException(409, "Сначала приостановите проект.")
        if "workspace_path" in updates and updates["workspace_path"] is None:
            updates["workspace_path"] = default_workspace(project)
        workspace_changed = "workspace_path" in updates and project.workspace_path != updates["workspace_path"]
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
        await probe_agent(project_id, agent)
    except TimeoutError as exc:
        raise HTTPException(400, "Таймаут проверки агента (15 секунд).") from exc
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
        await test_config(project.id, agent)
        agent.tested_at = utc_now()
        session.add(agent)
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
        await test_config(project.id, agent)
        return {"status": "success"}
    async with orchestrator.runtime(project.id).boundary:
        await require_agent_changes(project, session)
        agent = await owned_agent(data.agent_id, project, session)
        try:
            await test_config(project.id, agent)
        except HTTPException as exc:
            if agent.traffic_state != "asking":
                await orchestrator.traffic(agent, "error", exc.detail)
            raise
        if agent.traffic_state != "asking":
            agent.traffic_state = "waiting"
        agent.tested_at = utc_now()
        agent.cooldown_until = None
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
    )
    await test_config(project.id, candidate)
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
        return {"status": await orchestrator.action(
            project.id, "regenerate_plan", feedback=data.feedback, target_agent_ids=data.target_agent_ids
        )}
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
    name: AgentName
    adapter_type: ProviderName
    config_json: PresetConfig
    supports_vision: bool = False
    supports_audio: bool = False

    def agent_settings(self):
        config = self.config_json.model_dump(exclude_none=True)
        if self.adapter_type.lower() in CLI_PROVIDERS:
            if command := config.pop("command_template", None):
                config["command"] = command
            elif self.adapter_type.lower() == "codex":
                config["command"] = "codex exec --json --sandbox workspace-write -"
        config.update(supports_vision=self.supports_vision, supports_audio=self.supports_audio)
        return validate_agent_settings(self.adapter_type, config, self.config_json.model)


class PresetView(PresetCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    created_at: datetime

    @field_serializer("config_json")
    def public_config(self, config: PresetConfig):
        return config.model_dump(exclude={"api_key"}, exclude_none=True)


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
                model=preset.config_json.model, settings=preset.agent_settings(),
            ))
        for agent in agents:
            await test_config(project.id, agent)
            agent.tested_at = utc_now()
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
    ).where(TaskLog.project_id == project.id).group_by(TaskLog.agent_id).subquery())
    rows = (await session.execute(select(
        AgentConfig.id, AgentConfig.name, func.coalesce(logs.c.tokens, 0),
        func.coalesce(logs.c.completed, 0),
    ).outerjoin(logs, logs.c.agent_id == AgentConfig.id).where(
        AgentConfig.project_id == project.id
    ).order_by(AgentConfig.id))).all()
    total = await session.scalar(select(func.coalesce(func.sum(logs.c.tokens), 0)))
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
            result = await orchestrator.question(project.id, targets[0], prompt, media=media, mirror=True)
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
