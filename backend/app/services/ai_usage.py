"""Durable, independent usage transactions. Reservations serialize admission on PostgreSQL."""
import math
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select, text
from app.db import SessionLocal
from app.core.config import settings
from app.models.ai import AiUsage

captured = ContextVar("ai_usage_ids", default=None)
session_factory = ContextVar("ai_usage_sessions", default=None)


def estimate(value: str) -> int:
    return math.ceil(len(value) / 3)


async def reserve(feature, provider, model, workspace_id=None, tokens=0, audio_seconds=0, check_limits=True):
    from app.services.ai import AIError
    async with (session_factory.get() or SessionLocal)() as db:
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(82462029)"))
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        query = select(func.coalesce(func.sum(AiUsage.tokens_in + AiUsage.tokens_out), 0)).where(AiUsage.created_at >= start)
        total = await db.scalar(query)
        own = await db.scalar(query.where(AiUsage.workspace_id == workspace_id)) if workspace_id is not None else 0
        if check_limits and ((settings.ai_daily_token_limit > 0 and (total >= settings.ai_daily_token_limit or total + tokens > settings.ai_daily_token_limit)) or
                (workspace_id is not None and settings.ai_workspace_daily_token_limit > 0 and
                 (own >= settings.ai_workspace_daily_token_limit or own + tokens > settings.ai_workspace_daily_token_limit))):
            row = AiUsage(feature=feature, provider=provider, model=model, workspace_id=workspace_id,
                          error="Дневной лимит ИИ исчерпан", ok=False)
            db.add(row); await db.commit()
            raise AIError("Дневной лимит ИИ исчерпан")
        row = AiUsage(feature=feature, provider=provider, model=model, workspace_id=workspace_id,
                      tokens_in=tokens, tokens_out=0, audio_seconds=audio_seconds, error="Запрос выполняется")
        db.add(row); await db.commit()
        if captured.get() is not None:
            captured.get().append(row.id)
        return row.id


async def finish(usage_id, *, tokens_in=0, tokens_out=0, duration_ms=0, error=None, audio_seconds=None):
    async with (session_factory.get() or SessionLocal)() as db:
        row = await db.get(AiUsage, usage_id)
        row.tokens_in, row.tokens_out, row.duration_ms = tokens_in, tokens_out, duration_ms
        row.ok, row.error = error is None, error[:240] if error else None
        if audio_seconds is not None:
            row.audio_seconds = audio_seconds
        await db.commit()


async def report(db, days=1):
    import json
    try:
        prices = json.loads(settings.ai_prices)
    except (ValueError, TypeError):
        prices = {}
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.scalars(select(AiUsage).where(AiUsage.created_at >= today - timedelta(days=days - 1)))).all()
    groups = {}
    for row in rows:
        key = (row.feature, row.model, row.workspace_id)
        item = groups.setdefault(key, {"feature": row.feature, "model": row.model, "workspace_id": row.workspace_id,
                                      "requests": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0,
                                      "audio_seconds": 0, "cost_rub": 0, "price_known": False})
        item["requests"] += 1; item["errors"] += int(not row.ok)
        item["tokens_in"] += row.tokens_in; item["tokens_out"] += row.tokens_out
        item["audio_seconds"] += row.audio_seconds
        price = prices.get(row.model) if isinstance(prices, dict) else None
        if isinstance(price, list) and len(price) == 2 and all(isinstance(p, (int, float)) and p >= 0 for p in price):
            item["price_known"] = True
            item["cost_rub"] += (row.tokens_in * price[0] + row.tokens_out * price[1]) / 1_000_000
    return list(groups.values())
