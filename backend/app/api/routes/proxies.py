from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.db import get_db
from app.models import Proxy, TelegramAccount
from app.schemas.proxy import ProxyBulkCreate, ProxyBulkResult, ProxyRead
from app.services.proxy_parser import parse_proxy_line

router = APIRouter(prefix="/proxies", tags=["proxies"])


@router.get("", response_model=list[ProxyRead])
async def list_proxies(db: AsyncSession = Depends(get_db)):
    query = select(Proxy, func.count(TelegramAccount.id)).outerjoin(TelegramAccount).group_by(Proxy.id).order_by(Proxy.created_at.desc())
    rows = (await db.execute(query)).all()
    from app.services.client_proxies import usage
    clients = await usage(db)
    from app.models.marketing import ClientWorkspace
    names = {w.id: w.name for w in (await db.scalars(select(ClientWorkspace))).all()}
    return [ProxyRead.model_validate(proxy).model_copy(update={
        "assigned_accounts": count, "client_accounts": clients.get(proxy.id, 0),
        "dedicated_workspace_name": names.get(proxy.dedicated_workspace_id)}) for proxy, count in rows]


@router.post("/bulk", response_model=ProxyBulkResult, status_code=status.HTTP_201_CREATED)
async def bulk_create(payload: ProxyBulkCreate, db: AsyncSession = Depends(get_db)):
    existing = {(row.scheme, row.host, row.port, row.username) for row in (await db.scalars(select(Proxy))).all()}
    imported = 0
    skipped = 0
    errors: list[str] = []
    for line_number, line in enumerate(payload.entries.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = parse_proxy_line(line)
        except ValueError as exc:
            errors.append(f"Строка {line_number}: {exc}")
            skipped += 1
            continue
        key = (item.scheme, item.host, item.port, item.username)
        if key in existing:
            skipped += 1
            continue
        db.add(Proxy(scheme=item.scheme, host=item.host, port=item.port, username=item.username,
                     pool=payload.pool, password_encrypted=encrypt_secret(item.password)))
        existing.add(key)
        imported += 1
    await db.commit()
    return ProxyBulkResult(imported=imported, skipped=skipped, errors=errors[:30])


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy(proxy_id: int, db: AsyncSession = Depends(get_db)):
    proxy = await db.get(Proxy, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")
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


@router.patch("/{proxy_id}")
async def edit_proxy(proxy_id: int, payload: ProxyPatch, request: Request, db=Depends(get_db)):
    from app.core.access import check_origin
    from app.models.marketing import ClientWorkspace
    from app.services.client_proxies import usage
    check_origin(request)
    proxy = await db.get(Proxy, proxy_id)
    if not proxy:
        raise HTTPException(404, "Прокси не найден")
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
    if not proxy or proxy.pool != "client" or proxy.scheme != "socks5" or proxy.status != "working":
        raise HTTPException(422, "Выберите рабочий SOCKS5 из пула клиентов")
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
