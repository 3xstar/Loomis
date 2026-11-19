from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import CurrentUser, Session
from backend.api.ws import manager
from backend.core.orchestrator import orchestrator
from backend.database.models import AgentConfig, Project
from backend.settings import ROOT

router = APIRouter(prefix="/api/projects", tags=["projects"])
ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ProjectCreate(BaseModel):
    name: ProjectName
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)

    test_command: str | None = None
    meeting_interval_minutes: int = Field(default=15, ge=1, le=1440)
    deadline_minutes: int = Field(default=10, ge=1, le=1440)


class ProjectPatch(BaseModel):
    name: ProjectName | None = None
    description: str | None = None
    workspace_path: str | None = Field(default=None, min_length=1, max_length=1024)

    test_command: str | None = None
    meeting_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    deadline_minutes: int | None = Field(default=None, ge=1, le=1440)


class ProjectView(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    status: str
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: AgentName
    provider: AgentName
    model: ProjectName
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


class AgentPatch(BaseModel):
    name: AgentName | None = None
    provider: AgentName | None = None
    model: ProjectName | None = None
    settings: dict | None = None
    enabled: bool | None = None


class AgentView(AgentCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    created_at: datetime


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


@router.patch("/{project_id}", response_model=ProjectView)
async def edit_project(data: ProjectPatch, project: OwnedProject, session: Session):
    updates = data.model_dump(exclude_unset=True)
    if any(field in updates and updates[field] is None for field in ("deadline_minutes", "meeting_interval_minutes")):
        raise HTTPException(422, "Интервалы обязательны.")
    if project.status in {"running", "council"} and "workspace_path" in updates:
        raise HTTPException(409, "Сначала приостановите проект.")
    if "name" in updates and updates["name"] is None:
        raise HTTPException(422, "Имя проекта обязательно.")
    if "workspace_path" in updates and updates["workspace_path"] is None:
        updates["workspace_path"] = default_workspace(project)
    for field, value in updates.items():
        setattr(project, field, value)
    await session.commit()
    await session.refresh(project)
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


@router.post("/{project_id}/agents", response_model=AgentView, status_code=201)
async def add_agent(data: AgentCreate, project: OwnedProject, session: Session):
    agent = AgentConfig(project_id=project.id, **data.model_dump())
    session.add(agent)
    await save_agent(session)
    return agent


@router.get("/{project_id}/agents/{agent_id}", response_model=AgentView)
async def get_agent(agent: OwnedAgent):
    return agent


@router.patch("/{project_id}/agents/{agent_id}", response_model=AgentView)
async def edit_agent(data: AgentPatch, agent: OwnedAgent, session: Session):
    updates = data.model_dump(exclude_unset=True)
    if any(value is None for value in updates.values()):
        raise HTTPException(422, "Поля агента не могут быть пустыми.")
    for field, value in updates.items():
        setattr(agent, field, value)
    await save_agent(session)
    return agent


@router.delete("/{project_id}/agents/{agent_id}", status_code=204)
async def delete_agent(agent: OwnedAgent, session: Session):
    await session.delete(agent)
    await session.commit()



class ProjectAction(BaseModel):
    action: Literal["council", "approve", "pause", "resume", "rollback"]


class AgentQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=32000)


class ApprovalDecision(BaseModel):
    approved: bool


@router.post("/{project_id}/actions")
async def project_action(project: OwnedProject, data: ProjectAction):
    try:
        return {"status": await orchestrator.action(project.id, data.action)}
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/agents/{agent_id}/question")
async def agent_question(project: OwnedProject, agent: OwnedAgent, data: AgentQuestion):
    try:
        return await orchestrator.question(project.id, agent.id, data.question)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/approvals/{key}")
async def command_approval(project: OwnedProject, key: str, data: ApprovalDecision):
    try:
        orchestrator.decide(project.id, key, data.approved)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"approved": data.approved}
