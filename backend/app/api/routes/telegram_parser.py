import csv
import io
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import TelegramAccount
from app.models.telegram_parser import TelegramParsedContact, TelegramParseLog, TelegramParseTask
from app.schemas.telegram_parser import TelegramParseCreate

router = APIRouter(prefix="/telegram-parser", tags=["telegram-parser"])

SOURCE_TYPES = {"message": "chat_messages", "member": "chat_members", "comment": "post_comments"}


def task_view(task: TelegramParseTask) -> dict:
    return {
        "id": task.id, "account_id": task.account_id, "sources": task.sources,
        "collect_messages": task.collect_messages, "collect_members": task.collect_members,
        "collect_comments": task.collect_comments, "extended_profile": task.extended_profile,
        "per_source_limit": task.per_source_limit, "total_limit": task.total_limit,
        "activity_filter": task.activity_filter, "max_offline_days": task.max_offline_days,
        "status": task.status, "collected_count": task.collected_count,
        "processed_sources": task.processed_sources, "error": task.error,
        "created_at": task.created_at, "started_at": task.started_at, "finished_at": task.finished_at,
    }


@router.post("/tasks")
async def create_task(payload: TelegramParseCreate, db: AsyncSession = Depends(get_db)):
    account = await db.get(TelegramAccount, payload.account_id)
    if not account or not account.session_encrypted:
        raise HTTPException(422, "Выбранный Telegram-аккаунт не подключён")
    active = await db.scalar(select(TelegramParseTask.id).where(
        TelegramParseTask.account_id == payload.account_id,
        TelegramParseTask.status.in_(["queued", "running", "cancel_requested"]),
    ))
    if active:
        raise HTTPException(409, f"Для этого аккаунта уже выполняется задача #{active}")
    task = TelegramParseTask(**payload.model_dump(), status="queued")
    db.add(task)
    await db.commit()
    await db.refresh(task)
    db.add(TelegramParseLog(task_id=task.id, level="info", message="Задача добавлена в очередь."))
    await db.commit()
    return task_view(task)


@router.get("/tasks")
async def list_tasks(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(TelegramParseTask, TelegramAccount.phone, TelegramAccount.username)
        .join(TelegramAccount, TelegramAccount.id == TelegramParseTask.account_id)
        .order_by(TelegramParseTask.id.desc()).limit(100)
    )).all()
    return [{**task_view(task), "account_phone": phone, "account_username": username} for task, phone, username in rows]


@router.get("/tasks/{task_id}")
async def task_details(task_id: int, page: int = Query(1, ge=1), db: AsyncSession = Depends(get_db)):
    task = await db.get(TelegramParseTask, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    contacts = (await db.scalars(
        select(TelegramParsedContact).where(TelegramParsedContact.task_id == task_id)
        .order_by(TelegramParsedContact.id).offset((page - 1) * 100).limit(100)
    )).all()
    logs = (await db.scalars(
        select(TelegramParseLog).where(TelegramParseLog.task_id == task_id)
        .order_by(TelegramParseLog.id.desc()).limit(300)
    )).all()
    total = await db.scalar(select(func.count()).select_from(TelegramParsedContact).where(TelegramParsedContact.task_id == task_id))
    return {
        **task_view(task), "total_contacts": total, "page": page,
        "contacts": [{
            "id": c.id, "id_username": f"@{c.username}" if c.username else str(c.telegram_user_id),
            "telegram_user_id": c.telegram_user_id, "username": c.username,
            "first_name": c.first_name, "last_name": c.last_name, "phone": c.phone,
            "full_name": " ".join(filter(None, [c.first_name, c.last_name])),
            "gender": (c.details or {}).get("gender", "Не определён"),
            "bio": c.bio, "personal_channel": (c.details or {}).get("personal_channel"),
            "is_premium": c.is_premium, "activity": c.activity,
            "last_seen_at": c.last_seen_at, "source": c.source,
            "source_name": c.source_name, "found_via": c.found_via,
            "source_type": SOURCE_TYPES.get(c.found_via, c.found_via),
            "messages": (c.details or {}).get("messages", []),
            "message_count": (c.details or {}).get("message_count", 0),
        } for c in contacts],
        "logs": [{"id": row.id, "level": row.level, "message": row.message, "created_at": row.created_at} for row in logs],
    }


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.get(TelegramParseTask, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if task.status not in {"queued", "running"}:
        raise HTTPException(409, "Эта задача уже завершена")
    task.status = "cancel_requested"
    db.add(TelegramParseLog(task_id=task.id, level="warning", message="Запрошена остановка задачи."))
    await db.commit()
    return {"status": "cancel_requested"}


async def export_rows(task_id: int, db: AsyncSession):
    task = await db.get(TelegramParseTask, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return (await db.scalars(select(TelegramParsedContact).where(TelegramParsedContact.task_id == task_id).order_by(TelegramParsedContact.id))).all()


def row_values(contact: TelegramParsedContact) -> list:
    seen = contact.last_seen_at
    if seen and seen.tzinfo:
        seen = seen.astimezone(timezone.utc).replace(tzinfo=None)
    details = contact.details or {}
    username = contact.username or ""
    return [f"@{username}" if username else contact.telegram_user_id, contact.telegram_user_id, username,
            contact.first_name or "", contact.last_name or "", " ".join(filter(None, [contact.first_name, contact.last_name])),
            details.get("gender", "Не определён"), contact.bio or "", details.get("personal_channel") or "",
            contact.activity or "", seen or "", "\n---\n".join(details.get("messages") or []),
            details.get("message_count", 0), SOURCE_TYPES.get(contact.found_via, contact.found_via),
            contact.source, contact.source_name or ""]


HEADERS = ["ID/USERNAME", "ID", "USERNAME", "ИМЯ", "ФАМИЛИЯ", "ПОЛНОЕ ИМЯ", "ПОЛ", "БИОГРАФИЯ",
           "ЛИЧНЫЙ КАНАЛ", "СТАТУС ОНЛАЙН", "ПОСЛЕДНИЙ РАЗ В СЕТИ", "СООБЩЕНИЯ",
           "КОЛИЧЕСТВО СООБЩЕНИЙ", "ТИП ИСТОЧНИКА", "ССЫЛКА НА ИСТОЧНИК", "НАЗВАНИЕ ИСТОЧНИКА"]


@router.get("/tasks/{task_id}/export")
async def export_task(task_id: int, format: str = Query("csv", pattern="^(csv|xlsx)$"), db: AsyncSession = Depends(get_db)):
    rows = await export_rows(task_id, db)
    if format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Контакты"
        sheet.append(HEADERS)
        for contact in rows:
            sheet.append(row_values(contact))
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="telegram-contacts-{task_id}.xlsx"'})
    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(HEADERS)
    for contact in rows:
        writer.writerow(row_values(contact))
    return StreamingResponse(iter([stream.getvalue().encode("utf-8")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="telegram-contacts-{task_id}.csv"'})
