import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import SimpleEventIsolation
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from sqlalchemy.schema import CreateTable

from backend.api import auth, projects, ws
from backend.bot.handlers import router as bot_router
from backend.core.orchestrator import orchestrator
from backend.database.db import Base, engine
from backend.database.models import TaskLog
from backend.settings import settings


dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
dispatcher.include_router(bot_router)


def initialize_schema(connection) -> None:
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(274184326)")
    Base.metadata.create_all(connection)
    inspector = inspect(connection)
    upgrades = {
        "users": {"login_name": "VARCHAR(255)", "password_hash": "VARCHAR(255)",
                  "tg_logs_enabled": "BOOLEAN NOT NULL DEFAULT TRUE"},
        "agent_messages": {
            "agent_id": "INTEGER REFERENCES agent_configs(id) ON DELETE CASCADE",
            "media_id": "INTEGER REFERENCES project_media(id) ON DELETE SET NULL",
        },
        "task_logs": {"sender_name": "VARCHAR(100)"},
        "projects": {
            "status": "VARCHAR(50) NOT NULL DEFAULT 'draft'",
            "test_command": "TEXT",
            "meeting_interval_minutes": "INTEGER NOT NULL DEFAULT 15",
            "deadline_minutes": "INTEGER NOT NULL DEFAULT 10",
            "autonomy_mode": "VARCHAR(20) NOT NULL DEFAULT 'autonomous'",
            "failover_mode": "VARCHAR(10) NOT NULL DEFAULT 'auto'",
            "plan_digest": "VARCHAR(64)",
            "approved_plan_digest": "VARCHAR(64)",
            "turn_state": "JSON",
            "verbosity_level": "VARCHAR(10) NOT NULL DEFAULT 'concise'",
        },
        "agent_configs": {
            "project_id": "INTEGER REFERENCES projects(id) ON DELETE CASCADE",
            "cooldown_until": "TIMESTAMP",
            "traffic_state": "VARCHAR(10) NOT NULL DEFAULT 'waiting'",
            "last_error": "TEXT",
            "tested_at": "TIMESTAMP",
        },
    }
    legacy_messages = "agent_id" not in {column["name"] for column in inspector.get_columns("agent_messages")}
    for table, fields in upgrades.items():
        columns = {column["name"] for column in inspector.get_columns(table)}
        for field, definition in fields.items():
            if field not in columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")

    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_login_name ON users (login_name)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_configs_project_id ON agent_configs (project_id)"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_configs_project_name "
        "ON agent_configs (project_id, name)"
    )
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_agent_configs_name")
    if connection.dialect.name == "postgresql":
        target = next(column for column in inspector.get_columns("agent_messages")
                      if column["name"] == "target_name")
        if getattr(target["type"], "length", None) is not None:
            connection.exec_driver_sql("ALTER TABLE agent_messages ALTER COLUMN target_name TYPE TEXT")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_messages_agent_id ON agent_messages (agent_id)"
    )
    if legacy_messages:
        connection.exec_driver_sql("""
            UPDATE agent_messages SET agent_id = (
                SELECT id FROM agent_configs WHERE agent_configs.project_id = agent_messages.project_id
                AND (
                    (agent_messages.sender_name <> 'User' AND agent_configs.name = agent_messages.sender_name)
                    OR (agent_messages.sender_name = 'User' AND agent_messages.channel = 'direct'
                        AND '@' || agent_configs.name = agent_messages.target_name)
                )
            )
        """)
    connection.exec_driver_sql("""
        UPDATE task_logs SET sender_name = (
            SELECT name FROM agent_configs WHERE agent_configs.id = task_logs.agent_id
        ) WHERE sender_name IS NULL AND agent_id IS NOT NULL
    """)
    foreign_key = next(key for key in inspector.get_foreign_keys("task_logs")
                       if key["constrained_columns"] == ["agent_id"])
    if foreign_key["options"].get("ondelete") != "CASCADE":
        if connection.dialect.name == "sqlite":
            table = TaskLog.__table__
            ddl = str(CreateTable(table).compile(connection)).replace("CREATE TABLE task_logs (",
                                                                      "CREATE TABLE task_logs_new (", 1)
            columns = ", ".join(table.columns.keys())
            connection.exec_driver_sql(ddl)
            connection.exec_driver_sql(
                f"INSERT INTO task_logs_new ({columns}) SELECT {columns} FROM task_logs"
            )
            connection.exec_driver_sql("DROP TABLE task_logs")
            connection.exec_driver_sql("ALTER TABLE task_logs_new RENAME TO task_logs")
            for index in table.indexes:
                index.create(connection)
        else:
            name = connection.dialect.identifier_preparer.quote(foreign_key["name"])
            connection.exec_driver_sql(f"ALTER TABLE task_logs DROP CONSTRAINT {name}")
            connection.exec_driver_sql(
                "ALTER TABLE task_logs ADD FOREIGN KEY (agent_id) REFERENCES agent_configs(id) ON DELETE CASCADE"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = None
    polling = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(initialize_schema)
            await connection.exec_driver_sql("UPDATE projects SET status='paused' WHERE status='running'")
            await connection.exec_driver_sql("UPDATE projects SET status='planning' WHERE status='council'")
            await connection.exec_driver_sql(
                "UPDATE agent_configs SET traffic_state='waiting' WHERE traffic_state='working'"
            )
        if settings.bot_token:
            bot = Bot(token=settings.bot_token)
            orchestrator.bot = bot
            await bot.set_my_commands([
                BotCommand(command="start", description="Главное меню и статус аккаунта"),
                BotCommand(command="new_project", description="Создать новый проект"),
                BotCommand(command="projects", description="Мои проекты и управление"),
                BotCommand(command="quick_register", description="Быстрая регистрация аккаунта"),
                BotCommand(command="link", description="Привязать аккаунт по коду из приложения"),
                BotCommand(command="cancel", description="Отменить текущее действие"),
            ])
            polling = asyncio.create_task(dispatcher.start_polling(
                bot, handle_signals=False, close_bot_session=False, handle_as_tasks=True
            ))
        yield
    finally:
        await orchestrator.shutdown()
        orchestrator.bot = None
        try:
            if polling is not None:
                if not polling.done():
                    try:
                        await asyncio.wait_for(dispatcher.stop_polling(), timeout=5)
                    except (RuntimeError, TimeoutError):
                        polling.cancel()
                with suppress(asyncio.CancelledError):
                    await polling
        finally:
            try:
                if bot is not None:
                    await bot.session.close()
            finally:
                await engine.dispose()


app = FastAPI(title="Loomis", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(projects.hub_router)
app.include_router(ws.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(
    directory=Path(__file__).resolve().parents[1] / "desktop" / "ui",
    html=True,
), name="desktop")


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
