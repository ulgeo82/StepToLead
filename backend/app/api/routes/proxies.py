from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.db import get_db
from app.models import Proxy, TelegramAccount
from app.schemas.proxy import ProxyBulkCreate, ProxyBulkResult, ProxyRead
from app.services.proxy_parser import parse_proxy_line

from app.domains.leadgen import space
from app.services import tg_space

# Пул «Аутрич» (pool=agency) — у каждого клиента свой (этап B). Пул CRM-профилей клиентов (pool=client) —
# инфраструктура агентства: виден и меняется только в пространстве агентства.
router = APIRouter(prefix="/proxies", tags=["proxies"], dependencies=[Depends(space.bind)])


def at_home() -> bool:
    ws = tg_space.current()
    return ws is None or ws == tg_space.home()


def visible(query):
    """Прокси, которые видны в выбранном пространстве."""
    from sqlalchemy import and_, or_
    mine = and_(Proxy.pool == "agency", tg_space.belongs(Proxy.workspace_id, tg_space.current()))
    return query.where(or_(mine, Proxy.pool == "client") if at_home() else mine)


def proxy_or_404(proxy):
    if proxy is None or (proxy.pool == "agency" and not tg_space.owns(proxy.workspace_id)) \
            or (proxy.pool == "client" and not at_home()):
        raise HTTPException(404, "Прокси не найден")
    return proxy


@router.get("", response_model=list[ProxyRead])
async def list_proxies(pool: str | None = Query(default=None, pattern=r"^(agency|client)$"),
                       db: AsyncSession = Depends(get_db)):
    from app.services import proxy_health
    query = visible(select(Proxy)).order_by(Proxy.created_at.desc())
    if pool:
        query = query.where(Proxy.pool == pool)
    proxies = list((await db.scalars(query)).all())
    accounts = (await db.scalars(tg_space.scope(select(TelegramAccount).where(TelegramAccount.proxy_id.is_not(None)),
                                                TelegramAccount.workspace_id))).all()
    by_proxy: dict[int, list[dict]] = {}
    for account in accounts:
        by_proxy.setdefault(account.proxy_id, []).append({
            "id": account.id, "phone": account.phone,
            "name": " ".join(x for x in (account.first_name, account.last_name) if x) or None})
    from app.services.client_proxies import usage
    clients = await usage(db)
    from app.models.marketing import ClientWorkspace
    names = {w.id: w.name for w in (await db.scalars(select(ClientWorkspace))).all()}
    dups = proxy_health.duplicates(proxies)
    out = []
    for proxy in proxies:
        health = proxy_health.view(proxy, dups.get(proxy.id))
        out.append(ProxyRead.model_validate(proxy).model_copy(update={
            "assigned_accounts": len(by_proxy.get(proxy.id, [])), "client_accounts": clients.get(proxy.id, 0),
            "dedicated_workspace_name": names.get(proxy.dedicated_workspace_id),
            "rotating": health.pop("rotating"), "duplicate_of": dups.get(proxy.id), "health": health,
            "accounts_info": by_proxy.get(proxy.id, [])}))
    return out


@router.post("/bulk", response_model=ProxyBulkResult, status_code=status.HTTP_201_CREATED)
async def bulk_create(payload: ProxyBulkCreate, db: AsyncSession = Depends(get_db)):
    """Строки сохраняются сразу («не проверен»), проверка связи с Telegram и внешнего IP — в фоне (batch_id)."""
    from app.services import telegram_jobs
    from app.services.proxy_parser import skip_line
    if payload.pool == "client" and not at_home():
        raise HTTPException(422, "Прокси CRM-профилей добавляются в пространстве агентства")
    # Один и тот же прокси не может быть у двух клиентов: дубли ищем по всему порталу.
    existing = {(row.host.lower(), row.port, row.username) for row in (await db.scalars(select(Proxy))).all()}
    default_scheme = "socks5" if payload.scheme == "auto" else payload.scheme
    max_accounts = payload.max_accounts or (1 if payload.pool == "agency" else 3)
    imported = duplicates = 0
    errors: list[str] = []
    added: list[tuple[Proxy, bool]] = []
    for line_number, line in enumerate(payload.entries.splitlines(), start=1):
        if skip_line(line):
            continue
        try:
            item = parse_proxy_line(line, default_scheme)
        except ValueError as exc:
            errors.append(f"Строка {line_number}: {exc}")
            continue
        key = (item.host.lower(), item.port, item.username)
        if key in existing:
            duplicates += 1
            continue
        proxy = Proxy(scheme=item.scheme, host=item.host, port=item.port, username=item.username,
                      pool=payload.pool, max_accounts=max_accounts, password_encrypted=encrypt_secret(item.password),
                      workspace_id=tg_space.current() if payload.pool == "agency" else None)
        db.add(proxy)
        added.append((proxy, payload.scheme == "auto" and not item.explicit_scheme))
        existing.add(key)
        imported += 1
    await db.commit()
    batch_id = None
    if added:
        batch_id = telegram_jobs.start_proxy_check([p.id for p, _ in added], {p.id for p, auto in added if auto})
    return ProxyBulkResult(imported=imported, skipped=duplicates + len(errors), duplicates=duplicates,
                           errors=errors[:30], batch_id=batch_id)


class ProxyCheckIn(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=5000)


@router.post("/check", status_code=status.HTTP_202_ACCEPTED)
async def check_proxies(payload: ProxyCheckIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.access import check_origin
    from app.services import telegram_jobs
    check_origin(request)
    query = visible(select(Proxy.id))
    if payload.ids:
        query = query.where(Proxy.id.in_(payload.ids))
    ids = list((await db.scalars(query)).all())
    if not ids:
        raise HTTPException(422, "Нет прокси для проверки")
    return {"batch_id": telegram_jobs.start_proxy_check(ids)}


@router.get("/checks/{batch_id}/progress")
async def check_progress(batch_id: int):
    from app.services import progress, telegram_jobs
    return progress.get(telegram_jobs.key("proxy-check", batch_id)) or {"state": "idle"}


class ProxyBulkDelete(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class ProxyMoveIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    workspace_id: int = Field(gt=0)


@router.post("/move")
async def move_proxies(payload: ProxyMoveIn, db: AsyncSession = Depends(get_db)):
    """Перенести свободные прокси аутрича к другому клиенту."""
    from app.services import tg_move
    return await tg_move.move_proxies(db, payload.ids, payload.workspace_id)


@router.post("/bulk-delete")
async def bulk_delete(payload: ProxyBulkDelete, request: Request, db: AsyncSession = Depends(get_db)):
    """Занятые клиентскими CRM-профилями не удаляются; аккаунты аутрича на удалённом прокси остаются без прокси
    (и не подключаются, пока им не дадут новый)."""
    from app.core.access import check_origin
    from app.services.client_proxies import usage
    check_origin(request)
    load = await usage(db)
    deleted, skipped = 0, []
    for proxy in (await db.scalars(visible(select(Proxy)).where(Proxy.id.in_(payload.ids)))).all():
        if load.get(proxy.id, 0):
            skipped.append({"id": proxy.id, "reason": "Закреплён за клиентскими профилями"})
            continue
        for account in (await db.scalars(select(TelegramAccount).where(TelegramAccount.proxy_id == proxy.id))).all():
            account.proxy_id = None
        await db.delete(proxy)
        deleted += 1
    await db.commit()
    return {"deleted": deleted, "skipped": skipped}


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy(proxy_id: int, db: AsyncSession = Depends(get_db)):
    proxy = proxy_or_404(await db.get(Proxy, proxy_id))
    from app.services.client_proxies import usage
    load = (await usage(db)).get(proxy_id, 0)
    if load:
        raise HTTPException(409, "Прокси закреплён за клиентскими профилями. Сначала переназначьте их вручную.")
    accounts = list((await db.scalars(select(TelegramAccount).where(TelegramAccount.proxy_id == proxy_id))).all())
    for account in accounts:
        account.proxy_id = None
    await db.delete(proxy)
    await db.commit()


class ProxyPatch(BaseModel):
    pool: str = Field(pattern=r"^(agency|client)$")
    max_accounts: int = Field(default=3, ge=1, le=100)
    dedicated_workspace_id: int | None = None
    label: str | None = Field(default=None, max_length=120)


@router.patch("/{proxy_id}")
async def edit_proxy(proxy_id: int, payload: ProxyPatch, request: Request, db=Depends(get_db)):
    from app.core.access import check_origin
    from app.models.marketing import ClientWorkspace
    from app.services.client_proxies import usage
    check_origin(request)
    proxy = proxy_or_404(await db.get(Proxy, proxy_id))
    if payload.pool != proxy.pool and not at_home():
        raise HTTPException(422, "Пул прокси меняется в пространстве агентства")
    load = (await usage(db)).get(proxy_id, 0)
    if payload.max_accounts < load:
        raise HTTPException(409, "Лимит меньше числа закреплённых профилей")
    if load:
        if payload.pool != proxy.pool or payload.dedicated_workspace_id != proxy.dedicated_workspace_id:
            raise HTTPException(409, "Прокси используется профилями — сначала переназначьте их вручную")
    if payload.dedicated_workspace_id and not await db.get(ClientWorkspace, payload.dedicated_workspace_id):
        raise HTTPException(422, "Компания не найдена")
    proxy.pool, proxy.max_accounts = payload.pool, payload.max_accounts
    proxy.dedicated_workspace_id = payload.dedicated_workspace_id
    if "label" in payload.model_fields_set:
        proxy.label = (payload.label or "").strip() or None
    await db.commit()
    return {"ok": True}


class RemapIn(BaseModel):
    proxy_id: int
    confirm_ip_change: bool = False


@router.post("/profiles/{channel_id}/remap")
async def remap_profile(channel_id: int, payload: RemapIn, request: Request, db=Depends(get_db)):
    from app.core.access import check_origin
    from app.models.messaging import MessagingChannel
    from app.services.client_proxies import usage
    check_origin(request)
    if not payload.confirm_ip_change:
        raise HTTPException(422, "Подтвердите смену IP: Telegram может ограничить аккаунт")
    channel = await db.get(MessagingChannel, channel_id)
    proxy = await db.get(Proxy, payload.proxy_id)
    if not channel or channel.kind != "telegram_user":
        raise HTTPException(404, "Профиль не найден")
    if channel.active:
        raise HTTPException(409, "Сначала выключите канал и дождитесь отключения профиля")
    if not proxy or proxy.pool != "client" or proxy.scheme not in ("socks5", "http") or proxy.status != "working":
        raise HTTPException(422, "Выберите рабочий прокси из пула клиентов")
    if proxy.dedicated_workspace_id not in (None, channel.workspace_id):
        raise HTTPException(409, "Прокси выделен другой компании")
    if (await usage(db)).get(proxy.id, 0) >= proxy.max_accounts:
        raise HTTPException(409, "Прокси заполнен")
    channel.config = {**(channel.config or {}), "proxy_id": proxy.id}
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- per-client proxies (client card in admin)

class ClientProxyIn(BaseModel):
    line: str = Field(min_length=3, max_length=500)
    max_accounts: int = Field(default=3, ge=1, le=100)


class ClientProxyReplace(BaseModel):
    line: str = Field(min_length=3, max_length=500)
    confirm_ip_change: bool = False


async def _client_proxies_payload(db, workspace_id: int) -> dict:
    from app.models.messaging import MessagingChannel
    proxies = (await db.scalars(select(Proxy).where(Proxy.pool == "client", Proxy.dedicated_workspace_id == workspace_id)
                                .order_by(Proxy.id))).all()
    profiles = (await db.scalars(select(MessagingChannel).where(MessagingChannel.workspace_id == workspace_id,
                                                                MessagingChannel.kind == "telegram_user"))).all()
    shared = sum(1 for p in profiles if (p.config or {}).get("proxy_id") not in {x.id for x in proxies}
                 and (p.config or {}).get("proxy_id"))
    return {"proxies": [{
        "id": p.id, "host": p.host, "port": p.port, "username": p.username, "status": p.status,
        "last_error": p.last_error, "last_checked_at": p.last_checked_at, "max_accounts": p.max_accounts,
        "profiles": [{"id": c.id, "name": c.name, "status": c.status, "last_error": c.last_error}
                     for c in profiles if (c.config or {}).get("proxy_id") == p.id]} for p in proxies],
        "shared_pool_profiles": shared,
        "waiting": [{"id": c.id, "name": c.name} for c in profiles if c.status == "waiting_proxy"]}


@router.get("/workspace/{workspace_id}")
async def client_proxies_list(workspace_id: int, db=Depends(get_db)):
    return await _client_proxies_payload(db, workspace_id)


@router.post("/workspace/{workspace_id}", status_code=201)
async def client_proxy_add(workspace_id: int, payload: ClientProxyIn, request: Request, db=Depends(get_db)):
    """The agency pastes the client's proxy; it is checked against Telegram before it is saved."""
    from app.core.access import check_origin
    from app.models.marketing import ClientWorkspace
    from app.services import client_proxies
    check_origin(request)
    if not await db.get(ClientWorkspace, workspace_id):
        raise HTTPException(404, "Компания не найдена")
    try:
        await client_proxies.add_for_workspace(db, workspace_id, payload.line, payload.max_accounts)
    except client_proxies.ProxyInputError as error:
        raise HTTPException(422, str(error)) from None
    await db.commit()
    return await _client_proxies_payload(db, workspace_id)


@router.put("/{proxy_id}/replace")
async def client_proxy_replace(proxy_id: int, payload: ClientProxyReplace, request: Request, db=Depends(get_db)):
    """Replace a dead or blocked proxy: profiles stay bound to it and reconnect through the new address."""
    from app.core.access import check_origin
    from app.services import client_proxies
    check_origin(request)
    proxy = await db.get(Proxy, proxy_id)
    if not proxy or proxy.pool != "client":
        raise HTTPException(404, "Прокси не найден")
    load = (await client_proxies.usage(db)).get(proxy.id, 0)
    if load and not payload.confirm_ip_change:
        raise HTTPException(422, "Подтвердите смену IP: Telegram может попросить подтвердить вход в профилях заново")
    try:
        moved = await client_proxies.replace(db, proxy, payload.line)
    except client_proxies.ProxyInputError as error:
        raise HTTPException(422, str(error)) from None
    await db.commit()
    result = await _client_proxies_payload(db, proxy.dedicated_workspace_id) if proxy.dedicated_workspace_id else {}
    return {**result, "moved": moved}
