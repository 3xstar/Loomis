import json
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.database.db import async_session_factory
from backend.database.models import ModelLeaderboard, ResponseCache, utc_now
from worker.adapters import CLI_PROVIDERS
from worker.adapters.api_adapter import api_model_key


def model_key(provider, model, settings):
    if provider.lower() in CLI_PROVIDERS:
        return f"{provider.lower()}::{model}"
    return api_model_key(model, settings)


def upsert(session, table):
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    return insert(table)


async def record_model_result(session, config, success, latency_ms):
    row = ModelLeaderboard
    now = utc_now()
    insert = upsert(session, row).values(
        model_key=model_key(config.provider, config.model, config.settings), model_name=config.model,
        success_count=int(success), error_count=int(not success),
        avg_latency_ms=latency_ms if success else 0.0,
        reliability_score=100.0 if success else 80.0, updated_at=now,
    )
    await session.execute(insert.on_conflict_do_update(
        index_elements=[row.model_key],
        set_={
            "success_count": row.success_count + int(success),
            "error_count": row.error_count + int(not success),
            "avg_latency_ms": ((row.avg_latency_ms * row.success_count + latency_ms) /
                               (row.success_count + 1)) if success else row.avg_latency_ms,
            "reliability_score": row.reliability_score * 0.8 + (20.0 if success else 0.0),
            "updated_at": now,
        },
    ))


async def rank_configs(configs):
    if not configs:
        return []
    keys = {config.id: model_key(config.provider, config.model, config.settings) for config in configs}
    async with async_session_factory() as session:
        rows = await session.scalars(select(ModelLeaderboard).where(ModelLeaderboard.model_key.in_(set(keys.values()))))
        scores = {row.model_key: (-row.reliability_score, row.avg_latency_ms or float("inf")) for row in rows}
    return sorted(configs, key=lambda config: scores.get(keys[config.id], (-100.0, float("inf"))))


def prompt_digest(key, system_prompt, user_prompt):
    return sha256(json.dumps([key, system_prompt, user_prompt], ensure_ascii=False).encode("utf-8")).hexdigest()


async def cached_response(digest):
    row = ResponseCache
    async with async_session_factory() as session:
        result = await session.execute(update(row).where(
            row.prompt_hash == digest, row.created_at > utc_now() - timedelta(hours=24),
        ).values(tokens_saved=row.tokens_saved + row.response_tokens).returning(row.response_text))
        answer = result.scalar_one_or_none()
        await session.commit()
        return answer


async def cache_response(session, digest, key, result):
    row = ResponseCache
    now = utc_now()
    expired = now - timedelta(hours=24)
    await session.execute(delete(row).where(row.created_at <= expired))
    insert = upsert(session, row).values(
        prompt_hash=digest, model_key=key, response_text=result.message,
        response_tokens=result.tokens, created_at=now,
    )
    await session.execute(insert.on_conflict_do_nothing(index_elements=[row.prompt_hash]))
