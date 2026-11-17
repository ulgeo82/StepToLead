"""Фоновые операции инфраструктуры Telegram-аутрича (docs/TELEGRAM_INFRA.md): проверка прокси, импорт аккаунтов,
проверка спам-блока, аватарки. Каждая — своя сессия БД и прогресс (services/progress.py), ход виден в общем
индикаторе задач. Пароли прокси и сессии аккаунтов не попадают ни в прогресс, ни в логи."""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
import weakref

from sqlalchemy import select

from app.services import progress

log = logging.getLogger(__name__)

session_factory = None          # подменяется в тестах; по умолчанию app.db.SessionLocal
_tasks: set[asyncio.Task] = set()
_seq = itertools.count(int(time.time()) % 1_000_000)
results: dict[str, dict] = {}   # итоги пачек (импорт) на 1 час: key -> {"at", ...}


class _ImportPool:
    """Общие резервы параллельных пачек в одном процессе приложения."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.reserved: dict[int, int] = {}


_import_pools: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def sessions():
    if session_factory is not None:
        return session_factory
    from app.db import SessionLocal
    return SessionLocal


def new_batch() -> int:
    return next(_seq)


def key(kind: str, batch_id: int) -> str:
    return f"telegram:{kind}:{batch_id}"


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def keep(result_key: str, value: dict) -> None:
    now = time.time()
    for k in [k for k, v in results.items() if now - v.get("at", now) > 3600]:
        results.pop(k, None)
    results[result_key] = {**value, "at": now}


# ---------------------------------------------------------------- проверка прокси

async def check_proxies(batch_id: int, ids: list[int], auto_ids: set[int] | frozenset = frozenset(),
                        concurrency: int = 20) -> None:
    from app.models.proxy import Proxy
    from app.services import proxy_health
    tracker = progress.tracker(key("proxy-check", batch_id))
    tracker.run()
    tracker.begin("check", total=len(ids))
    gate = asyncio.Semaphore(concurrency)
    ok = bad = 0

    async def one(pid: int) -> None:
        nonlocal ok, bad
        async with gate:
            async with sessions()() as db:
                proxy = await db.get(Proxy, pid)
                if proxy is None:
                    tracker.tick("check")
                    return
                try:
                    await proxy_health.check_one(proxy, auto_scheme=pid in auto_ids)
                except Exception:  # noqa: BLE001 — одна строка не должна ронять пачку
                    log.exception("proxy check failed id=%s", pid)
                    proxy_health.record(proxy, None, "Проверка не удалась")
                if proxy.status == "working":
                    ok += 1
                else:
                    bad += 1
                await db.commit()
            tracker.tick("check")

    try:
        await asyncio.gather(*(one(pid) for pid in ids))
        tracker.end("check", note=f"работают {ok}, не работают {bad}")
        tracker.finish("done", f"Проверено {len(ids)}: работают {ok}, не работают {bad}")
    except Exception as exc:  # noqa: BLE001
        log.exception("proxy batch failed")
        tracker.finish("error", f"Проверка прервалась: {type(exc).__name__}")


def start_proxy_check(ids: list[int], auto_ids: set[int] | None = None) -> int:
    batch_id = new_batch()
    seconds = max(5, len(ids) / 20 * 12)
    progress.start(key("proxy-check", batch_id), f"Проверка прокси ({len(ids)})",
                   [("check", "Связь с Telegram и внешний IP", seconds)], queued=True)
    spawn(check_proxies(batch_id, ids, frozenset(auto_ids or ())))
    return batch_id


# ---------------------------------------------------------------- свободные прокси аутрича

async def free_proxies(db, need: int, *, workspace_id: int | None = None,
                       reserved: dict[int, int] | None = None) -> list:
    """До need прокси пула «Аутрич» со свободными местами: только рабочие, без смены внешнего IP; сначала
    уникальные внешние IP и наименее занятые. Один прокси может попасть в список столько раз, сколько у него мест."""
    from sqlalchemy import func as sa_func
    from app.models import Proxy, TelegramAccount
    from app.services import proxy_health, tg_space
    proxies = list((await db.scalars(select(Proxy).where(Proxy.pool == "agency", Proxy.status == "working",
                                                         tg_space.belongs(Proxy.workspace_id, workspace_id))
                                     .order_by(Proxy.id))).all())
    load = dict((await db.execute(select(TelegramAccount.proxy_id, sa_func.count(TelegramAccount.id))
                                  .where(TelegramAccount.proxy_id.is_not(None))
                                  .group_by(TelegramAccount.proxy_id))).all())
    for pid, extra in (reserved or {}).items():
        load[pid] = load.get(pid, 0) + extra
    dups = proxy_health.duplicates(proxies)
    good = [p for p in proxies if not proxy_health.view(p, dups.get(p.id))["rotating"]]
    out = []
    # Прокси-дубли (тот же внешний IP, что у другого) — в последнюю очередь.
    for p in sorted(good, key=lambda x: (x.id in dups, load.get(x.id, 0), x.id)):
        slots = max(0, (p.max_accounts or 1) - load.get(p.id, 0))
        out.extend([p] * slots)
        if len(out) >= need:
            break
    return out[:need]


# ---------------------------------------------------------------- импорт аккаунтов (tdata)

async def import_accounts(batch_id: int, files: list[tuple[str, str]], proxy_ids: list[int | None],
                          concurrency: int = 3, workspace_id: int | None = None, mode: str = "order") -> None:
    """В auto места резервируются на время входа, при пропуске или ошибке возвращаются в общий пул."""
    import os
    from datetime import datetime, timezone
    from app.core.crypto import encrypt_secret
    from app.models import Proxy, TelegramAccount
    from opentele2.exception import OpenTeleException
    from app.services.tdata_import import TelegramConnectionError, convert_tdata_archive, tdata_user_id
    from app.services import tg_space
    from app.services.telegram import proxy_config
    result_key = key("import", batch_id)
    tracker = progress.tracker(result_key)
    tracker.run()
    tracker.begin("import", total=len(files))
    items: list[dict] = [{} for _ in files]
    gate = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    pool_state = _import_pools.get(loop)
    if pool_state is None:
        pool_state = _ImportPool()
        _import_pools[loop] = pool_state
    condition, reserved = pool_state.condition, pool_state.reserved

    async def acquire_proxy() -> int | None:
        async with condition:
            while True:
                async with sessions()() as db:
                    pool = await free_proxies(db, 1, workspace_id=workspace_id, reserved=reserved)
                if pool:
                    pid = pool[0].id
                    reserved[pid] = reserved.get(pid, 0) + 1
                    return pid
                if not reserved:
                    return None
                # Занятые конвертацией места ещё могут освободиться после повтора или ошибки.
                await condition.wait()

    def skipped(item, duplicate):
        same = tg_space.same_space(duplicate.workspace_id, workspace_id)
        item.update(status="skipped", account_id=duplicate.id if same else None,
                    phone=duplicate.phone if same else None,
                    error="Аккаунт уже добавлен" if same else "Аккаунт уже подключён у другого клиента")

    async def one(i: int, filename: str, path: str, proxy_id: int | None) -> None:
        async with gate:
            item = {"filename": filename, "status": "error", "account_id": None, "phone": None, "proxy": None,
                    "error": None}
            leased = False
            try:
                with open(path, "rb") as fh:
                    content = fh.read()
                if mode == "auto":
                    uid = await asyncio.to_thread(tdata_user_id, content)
                    if uid:
                        async with sessions()() as db:
                            duplicate = await db.scalar(select(TelegramAccount).where(
                                TelegramAccount.telegram_user_id == uid))
                            if duplicate:
                                skipped(item, duplicate)
                                return
                    proxy_id = await acquire_proxy()
                    leased = proxy_id is not None
                if proxy_id is None:
                    item.update(status="waiting_proxy", error="Не хватило свободных прокси — добавьте прокси и "
                                                               "загрузите этот файл снова")
                    return
                async with sessions()() as db:
                    proxy = await db.get(Proxy, proxy_id)
                    if proxy is None or not tg_space.same_space(proxy.workspace_id, workspace_id):
                        raise ValueError("Выбранный прокси недоступен")
                    item["proxy"] = f"{proxy.host}:{proxy.port}" if proxy else None
                    try:
                        imported = await asyncio.wait_for(convert_tdata_archive(content, proxy_config(proxy)), 90)
                    except asyncio.TimeoutError:
                        raise ValueError("Telegram не ответил вовремя — проверьте прокси") from None
                # Один commit за раз: два одинаковых файла не добавляют аккаунт параллельно.
                async with condition:
                    async with sessions()() as db:
                        duplicate = await db.scalar(select(TelegramAccount).where(
                            (TelegramAccount.telegram_user_id == imported.telegram_user_id)
                            | (TelegramAccount.phone == imported.phone)))
                        if duplicate:
                            skipped(item, duplicate)
                            return
                        account = TelegramAccount(
                            phone=imported.phone, telegram_user_id=imported.telegram_user_id,
                            first_name=imported.first_name, last_name=imported.last_name, username=imported.username,
                            bio=imported.bio, session_encrypted=encrypt_secret(imported.session), proxy_id=proxy_id,
                            status="online", last_seen_at=datetime.now(timezone.utc), workspace_id=workspace_id)
                        db.add(account)
                        await db.commit()
                        item.update(status="imported", account_id=account.id, phone=account.phone)
            except TelegramConnectionError as exc:
                item["error"] = str(exc)[:500]
            except ValueError as exc:
                item["error"] = str(exc)[:500]
            except OpenTeleException:
                item["error"] = "Telegram Desktop не смог прочитать эту tdata-сессию"
            except Exception as exc:  # noqa: BLE001
                log.warning("tdata import failed: %s", type(exc).__name__)
                item["error"] = f"Не удалось подключить tdata: {type(exc).__name__}"
            finally:
                if leased:
                    async with condition:
                        reserved[proxy_id] -= 1
                        if not reserved[proxy_id]:
                            del reserved[proxy_id]
                        condition.notify_all()
                items[i] = item
                try:
                    os.remove(path)
                except OSError:
                    pass
                tracker.tick("import")

    try:
        await asyncio.gather(*(one(i, f, p, proxy_ids[i]) for i, (f, p) in enumerate(files)))
    finally:
        counts = {s: sum(1 for it in items if it.get("status") == s)
                  for s in ("imported", "skipped", "waiting_proxy", "error")}
        async with condition:
            async with sessions()() as db:
                remaining = len(await free_proxies(db, 10_000, workspace_id=workspace_id, reserved=reserved))
        keep(result_key, {"state": "done", "items": items, "counts": counts, "free_proxies": remaining})
        tracker.end("import", note=f"добавлено {counts['imported']}")
        tracker.finish("done", f"Добавлено {counts['imported']}, пропущено {counts['skipped']}, "
                               f"ждут прокси {counts['waiting_proxy']}, ошибок {counts['error']}. "
                               f"Свободных мест на прокси: {remaining}")
        try:
            os.rmdir(os.path.dirname(files[0][1])) if files else None
        except OSError:
            pass


def start_import(files: list[tuple[str, str]], proxy_ids: list[int | None], workspace_id: int | None = None,
                 mode: str = "order") -> int:
    batch_id = new_batch()
    progress.start(key("import", batch_id), f"Импорт аккаунтов ({len(files)})",
                   [("import", "Вход через прокси и проверка", max(10, len(files) / 3 * 25))], queued=True)
    keep(key("import", batch_id), {"state": "running", "items": []})
    spawn(import_accounts(batch_id, files, proxy_ids, workspace_id=workspace_id, mode=mode))
    return batch_id


# ---------------------------------------------------------------- спам-блок

async def _with_account(account, fn):
    """Подключить аккаунт ТОЛЬКО через его прокси и выполнить fn(client)."""
    from app.core.crypto import decrypt_secret
    from app.services.telegram import make_client
    if account.proxy is None:
        raise RuntimeError("Нет прокси — аккаунт не подключаем")
    client = make_client(decrypt_secret(account.session_encrypted), account.proxy)
    try:
        await asyncio.wait_for(client.connect(), 20)
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия больше не авторизована")
        return await fn(client)
    finally:
        await client.disconnect()


async def spamcheck(batch_id: int, ids: list[int], pause: float = 5.0) -> None:
    from datetime import datetime, timezone
    from sqlalchemy.orm import selectinload
    from app.models import TelegramAccount
    from app.services import account_health
    tracker = progress.tracker(key("spamcheck", batch_id))
    tracker.run()
    tracker.begin("check", total=len(ids))
    limited = 0
    for n, aid in enumerate(ids):
        async with sessions()() as db:
            account = await db.scalar(select(TelegramAccount).options(selectinload(TelegramAccount.proxy))
                                      .where(TelegramAccount.id == aid))
            if account and account.session_encrypted:
                try:
                    result = await _with_account(account, account_health.ask_spambot)
                except Exception as exc:  # noqa: BLE001
                    result = {"state": "unknown", "until": None}
                    account.last_error = f"Спам-блок не проверен: {str(exc)[:200] or type(exc).__name__}"
                account_health.apply_spamblock(account, result, datetime.now(timezone.utc))
                limited += result["state"] == "limited"
                await db.commit()
        tracker.tick("check")
        if pause and n < len(ids) - 1:
            await asyncio.sleep(pause)
    tracker.end("check", note=f"со спам-блоком {limited}")
    tracker.finish("done", f"Проверено {len(ids)}, со спам-блоком {limited}")


def start_spamcheck(ids: list[int]) -> int:
    batch_id = new_batch()
    progress.start(key("spamcheck", batch_id), f"Проверка спам-блока ({len(ids)})",
                   [("check", "Вопрос @SpamBot", max(10, len(ids) * 22))], queued=True)
    spawn(spamcheck(batch_id, ids))
    return batch_id


# ---------------------------------------------------------------- аватарки

async def set_avatars(batch_id: int, ids: list[int], photos: list[bytes], strength: str, pause: float) -> None:
    import io
    from sqlalchemy.orm import selectinload
    from telethon import functions
    from telethon.errors import FloodWaitError
    from datetime import datetime, timedelta, timezone
    from app.models import TelegramAccount
    from app.services.avatar_variants import variant
    tracker = progress.tracker(key("avatars", batch_id))
    tracker.run()
    tracker.begin("set", total=len(ids))
    done = failed = 0
    for n, aid in enumerate(ids):
        async with sessions()() as db:
            account = await db.scalar(select(TelegramAccount).options(selectinload(TelegramAccount.proxy))
                                      .where(TelegramAccount.id == aid))
            if account and account.session_encrypted:
                picture = variant(photos[n % len(photos)], f"{batch_id}:{aid}", strength)

                async def upload(client):
                    stream = io.BytesIO(picture)
                    stream.name = "avatar.jpg"
                    uploaded = await client.upload_file(stream)
                    await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))

                try:
                    await _with_account(account, upload)
                    account.avatar_version = (account.avatar_version or 0) + 1
                    account.health = {**(account.health or {}), "has_photo": True}
                    from app.services.avatar_variants import thumb
                    account.avatar_thumb = thumb(picture)
                    done += 1
                except FloodWaitError as exc:
                    account.status = "flood_wait"
                    account.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
                    failed += 1
                except Exception as exc:  # noqa: BLE001
                    account.last_error = f"Аватар не поставлен: {str(exc)[:200] or type(exc).__name__}"
                    failed += 1
                await db.commit()
        tracker.tick("set")
        if pause and n < len(ids) - 1:
            await asyncio.sleep(pause)
    tracker.end("set", note=f"поставлено {done}")
    tracker.finish("done", f"Аватарки: поставлено {done}, не удалось {failed}")


def start_avatars(ids: list[int], photos: list[bytes], strength: str, pause: float) -> int:
    batch_id = new_batch()
    progress.start(key("avatars", batch_id), f"Аватарки ({len(ids)})",
                   [("set", "Уникальная аватарка каждому аккаунту", max(10, len(ids) * (pause + 8)))], queued=True)
    spawn(set_avatars(batch_id, ids, photos, strength, pause))
    return batch_id
