import asyncio
from contextlib import asynccontextmanager, suppress

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import SimpleEventIsolation
from fastapi import FastAPI
from sqlalchemy import inspect

from backend.api import auth, projects, ws
from backend.bot.handlers import router as bot_router
from backend.core.orchestrator import orchestrator
from backend.database.db import Base, engine
from backend.settings import settings


dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
dispatcher.include_router(bot_router)


def initialize_schema(connection) -> None:
    Base.metadata.create_all(connection)
    inspector = inspect(connection)
    upgrades = {
        "users": {"login_name": "VARCHAR(255)", "password_hash": "VARCHAR(255)"},
        "projects": {
            "status": "VARCHAR(50) NOT NULL DEFAULT 'draft'",
            "test_command": "TEXT",
            "meeting_interval_minutes": "INTEGER NOT NULL DEFAULT 15",
            "deadline_minutes": "INTEGER NOT NULL DEFAULT 10",
        },
        "agent_configs": {
            "project_id": "INTEGER REFERENCES projects(id) ON DELETE CASCADE",
            "cooldown_until": "TIMESTAMP",
        },
    }
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = None
    polling = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(initialize_schema)
            await connection.exec_driver_sql("UPDATE projects SET status='paused' WHERE status='running'")
            await connection.exec_driver_sql("UPDATE projects SET status='planning' WHERE status='council'")
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
app.include_router(ws.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
