"""Токены ИИ клиента: месячный пакет по тарифу, перенос остатка на месяц, купленные токены и скрытый резерв.

* Клиент видит только свой пакет и остаток — без модели и без резерва.
* Резерв (bucket="reserve") тратят обязательные отчёты: обработка брифа и ИИ-выводы в отчётах.
  Пока резерв не исчерпан, они не трогают лимит клиента — и не идут из кармана агентства.
* bucket="agency" — служебные запросы агентства (диагностика, демо-аудит), клиенту не считаются.
* Порядок списания: месячный пакет (+ перенос неиспользованного за прошлый месяц) → купленные токены.
* Индивидуальный тариф без своего лимита — без ограничений.
"""
import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ai import AiTokenAccount, AiTokenPurchase, AiUsage
from app.models.marketing import ClientWorkspace, PortalNotification, PortalUser
from app.models.system import AppSetting

logger = logging.getLogger("uvicorn.error.ai_tokens")
KEY = "ai_tokens"
DEFAULTS = {
    # Видимый клиенту месячный пакет. На «Старте» ИИ-функций нет.
    "monthly": {"start": 0, "growth": 2_000_000, "system": 5_000_000, "trial": 0},
    # Скрытый резерв на обязательные отчёты (бриф, ИИ-выводы в отчётах). individual — тариф по договору.
    "reserve": {"start": 300_000, "growth": 300_000, "system": 500_000, "trial": 100_000, "individual": 500_000},
    "packs": [{"tokens": 1_000_000, "price": 1490}, {"tokens": 3_000_000, "price": 3990},
              {"tokens": 10_000_000, "price": 11900}],
    "warn_share": 0.25,
}
EMPTY = "Токены ИИ на этот месяц закончились. Пополните их в «Настройки → ИИ-токены»."
YOOKASSA = "https://api.yookassa.ru/v3"


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(start: datetime) -> datetime:
    return (start + timedelta(days=32)).replace(day=1)


def month_key(now: datetime) -> str:
    return month_start(now).strftime("%Y-%m")


async def config(db: AsyncSession) -> dict:
    row = await db.get(AppSetting, KEY)
    stored = dict(row.value or {}) if row else {}
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for name in ("monthly", "reserve"):
        out[name].update({k: int(v) for k, v in (stored.get(name) or {}).items() if isinstance(v, (int, float)) and v >= 0})
    packs = [p for p in stored.get("packs") or [] if isinstance(p, dict) and int(p.get("tokens") or 0) > 0 and int(p.get("price") or 0) > 0]
    if packs:
        out["packs"] = [{"tokens": int(p["tokens"]), "price": int(p["price"])} for p in packs]
    share = stored.get("warn_share")
    if isinstance(share, (int, float)) and 0 < share < 1:
        out["warn_share"] = float(share)
    return out


async def save_config(db: AsyncSession, value: dict) -> dict:
    row = await db.get(AppSetting, KEY)
    if row is None:
        db.add(AppSetting(key=KEY, value=value))
    else:
        row.value = value
    await db.flush()
    return await config(db)


async def _used(db, workspace_id, since, until=None) -> tuple[int, int]:
    query = select(AiUsage.bucket, func.coalesce(func.sum(AiUsage.tokens_in + AiUsage.tokens_out), 0)).where(
        AiUsage.workspace_id == workspace_id, AiUsage.created_at >= since)
    if until is not None:
        query = query.where(AiUsage.created_at < until)
    rows = {bucket: int(total) for bucket, total in (await db.execute(query.group_by(AiUsage.bucket))).all()}
    return rows.get("client", 0), rows.get("reserve", 0)


def _ai_included(plan: str | None) -> bool:
    from app.services import plans
    return plans.allows(plan, "ai_chat") or plans.allows(plan, "ai_calls")


async def status(db: AsyncSession, workspace_id: int, now: datetime | None = None) -> dict:
    """Полный расчёт (с резервом). Клиенту отдаём public_view()."""
    from app.services import plans
    now = now or datetime.now(timezone.utc)
    cfg = await config(db)
    workspace = await db.get(ClientWorkspace, workspace_id)
    account = await db.get(AiTokenAccount, workspace_id)
    plan = workspace.plan if workspace and workspace.plan in plans.PLANS else None
    if account and account.monthly_override is not None:
        base = int(account.monthly_override)
    elif plan is None:
        base = None  # индивидуальный тариф: без ограничений
    else:
        base = int(cfg["monthly"].get(plan, 0))
    reserve_limit = int(account.reserve_override if account and account.reserve_override is not None
                        else cfg["reserve"].get(plan or "individual", 0))
    start = month_start(now); renew = next_month(start); prev = month_start(start - timedelta(days=1))
    client, reserve = await _used(db, workspace_id, start)
    used = client + max(0, reserve - reserve_limit)
    carry = 0
    created = workspace.created_at if workspace else None
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if base and created is not None and created < start:  # перенос — только для клиента, который был с нами в прошлом месяце
        prev_client, prev_reserve = await _used(db, workspace_id, prev, start)
        carry = min(base, max(0, base - prev_client - max(0, prev_reserve - reserve_limit)))
    package = None if base is None else base + carry
    recorded = account.overflow_recorded if account and account.overflow_month == month_key(now) else 0
    bought_start = max(0, (account.bought_total - account.bought_spent + recorded) if account else 0)
    overflow = 0 if package is None else max(0, used - package)
    bought_left = max(0, bought_start - overflow)
    remaining = None if package is None else max(0, package - used) + bought_left
    capacity = None if package is None else package + bought_start
    # Прогноз: средний расход клиента за последние до 7 дней.
    since = now - timedelta(days=7)
    first = await db.scalar(select(func.min(AiUsage.created_at)).where(
        AiUsage.workspace_id == workspace_id, AiUsage.bucket == "client", AiUsage.created_at >= since))
    week_client, _ = await _used(db, workspace_id, since)
    days_left = None
    if remaining is not None and week_client > 0 and first is not None:
        first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
        span = min(7.0, max(1.0, (now - first).total_seconds() / 86400))
        days_left = math.floor(remaining / (week_client / span))
    share = None if not capacity else remaining / capacity
    level = "unlimited" if package is None else "empty" if remaining <= 0 else (
        "low" if share is not None and share <= cfg["warn_share"] else "ok")
    return {"plan": plan, "ai_included": _ai_included(plan), "unlimited": package is None,
            "monthly": base, "carryover": carry, "package": package, "used": used, "bought_left": bought_left,
            "bought_start": bought_start, "overflow": min(overflow, bought_start),
            "remaining": remaining, "capacity": capacity, "share_left": share, "level": level,
            "days_left": days_left, "days_to_renew": max(0, math.ceil((renew - now).total_seconds() / 86400)),
            "renews_at": renew.date().isoformat(), "reserve_limit": reserve_limit, "reserve_used": reserve,
            "reserve_left": max(0, reserve_limit - reserve), "warn_share": cfg["warn_share"]}


def public_view(state: dict) -> dict:
    hidden = {"reserve_limit", "reserve_used", "reserve_left", "bought_start", "overflow"}
    return {k: v for k, v in state.items() if k not in hidden}


async def admit(db: AsyncSession, workspace_id: int | None, bucket: str, tokens: int) -> None:
    """Пускать ли запрос. Резерв сначала тратит свой лимит, потом — пакет клиента."""
    from app.services.ai import AIError
    if workspace_id is None or bucket == "agency":
        return
    state = await status(db, workspace_id)
    if bucket == "reserve" and state["reserve_left"] > 0:
        return
    if state["unlimited"] or state["remaining"] > 0:
        return
    raise AIError(EMPTY, 402, code="tokens_empty")


async def settle(db: AsyncSession, workspace_id: int) -> None:
    """После запроса: списать перерасход из купленных и предупредить владельца (≈25% и 0%)."""
    state = await status(db, workspace_id)
    if state["unlimited"]:
        return
    key = month_key(datetime.now(timezone.utc))
    account = await db.get(AiTokenAccount, workspace_id)
    if state["overflow"] and account is None:
        account = AiTokenAccount(workspace_id=workspace_id, bought_total=0, bought_spent=0, overflow_recorded=0)
        db.add(account)
    if account is not None:
        if account.overflow_month != key:
            account.overflow_month, account.overflow_recorded = key, 0
        delta = state["overflow"] - account.overflow_recorded
        if delta:
            account.bought_spent += delta
            account.overflow_recorded = state["overflow"]
    if not state["ai_included"] and not state["bought_start"]:
        return
    mark = f"{key}:0" if state["level"] == "empty" else f"{key}:25" if state["level"] == "low" else None
    previous = account.warned if account else None
    if mark is None or previous == mark or (mark.endswith(":25") and previous == f"{key}:0"):
        return
    if account is None:
        account = AiTokenAccount(workspace_id=workspace_id, bought_total=0, bought_spent=0, overflow_recorded=0)
        db.add(account)
    account.warned = mark
    if state["level"] == "empty":
        title, body = "Токены ИИ закончились", "ИИ-подсказки и разборы остановлены до пополнения. Пополнить — «Настройки → ИИ-токены»."
    else:
        left = f" — хватит примерно на {state['days_left']} дн." if state["days_left"] is not None and state["days_left"] < state["days_to_renew"] else ""
        title = "Осталось около 25% токенов ИИ"
        body = f"Остаток {fmt(state['remaining'])} токенов{left} Пополнить — «Настройки → ИИ-токены»."
    await notify_owners(db, workspace_id, title, body)


async def notify_owners(db, workspace_id, title, body):
    from app.services import push
    owners = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == workspace_id,
              PortalUser.active.is_(True), PortalUser.role == "client_owner"))).all()
    for user in owners:
        db.add(PortalNotification(workspace_id=workspace_id, user_id=user.id, level="warning", title=title, body=body))
        push.queue(db, user.id, title, body, "/settings#tokens")


def fmt(n: int | None) -> str:
    return "∞" if n is None else f"{n:,}".replace(",", " ")


# --------------------------------------------------------------------------- покупка

def yookassa_configured() -> bool:
    return bool(os.environ.get("YOOKASSA_SHOP_ID") and os.environ.get("YOOKASSA_SECRET_KEY"))


def _auth():
    return os.environ.get("YOOKASSA_SHOP_ID", ""), os.environ.get("YOOKASSA_SECRET_KEY", "")


async def create_payment(purchase: AiTokenPurchase, workspace_name: str) -> str:
    """Платёж ЮKassa (карта, СБП, кошельки). Возвращает ссылку на оплату."""
    body = {"amount": {"value": f"{purchase.price_rub}.00", "currency": "RUB"}, "capture": True,
            "confirmation": {"type": "redirect", "return_url": f"{settings.frontend_origin.rstrip('/')}/settings?paid={purchase.id}#tokens"},
            "description": f"Токены ИИ StepToLead: {fmt(purchase.tokens)} — {workspace_name}"[:128],
            "metadata": {"purchase_id": str(purchase.id), "workspace_id": str(purchase.workspace_id)}}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{YOOKASSA}/payments", auth=_auth(), json=body,
                                     headers={"Idempotence-Key": f"stl-tokens-{purchase.id}-{uuid.uuid4().hex[:8]}"})
    if response.status_code >= 400:
        logger.warning("yookassa create failed: %s", response.status_code)
        raise RuntimeError("Платёжный сервис не ответил, попробуйте позже")
    data = response.json()
    purchase.external_id = data.get("id")
    return data["confirmation"]["confirmation_url"]


async def fetch_payment(external_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{YOOKASSA}/payments/{external_id}", auth=_auth())
    response.raise_for_status()
    return response.json()


async def credit(db: AsyncSession, purchase: AiTokenPurchase) -> bool:
    """Зачислить оплаченные токены. Повторный вызов ничего не делает."""
    if purchase.status == "paid":
        return False
    purchase.status, purchase.paid_at = "paid", datetime.now(timezone.utc)
    account = await db.get(AiTokenAccount, purchase.workspace_id)
    if account is None:
        account = AiTokenAccount(workspace_id=purchase.workspace_id, bought_total=0, bought_spent=0, overflow_recorded=0)
        db.add(account)
    account.bought_total = (account.bought_total or 0) + purchase.tokens
    if account.warned and account.warned.startswith(month_key(datetime.now(timezone.utc))):
        account.warned = None  # после пополнения снова предупредим, когда останется мало
    await db.flush()
    return True


async def sync_payment(db: AsyncSession, purchase: AiTokenPurchase) -> AiTokenPurchase:
    """Сверить статус с ЮKassa (на возврате с оплаты и по вебхуку — источник правды всегда API)."""
    if purchase.status != "pending" or purchase.provider != "yookassa" or not purchase.external_id or not yookassa_configured():
        return purchase
    data = await fetch_payment(purchase.external_id)
    if data.get("status") == "succeeded" and str((data.get("metadata") or {}).get("purchase_id")) == str(purchase.id):
        await credit(db, purchase)
    elif data.get("status") == "canceled":
        purchase.status = "canceled"
    return purchase


def purchase_json(row: AiTokenPurchase) -> dict:
    return {"id": row.id, "created_at": row.created_at.isoformat() if row.created_at else None, "tokens": row.tokens,
            "price_rub": row.price_rub, "status": row.status, "provider": row.provider, "comment": row.comment,
            "paid_at": row.paid_at.isoformat() if row.paid_at else None}
