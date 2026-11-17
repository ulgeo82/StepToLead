import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from opentele2.exception import OpenTeleException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
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

from app.domains.leadgen import space
from app.services import tg_space

# Аккаунты — в пространстве выбранного клиента (этап B, docs/OUTREACH_INBOX.md). Номер телефона уникален на
# весь портал: один аккаунт не может работать у двух клиентов.
router = APIRouter(prefix="/telegram-accounts", tags=["telegram-accounts"], dependencies=[Depends(space.bind)])


def own_proxy(proxy) -> bool:
    """Прокси аутрича этого же клиента."""
    return proxy is not None and proxy.pool == "agency" and tg_space.owns(proxy.workspace_id)


async def get_account_with_proxy(account_id: int, db: AsyncSession) -> TelegramAccount:
    query = select(TelegramAccount).options(selectinload(TelegramAccount.proxy)).where(TelegramAccount.id == account_id)
    account = await db.scalar(query)
    return tg_space.require(account, "Telegram-аккаунт не найден")


@router.get("/config", response_model=TelegramConfigRead)
async def telegram_config():
    return TelegramConfigRead(
        configured=settings.encryption_secret != "change-this-before-production"
    )


@router.get("", response_model=list[TelegramAccountRead])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    query = tg_space.scope(select(TelegramAccount), TelegramAccount.workspace_id) \
        .order_by(TelegramAccount.created_at.desc())
    accounts = list((await db.scalars(query)).all())
    return await _with_health(db, accounts)


async def _with_health(db, accounts):
    """Аккаунты + их прокси (без пароля) и здоровье (services/account_health.py)."""
    from app.services import account_health, proxy_health
    now = datetime.now(timezone.utc)
    proxies = {p.id: p for p in (await db.scalars(select(Proxy))).all()}
    dups = proxy_health.duplicates(proxies.values())
    levels = {pid: proxy_health.view(p, dups.get(pid))["level"] for pid, p in proxies.items()}
    stats = await account_health.message_stats(db, [a.id for a in accounts], now)
    out = []
    for account in accounts:
        proxy = proxies.get(account.proxy_id)
        out.append(TelegramAccountRead.model_validate(account).model_copy(update={
            "proxy_info": {"id": proxy.id, "host": proxy.host, "port": proxy.port, "scheme": proxy.scheme,
                           "status": proxy.status, "health_level": levels.get(proxy.id)} if proxy else None,
            "health": account_health.compute(account, levels.get(account.proxy_id), stats.get(account.id), now)}))
    return out


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
    if proxy_id and not own_proxy(proxy):
        raise HTTPException(status_code=404, detail="Выбранный прокси не найден")
    if proxy is None:
        raise HTTPException(status_code=422, detail="Выберите прокси: без него аккаунт не подключаем")
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
                workspace_id=tg_space.current(),
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
    if account and not tg_space.owns(account.workspace_id):
        raise HTTPException(status_code=409, detail="Этот аккаунт уже подключён у другого клиента")
    proxy = await db.get(Proxy, payload.proxy_id) if payload.proxy_id else None
    if payload.proxy_id and not own_proxy(proxy):
        raise HTTPException(status_code=404, detail="Выбранный прокси не найден")
    if proxy is None:
        raise HTTPException(status_code=422, detail="Выберите прокси: без него аккаунт не подключаем")
    if account and account.session_encrypted:
        raise HTTPException(status_code=409, detail="Этот аккаунт уже подключён")
    if not account:
        account = TelegramAccount(phone=phone, proxy_id=payload.proxy_id, status="connecting",
                                  workspace_id=tg_space.current())
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
    account = tg_space.require(await db.get(TelegramAccount, account_id), "Telegram-аккаунт не найден")
    proxy = await db.get(Proxy, payload.proxy_id) if payload.proxy_id else None
    if payload.proxy_id and not own_proxy(proxy):
        raise HTTPException(status_code=404, detail="Прокси агентства не найден")
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
    account = tg_space.require(await db.get(TelegramAccount, account_id), "Telegram-аккаунт не найден")
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


# ---------------------------------------------------------------- этап A: здоровье, массовый импорт, прокси, спам-блок, аватарки
# Контракт — docs/TELEGRAM_INFRA.md. Аккаунт никогда не подключается к Telegram без прокси.

@router.get("/health-summary")
async def health_summary(db: AsyncSession = Depends(get_db)):
    from app.services import proxy_health, telegram_jobs
    accounts = await _with_health(db, list((await db.scalars(
        tg_space.scope(select(TelegramAccount), TelegramAccount.workspace_id))).all()))
    acc = {"total": len(accounts), "ok": 0, "warn": 0, "dead": 0, "waiting_proxy": 0, "spamblock": 0}
    for a in accounts:
        acc[a.health["level"]] = acc.get(a.health["level"], 0) + 1
        acc["waiting_proxy"] += a.proxy_id is None
        acc["spamblock"] += a.health["spamblock"].get("state") == "limited"
    proxies = list((await db.scalars(tg_space.scope(select(Proxy).where(Proxy.pool == "agency"),
                                                    Proxy.workspace_id))).all())
    dups = proxy_health.duplicates(proxies)
    prx = {"total": len(proxies), "ok": 0, "warn": 0, "dead": 0, "unknown": 0}
    for p in proxies:
        level = proxy_health.view(p, dups.get(p.id))["level"]
        prx[level] = prx.get(level, 0) + 1
    prx["free"] = len(await telegram_jobs.free_proxies(db, 10_000, workspace_id=tg_space.current()))
    return {"accounts": acc, "proxies": prx}


@router.post("/import-batch", status_code=status.HTTP_202_ACCEPTED)
async def import_batch(files: list[UploadFile] = File(...), mode: str = Form(default="auto"),
                       proxy_ids: str = Form(default=""), db: AsyncSession = Depends(get_db)):
    """Массовый импорт tdata: каждому аккаунту — свой прокси до первого входа (auto — свободный из пула «Аутрич»,
    order — i-й файл с i-м прокси). Не хватило прокси — файл не подключается (status waiting_proxy)."""
    import os
    import tempfile
    from app.services import telegram_jobs
    ensure_session_storage_configured()
    if mode not in ("auto", "order"):
        raise HTTPException(422, "Режим: auto или order")
    if not files:
        raise HTTPException(422, "Выберите хотя бы один ZIP-файл")
    if len(files) > 200:
        raise HTTPException(422, "За один раз можно импортировать не более 200 аккаунтов")
    if mode == "order":
        try:
            ordered = [int(x) for x in proxy_ids.replace(" ", "").split(",") if x]
        except ValueError:
            raise HTTPException(422, "proxy_ids — номера прокси через запятую") from None
        if len(ordered) != len(files):
            raise HTTPException(422, f"Файлов {len(files)}, а прокси {len(ordered)} — нужно поровну")
        found = {p.id: p for p in (await db.scalars(select(Proxy).where(Proxy.id.in_(ordered)))).all()}
        bad = [pid for pid in ordered if pid not in found or not own_proxy(found[pid])]
        if bad:
            raise HTTPException(422, f"Прокси не из пула «Аутрич» или не найдены: {', '.join(map(str, bad[:10]))}")
        dead = [pid for pid in ordered if found[pid].status == "error"]
        if dead:
            raise HTTPException(422, f"Прокси не работают: {', '.join(map(str, dead[:10]))} — проверьте или замените")
        chosen: list[int | None] = ordered
    else:
        # В auto привязки к файлам нет: свободные места резервирует фоновая задача.
        chosen = []
    folder = tempfile.mkdtemp(prefix="stl-tdata-")
    saved: list[tuple[str, str]] = []
    errors: list[dict] = []
    for i, uploaded in enumerate(files):
        name = uploaded.filename or f"account-{i + 1}.zip"
        content = await uploaded.read(MAX_ARCHIVE_SIZE + 1)
        await uploaded.close()
        if not name.lower().endswith(".zip"):
            error = ("Формат .session/.json — пока не поддерживается"
                     if name.lower().endswith((".session", ".json")) else "Нужен ZIP-файл")
            errors.append({"filename": name, "error": error})
            continue
        if len(content) > MAX_ARCHIVE_SIZE:
            errors.append({"filename": name, "error": "Размер ZIP не должен превышать 100 МБ"})
            continue
        path = os.path.join(folder, f"{i}.zip")
        with open(path, "wb") as fh:
            fh.write(content)
        saved.append((name, path))
    if not saved:
        raise HTTPException(422, "; ".join(e["error"] for e in errors[:3]) or "Нет файлов для импорта")
    if mode == "auto":
        kept = [None] * len(saved)
        free = await telegram_jobs.free_proxies(db, len(saved), workspace_id=tg_space.current())
        without_proxy = max(0, len(saved) - len(free))
    else:                # «по порядку»: прокси того файла, который был на этом месте в списке
        kept = [chosen[int(os.path.basename(path).split(".")[0])] for _, path in saved]
        without_proxy = sum(1 for pid in kept if pid is None)
    batch_id = telegram_jobs.start_import(saved, kept, workspace_id=tg_space.current(), mode=mode)
    return {"batch_id": batch_id, "queued": len(saved), "rejected": errors,
            "without_proxy": without_proxy}


@router.get("/imports/{batch_id}/progress")
async def import_progress(batch_id: int):
    from app.services import progress, telegram_jobs
    return progress.get(telegram_jobs.key("import", batch_id)) or {"state": "idle"}


@router.get("/imports/{batch_id}")
async def import_result(batch_id: int):
    from app.services import telegram_jobs
    result = telegram_jobs.results.get(telegram_jobs.key("import", batch_id))
    if result is None:
        raise HTTPException(404, "Итог импорта не найден (хранится 1 час)")
    return {"state": result.get("state"), "items": result.get("items") or [],
            "counts": result.get("counts") or {}, "free_proxies": result.get("free_proxies")}


class ReplaceProxyIn(BaseModel):
    proxy_id: int | None = None
    confirm_ip_change: bool = False


@router.post("/{account_id}/replace-proxy", response_model=TelegramAccountRead)
async def replace_proxy(account_id: int, payload: ReplaceProxyIn, db: AsyncSession = Depends(get_db)):
    from app.services import telegram_jobs
    account = tg_space.require(await db.get(TelegramAccount, account_id), "Telegram-аккаунт не найден")
    if account.proxy_id and account.session_encrypted and not payload.confirm_ip_change:
        raise HTTPException(422, "Подтвердите смену IP: Telegram может ограничить аккаунт")
    if payload.proxy_id:
        proxy = await db.get(Proxy, payload.proxy_id)
        if not own_proxy(proxy) or not tg_space.same_space(proxy.workspace_id, account.workspace_id):
            raise HTTPException(422, "Выберите прокси из пула «Аутрич» этого клиента")
        if proxy.status == "error":
            raise HTTPException(422, "Этот прокси не работает")
        busy = await db.scalar(select(func.count(TelegramAccount.id)).where(
            TelegramAccount.proxy_id == proxy.id, TelegramAccount.id != account.id))
        if busy >= (proxy.max_accounts or 1):
            raise HTTPException(409, "Прокси уже занят")
    else:
        free = await telegram_jobs.free_proxies(db, 1, workspace_id=account.workspace_id or tg_space.current())
        if not free:
            raise HTTPException(409, "Нет свободных рабочих прокси — добавьте прокси")
        proxy = free[0]
    account.proxy_id = proxy.id
    if account.status == "waiting_proxy":
        account.status = "disconnected"
    await db.commit()
    await db.refresh(account)
    return (await _with_health(db, [account]))[0]


class IdsIn(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=5000)


@router.post("/assign-proxies")
async def assign_proxies(payload: IdsIn, db: AsyncSession = Depends(get_db)):
    """Раздать свободные прокси аккаунтам без прокси (сами аккаунты не подключаются — это сделают кампании)."""
    from app.services import telegram_jobs
    query = tg_space.scope(select(TelegramAccount).where(TelegramAccount.proxy_id.is_(None)),
                           TelegramAccount.workspace_id).order_by(TelegramAccount.id)
    if payload.ids:
        query = query.where(TelegramAccount.id.in_(payload.ids))
    waiting = list((await db.scalars(query)).all())
    free = await telegram_jobs.free_proxies(db, len(waiting), workspace_id=tg_space.current())
    for account, proxy in zip(waiting, free):
        account.proxy_id = proxy.id
        if account.status == "waiting_proxy":
            account.status = "disconnected"
    await db.commit()
    return {"assigned": min(len(waiting), len(free)), "still_waiting": max(0, len(waiting) - len(free))}


@router.post("/spamcheck", status_code=status.HTTP_202_ACCEPTED)
async def spamcheck(payload: IdsIn, db: AsyncSession = Depends(get_db)):
    from app.services import telegram_jobs
    query = tg_space.scope(select(TelegramAccount.id).where(TelegramAccount.session_encrypted.is_not(None),
                                                            TelegramAccount.proxy_id.is_not(None)),
                           TelegramAccount.workspace_id)
    if payload.ids:
        query = query.where(TelegramAccount.id.in_(payload.ids))
    ids = list((await db.scalars(query.order_by(TelegramAccount.id))).all())
    if not ids:
        raise HTTPException(422, "Нет аккаунтов с сессией и прокси для проверки")
    return {"batch_id": telegram_jobs.start_spamcheck(ids)}


@router.get("/spamcheck/{batch_id}/progress")
async def spamcheck_progress(batch_id: int):
    from app.services import progress, telegram_jobs
    return progress.get(telegram_jobs.key("spamcheck", batch_id)) or {"state": "idle"}


AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


async def _read_photo(upload: UploadFile) -> bytes:
    if upload.content_type not in AVATAR_TYPES:
        raise HTTPException(422, "Фото должно быть JPG, PNG или WEBP")
    content = await upload.read(8 * 1024 * 1024 + 1)
    await upload.close()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Фото не должно быть больше 8 МБ")
    return content


@router.post("/avatars/preview")
async def avatar_preview(file: UploadFile = File(...), strength: str = Form(default="low")):
    import base64
    import secrets
    from app.services.avatar_variants import variant
    if strength not in ("low", "medium"):
        raise HTTPException(422, "Сила: low или medium")
    content = await _read_photo(file)
    try:
        variants = [await asyncio.to_thread(variant, content, secrets.token_hex(4), strength) for _ in range(4)]
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Не удалось прочитать изображение") from None
    return {"variants": ["data:image/jpeg;base64," + base64.b64encode(v).decode() for v in variants]}


@router.post("/avatars", status_code=status.HTTP_202_ACCEPTED)
async def set_avatars(files: list[UploadFile] = File(...), account_ids: str = Form(...),
                      strength: str = Form(default="low"), pause_seconds: int = Form(default=40),
                      db: AsyncSession = Depends(get_db)):
    from app.services import telegram_jobs
    if strength not in ("low", "medium"):
        raise HTTPException(422, "Сила: low или medium")
    if not 1 <= len(files) <= 20:
        raise HTTPException(422, "Загрузите от 1 до 20 фото")
    try:
        ids = [int(x) for x in account_ids.replace(" ", "").split(",") if x]
    except ValueError:
        raise HTTPException(422, "account_ids — номера аккаунтов через запятую") from None
    found = list((await db.scalars(tg_space.scope(select(TelegramAccount.id).where(
        TelegramAccount.id.in_(ids), TelegramAccount.session_encrypted.is_not(None),
        TelegramAccount.proxy_id.is_not(None)), TelegramAccount.workspace_id))).all())
    if not found:
        raise HTTPException(422, "Нет выбранных аккаунтов с сессией и прокси")
    photos = [await _read_photo(f) for f in files]
    pause = max(10, min(int(pause_seconds or 40), 600))
    return {"batch_id": telegram_jobs.start_avatars(sorted(found), photos, strength, pause),
            "accounts": len(found), "skipped": len(set(ids)) - len(found)}


@router.get("/avatars/{batch_id}/progress")
async def avatars_progress(batch_id: int):
    from app.services import progress, telegram_jobs
    return progress.get(telegram_jobs.key("avatars", batch_id)) or {"state": "idle"}
