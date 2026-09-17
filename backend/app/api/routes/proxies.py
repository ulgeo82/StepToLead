from fastapi import APIRouter, Depends, HTTPException, status
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
    return [ProxyRead.model_validate(proxy).model_copy(update={"assigned_accounts": count}) for proxy, count in rows]


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
        db.add(Proxy(scheme=item.scheme, host=item.host, port=item.port, username=item.username, password_encrypted=encrypt_secret(item.password)))
        existing.add(key)
        imported += 1
    await db.commit()
    return ProxyBulkResult(imported=imported, skipped=skipped, errors=errors[:30])


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy(proxy_id: int, db: AsyncSession = Depends(get_db)):
    proxy = await db.get(Proxy, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")
    accounts = list((await db.scalars(select(TelegramAccount).where(TelegramAccount.proxy_id == proxy_id))).all())
    for account in accounts:
        account.proxy_id = None
    await db.delete(proxy)
    await db.commit()

