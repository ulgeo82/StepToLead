import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from opentele2.exception import OpenTeleException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.access import require_admin
from app.core.crypto import encrypt_secret
from app.db import SessionLocal, get_db
from app.models import Proxy, TelegramAccount
from app.schemas.telegram_account import (
    AccountProxyUpdate,
    AuthConfirmCode,
    AuthRequestCode,
    AuthResult,
    TelegramAccountRead,
    TelegramConfigRead,
    TDataImportItem,
    TDataImportResult,
    UsernameAvailability,
    UsernameCheck,
)
from app.services.account_status import status_hub
from app.services.telegram import (
    check_username,
    confirm_login,
    ensure_telegram_configured,
    ensure_session_storage_configured,
    request_login_code,
    proxy_config,
    suggest_username,
    sync_account_from_telegram,
    update_telegram_profile,
)
from app.services.tdata_import import MAX_ARCHIVE_SIZE, TelegramConnectionError, convert_tdata_archive

router = APIRouter(prefix="/telegram-accounts", tags=["telegram-accounts"])


async def get_account_with_proxy(account_id: int, db: AsyncSession) -> TelegramAccount:
    query = select(TelegramAccount).options(selectinload(TelegramAccount.proxy)).where(TelegramAccount.id == account_id)
    account = await db.scalar(query)
    if not account:
        raise HTTPException(status_code=404, detail="Telegram-аккаунт не найден")
    return account


@router.get("/config", response_model=TelegramConfigRead)
async def telegram_config():
    return TelegramConfigRead(
        configured=settings.encryption_secret != "change-this-before-production"
    )


@router.get("", response_model=list[TelegramAccountRead])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    query = select(TelegramAccount).order_by(TelegramAccount.created_at.desc())
    return list((await db.scalars(query)).all())


@router.post("/import-tdata", response_model=TDataImportResult)
async def import_tdata(
    files: list[UploadFile] = File(...),
    proxy_id: int | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    ensure_session_storage_configured()
    if not files:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один ZIP-файл")
    if len(files) > 50:
        raise HTTPException(status_code=422, detail="За один раз можно импортировать не более 50 аккаунтов")

    proxy = await db.get(Proxy, proxy_id) if proxy_id else None
    if proxy_id and not proxy:
        raise HTTPException(status_code=404, detail="Выбранный прокси не найден")
    connection_proxy = proxy_config(proxy)

    items: list[TDataImportItem] = []
    for uploaded in files:
        filename = uploaded.filename or "account.zip"
        if not filename.lower().endswith(".zip"):
            items.append(TDataImportItem(filename=filename, status="error", error="Нужен ZIP-файл"))
            await uploaded.close()
            continue
        try:
            content = await uploaded.read(MAX_ARCHIVE_SIZE + 1)
            if len(content) > MAX_ARCHIVE_SIZE:
                raise ValueError("Размер ZIP не должен превышать 100 МБ")
            try:
                imported = await asyncio.wait_for(
                    convert_tdata_archive(content, connection_proxy),
                    timeout=60,
                )
            except TimeoutError as exc:
                raise ValueError(
                    "Telegram не ответил за отведённое время. Проверьте или замените прокси."
                ) from exc
            if proxy:
                proxy.status = "active"
                proxy.last_error = None
                proxy.last_checked_at = datetime.now(timezone.utc)
            duplicate = await db.scalar(
                select(TelegramAccount).where(
                    (TelegramAccount.telegram_user_id == imported.telegram_user_id)
                    | (TelegramAccount.phone == imported.phone)
                )
            )
            if duplicate:
                if proxy:
                    await db.commit()
                items.append(TDataImportItem(
                    filename=filename,
                    status="skipped",
                    account_id=duplicate.id,
                    phone=duplicate.phone,
                    error="Аккаунт уже добавлен",
                ))
                continue

            account = TelegramAccount(
                phone=imported.phone,
                telegram_user_id=imported.telegram_user_id,
                first_name=imported.first_name,
                last_name=imported.last_name,
                username=imported.username,
                bio=imported.bio,
                session_encrypted=encrypt_secret(imported.session),
                proxy_id=proxy_id,
                status="online",
                last_seen_at=datetime.now(timezone.utc),
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
            await status_hub.broadcast(account)
            items.append(TDataImportItem(filename=filename, status="imported", account_id=account.id, phone=account.phone))
        except TelegramConnectionError as exc:
            await db.rollback()
            if proxy:
                proxy.status = "error"
                proxy.last_error = str(exc)[:500]
                proxy.last_checked_at = datetime.now(timezone.utc)
                await db.commit()
            items.append(TDataImportItem(filename=filename, status="error", error=str(exc)))
        except (ValueError, OpenTeleException) as exc:
            await db.rollback()
            items.append(TDataImportItem(filename=filename, status="error", error=str(exc)))
        except Exception as exc:
            await db.rollback()
            message = str(exc).strip() or type(exc).__name__
            items.append(TDataImportItem(
                filename=filename,
                status="error",
                error=f"Не удалось подключить tdata: {message}"[:500],
            ))
        finally:
            await uploaded.close()

    return TDataImportResult(
        imported=sum(item.status == "imported" for item in items),
        skipped=sum(item.status == "skipped" for item in items),
        failed=sum(item.status == "error" for item in items),
        items=items,
    )


@router.post("/auth/request-code", response_model=TelegramAccountRead)
async def auth_request_code(payload: AuthRequestCode, db: AsyncSession = Depends(get_db)):
    phone = "+" + "".join(character for character in payload.phone if character.isdigit())
    account = await db.scalar(select(TelegramAccount).where(TelegramAccount.phone == phone))
    proxy = await db.get(Proxy, payload.proxy_id) if payload.proxy_id else None
    if payload.proxy_id and not proxy:
        raise HTTPException(status_code=404, detail="Выбранный прокси не найден")
    if account and account.session_encrypted:
        raise HTTPException(status_code=409, detail="Этот аккаунт уже подключён")
    if not account:
        account = TelegramAccount(phone=phone, proxy_id=payload.proxy_id, status="connecting")
        db.add(account)
        await db.flush()
    else:
        account.proxy_id = payload.proxy_id
        account.status = "connecting"
    try:
        await request_login_code(account, proxy)
    finally:
        await db.commit()
    await db.refresh(account)
    await status_hub.broadcast(account)
    return account


@router.post("/auth/confirm", response_model=AuthResult)
async def auth_confirm(payload: AuthConfirmCode, db: AsyncSession = Depends(get_db)):
    account = await get_account_with_proxy(payload.account_id, db)
    try:
        requires_2fa = await confirm_login(account, payload.code, payload.password, account.proxy)
    finally:
        await db.commit()
    await db.refresh(account)
    await status_hub.broadcast(account)
    return AuthResult(account=TelegramAccountRead.model_validate(account), requires_2fa=requires_2fa)


@router.patch("/{account_id}/proxy", response_model=TelegramAccountRead)
async def assign_proxy(account_id: int, payload: AccountProxyUpdate, db: AsyncSession = Depends(get_db)):
    account = await db.get(TelegramAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Telegram-аккаунт не найден")
    if payload.proxy_id and not await db.get(Proxy, payload.proxy_id):
        raise HTTPException(status_code=404, detail="Прокси не найден")
    account.proxy_id = payload.proxy_id
    account.status = "checking" if account.session_encrypted else "disconnected"
    account.last_error = None
    await db.commit()
    await db.refresh(account)
    await status_hub.broadcast(account)
    return account


@router.post("/{account_id}/sync", response_model=TelegramAccountRead)
async def sync_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await get_account_with_proxy(account_id, db)
    try:
        await sync_account_from_telegram(account)
    finally:
        await db.commit()
    await db.refresh(account)
    await status_hub.broadcast(account)
    return account


@router.patch("/{account_id}/profile", response_model=TelegramAccountRead)
async def update_profile(
    account_id: int,
    first_name: str = Form(..., min_length=1, max_length=120),
    last_name: str = Form("", max_length=120),
    bio: str = Form("", max_length=255),
    username: str = Form("", max_length=32),
    avatar: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
):
    account = await get_account_with_proxy(account_id, db)
    avatar_data: tuple[str, bytes] | None = None
    if avatar:
        if avatar.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(status_code=422, detail="Аватар должен быть JPG, PNG или WEBP")
        content = await avatar.read()
        if len(content) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Размер аватара не должен превышать 8 МБ")
        avatar_data = (avatar.filename or "avatar.jpg", content)
    try:
        await update_telegram_profile(account, first_name, last_name, bio, username, avatar_data)
    finally:
        await db.commit()
    await db.refresh(account)
    await status_hub.broadcast(account)
    return account


@router.post("/{account_id}/username/check", response_model=UsernameAvailability)
async def username_check(account_id: int, payload: UsernameCheck, db: AsyncSession = Depends(get_db)):
    account = await get_account_with_proxy(account_id, db)
    available = await check_username(account, payload.username)
    return UsernameAvailability(username=payload.username, available=available)


@router.post("/{account_id}/username/suggest", response_model=UsernameAvailability)
async def username_suggest(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await get_account_with_proxy(account_id, db)
    username = await suggest_username(account)
    return UsernameAvailability(username=username, available=True)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(TelegramAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Telegram-аккаунт не найден")
    await db.delete(account)
    await db.commit()


@router.websocket("/ws/status")
async def account_status_socket(websocket: WebSocket):
    await status_hub.connect(websocket)
    try:
        async with SessionLocal() as db:
            accounts = list((await db.scalars(select(TelegramAccount).order_by(TelegramAccount.created_at.desc()))).all())
            await websocket.send_json({"type": "snapshot", "accounts": [TelegramAccountRead.model_validate(account).model_dump(mode="json") for account in accounts]})
        while True:
            # Проверяем отзыв/истечение сессии и у уже открытого соединения.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=15)
            except TimeoutError:
                pass
            async with SessionLocal() as db:
                try:
                    await require_admin(websocket, db)
                except Exception:
                    await websocket.close(code=1008)
                    break
    except WebSocketDisconnect:
        status_hub.disconnect(websocket)
    except asyncio.CancelledError:
        status_hub.disconnect(websocket)
        raise
    finally:
        status_hub.disconnect(websocket)
