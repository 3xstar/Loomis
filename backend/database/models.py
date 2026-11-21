"""SQLAlchemy models for Loomis users, projects, and agent settings."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.settings import ROOT

from .db import Base

DEFAULT_PROJECTS_DIR = str(ROOT / "workspaces")

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ModelLeaderboard(Base):
    __tablename__ = "model_leaderboard"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(Text, unique=True)
    model_name: Mapped[str] = mapped_column(String(255))
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    reliability_score: Mapped[float] = mapped_column(Float, default=100.0, server_default="100.0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResponseCache(Base):
    __tablename__ = "response_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), unique=True)
    model_key: Mapped[str] = mapped_column(Text)
    response_text: Mapped[str] = mapped_column(Text)
    tokens_saved: Mapped[int] = mapped_column(BigInteger, default=0)
    response_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    login_name: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    github_token: Mapped[str | None] = mapped_column(Text)
    github_username: Mapped[str | None] = mapped_column(String(100))
    default_projects_dir: Mapped[str | None] = mapped_column(String(1024), default=DEFAULT_PROJECTS_DIR)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    tg_logs_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")


class UserModelPreset(Base):
    __tablename__ = "user_model_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    adapter_type: Mapped[str] = mapped_column(String(100))
    config_json: Mapped[dict] = mapped_column(JSON)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    total_tokens_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LinkToken(Base):
    __tablename__ = "link_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MagicLoginRequest(Base):
    __tablename__ = "magic_login_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    workspace_path: Mapped[str | None] = mapped_column(String(1024))
    git_repo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    test_command: Mapped[str | None] = mapped_column(Text)
    meeting_interval_minutes: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    deadline_minutes: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    autonomy_mode: Mapped[str] = mapped_column(String(20), default="autonomous", server_default="autonomous")
    failover_mode: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")
    verbosity_level: Mapped[str] = mapped_column(String(10), default="concise", server_default="concise")
    plan_digest: Mapped[str | None] = mapped_column(String(64))
    approved_plan_digest: Mapped[str | None] = mapped_column(String(64))
    turn_state: Mapped[dict | None] = mapped_column(JSON)

    owner: Mapped["User"] = relationship(back_populates="projects")
    spec_items: Mapped[list["SpecItem"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    agents: Mapped[list["AgentConfig"]] = relationship(cascade="all, delete-orphan")


class SpecItem(Base):
    __tablename__ = "spec_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    project: Mapped["Project"] = relationship(back_populates="spec_items")


class ProjectMedia(Base):
    __tablename__ = "project_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    spec_item_id: Mapped[int | None] = mapped_column(ForeignKey("spec_items.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(50))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class AgentConfig(Base):
    __tablename__ = "agent_configs"
    __table_args__ = (Index("ix_agent_configs_project_name", "project_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("user_model_presets.id", ondelete="SET NULL"))
    total_tokens_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    traffic_state: Mapped[str] = mapped_column(String(10), default="waiting", server_default="waiting")
    last_error: Mapped[str | None] = mapped_column(Text)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent_configs.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[int | None] = mapped_column(ForeignKey("project_media.id", ondelete="SET NULL"))
    sender_name: Mapped[str] = mapped_column(String(100))
    target_name: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent_configs.id", ondelete="CASCADE"), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    duration_seconds: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
