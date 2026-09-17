import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import get_db
from app.models import Campaign, TelegramAccount, Lead
from app.models.automation import CampaignRuntime, CampaignAccount, CampaignDialog, CampaignMessage, CampaignEvent, GlobalBlock
from app.schemas.automation import AutomationSettings, AutomationUpdate, BlockCreate
from app.services.campaign_runner import AIProviderError, request_ai
from app.services.campaign_log import add_campaign_event

router = APIRouter(tags=["automation"])


async def runtime_for(campaign_id, db, lock=False):
    if not await db.get(Campaign, campaign_id):
        raise HTTPException(404, "Кампания не найдена")
    query = select(CampaignRuntime).where(CampaignRuntime.campaign_id == campaign_id)
    runtime = await db.scalar(query.with_for_update() if lock else query)
    if not runtime:
        runtime = CampaignRuntime(campaign_id=campaign_id, status="paused", settings={})
        db.add(runtime)
        await db.flush()
    return runtime


async def view(runtime, db):
    account_ids = list(await db.scalars(select(CampaignAccount.account_id).where(CampaignAccount.campaign_id == runtime.campaign_id)))
    counts = dict((await db.execute(select(CampaignMessage.status, func.count()).where(CampaignMessage.campaign_id == runtime.campaign_id).group_by(CampaignMessage.status))).all())
    return {"campaign_id": runtime.campaign_id, "status": runtime.status,
            "settings": AutomationSettings(**runtime.settings).model_dump(), "account_ids": account_ids,
            "api_key_set": bool(runtime.api_key_encrypted), "last_error": runtime.last_error,
            "next_action_at": runtime.next_action_at, "counts": counts}


@router.get("/campaigns/{campaign_id}/automation")
async def get_automation(campaign_id: int, db: AsyncSession = Depends(get_db)):
    runtime = await runtime_for(campaign_id, db)
    result = await view(runtime, db)
    await db.commit()
    return result


@router.put("/campaigns/{campaign_id}/automation")
async def save_automation(campaign_id: int, payload: AutomationUpdate, db: AsyncSession = Depends(get_db)):
    runtime = await runtime_for(campaign_id, db, True)
    if runtime.status == "running":
        raise HTTPException(409, "Сначала поставьте кампанию на паузу")
    ids = sorted(set(payload.account_ids))
    orphan = await db.scalar(select(CampaignDialog.id).where(CampaignDialog.campaign_id == campaign_id, CampaignDialog.account_id.not_in(ids), CampaignDialog.state.in_(["active", "manager_requested", "partner_requested", "negative_requested"])))
    if orphan:
        raise HTTPException(409, "У удаляемого из кампании аккаунта есть активные диалоги. Сначала остановите их.")
    for account_id in ids:
        account = await db.get(TelegramAccount, account_id)
        if not account or not account.session_encrypted:
            raise HTTPException(422, f"Аккаунт #{account_id} не подключён")
    runtime.settings = payload.settings.model_dump()
    if payload.api_key is not None:
        runtime.api_key_encrypted = encrypt_secret(payload.api_key.strip())
    runtime.last_error = None
    await db.execute(delete(CampaignAccount).where(CampaignAccount.campaign_id == campaign_id))
    db.add_all([CampaignAccount(campaign_id=campaign_id, account_id=i) for i in ids])
    await db.commit()
    await add_campaign_event(db, campaign_id, "info", "settings_saved", "Настройки кампании сохранены.", details={"account_count": len(ids)})
    return await view(runtime, db)


@router.post("/campaigns/{campaign_id}/test-ai")
async def test_ai(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Проверяет настройки ИИ без запуска кампании и Telegram-отправок."""
    runtime = await runtime_for(campaign_id, db)
    settings = AutomationSettings(**runtime.settings)
    key = decrypt_secret(runtime.api_key_encrypted)
    if not (settings.system_prompt.strip() and settings.ai_model.strip() and settings.ai_url and key):
        raise HTTPException(422, "Сначала сохраните промпт, адрес API, модель и API-ключ")
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(
                request_ai,
                settings,
                key,
                [{"role": "user", "content": "Это проверка подключения. Ответьте одной короткой фразой на русском языке."}],
            ),
            timeout=35,
        )
    except AIProviderError as exc:
        runtime.last_error = str(exc)
        await db.commit()
        await add_campaign_event(db, campaign_id, "error", "ai_test_failed", runtime.last_error, details={"http_status": exc.status, "provider_code": exc.code})
        raise HTTPException(502, runtime.last_error) from exc
    except Exception as exc:
        runtime.last_error = "Не удалось проверить ИИ. Проверьте адрес API, модель, ключ и сетевой доступ backend."
        await db.commit()
        await add_campaign_event(db, campaign_id, "error", "ai_test_failed", runtime.last_error, details={"error_type": type(exc).__name__})
        raise HTTPException(502, runtime.last_error) from exc
    runtime.last_error = None
    await db.commit()
    await add_campaign_event(db, campaign_id, "success", "ai_test_success", "Проверка подключения к ИИ успешно завершена.")
    return {"status": "ok", "answer": answer}


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(82462027)"))
    # Lock the same row as sending: pause/start never races a send admission.
    runtime = await runtime_for(campaign_id, db, True)
    settings = AutomationSettings(**runtime.settings)
    ids = list(await db.scalars(select(CampaignAccount.account_id).where(CampaignAccount.campaign_id == campaign_id)))
    if not ids:
        raise HTTPException(422, "Выберите аккаунты для кампании")
    if settings.auto_reply and not (settings.system_prompt.strip() and settings.ai_model.strip() and settings.ai_url and runtime.api_key_encrypted):
        raise HTTPException(422, "Для ИИ-ответов заполните промпт, адрес API, модель и ключ")
    if settings.auto_reply:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    request_ai,
                    settings,
                    decrypt_secret(runtime.api_key_encrypted),
                    [{"role": "user", "content": "Проверка перед запуском. Ответьте одним словом: готово."}],
                ),
                timeout=35,
            )
        except AIProviderError as exc:
            runtime.last_error = f"Запуск отменён: {exc}"
            await db.commit()
            await add_campaign_event(db, campaign_id, "error", "campaign_start_blocked", runtime.last_error, details={"http_status": exc.status, "provider_code": exc.code})
            raise HTTPException(422, runtime.last_error) from exc
        except Exception as exc:
            runtime.last_error = "Запуск отменён: ИИ-сервис не прошёл проверку подключения."
            await db.commit()
            await add_campaign_event(db, campaign_id, "error", "campaign_start_blocked", runtime.last_error, details={"error_type": type(exc).__name__})
            raise HTTPException(502, runtime.last_error) from exc
    if not await db.scalar(select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign_id)):
        raise HTTPException(422, "Сначала импортируйте базу получателей")
    conflict = await db.scalar(
        select(CampaignAccount.campaign_id)
        .select_from(CampaignAccount)
        .join(CampaignRuntime, CampaignRuntime.campaign_id == CampaignAccount.campaign_id)
        .where(
            CampaignAccount.account_id.in_(ids),
            CampaignRuntime.status == "running",
            CampaignAccount.campaign_id != campaign_id,
        )
    )
    if conflict:
        raise HTTPException(409, f"Один из аккаунтов уже занят запущенной кампанией #{conflict}")
    runtime.status = "running"
    runtime.last_error = None
    runtime.next_action_at = None
    await db.commit()
    lead_count = await db.scalar(select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign_id))
    await add_campaign_event(db, campaign_id, "success", "campaign_started", "Кампания запущена, планировщик начал обработку очереди.", details={"account_count": len(ids), "lead_count": lead_count})
    return await view(runtime, db)


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    runtime = await runtime_for(campaign_id, db, True)
    runtime.status = "paused"
    await db.commit()
    await add_campaign_event(db, campaign_id, "info", "campaign_paused", "Кампания поставлена на паузу пользователем.")
    return await view(runtime, db)


@router.get("/campaigns/{campaign_id}/dialogs")
async def dialogs(campaign_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(CampaignDialog).where(CampaignDialog.campaign_id == campaign_id).order_by(CampaignDialog.id.desc()).limit(200))).all()
    return [{"id": d.id, "username": d.username, "account_id": d.account_id, "state": d.state,
             "peer_id": d.peer_id, "followup_sent": d.followup_sent} for d in rows]


@router.get("/campaigns/{campaign_id}/messages")
async def messages(campaign_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CampaignMessage, CampaignDialog.username).join(CampaignDialog).where(CampaignMessage.campaign_id == campaign_id).order_by(CampaignMessage.id.desc()).limit(200))).all()
    return [{"id": m.id, "dialog_id": m.dialog_id, "username": username, "account_id": m.account_id,
             "kind": m.kind, "body": m.body, "status": m.status, "error": m.error,
             "created_at": m.created_at} for m, username in rows]


@router.get("/campaigns/{campaign_id}/logs")
async def campaign_logs(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Returns newest sanitized operational events for live UI monitoring."""
    if not await db.get(Campaign, campaign_id):
        raise HTTPException(404, "Кампания не найдена")
    rows = (await db.scalars(select(CampaignEvent).where(CampaignEvent.campaign_id == campaign_id).order_by(CampaignEvent.id.desc()).limit(300))).all()
    return [{"id": row.id, "level": row.level, "event": row.event, "message": row.message,
             "account_id": row.account_id, "username": row.username, "details": row.details,
             "created_at": row.created_at} for row in rows]


@router.post("/campaigns/{campaign_id}/dialogs/{dialog_id}/stop")
async def stop_dialog(campaign_id: int, dialog_id: int, db: AsyncSession = Depends(get_db)):
    await runtime_for(campaign_id, db, True)
    dialog = await db.get(CampaignDialog, dialog_id)
    if not dialog or dialog.campaign_id != campaign_id:
        raise HTTPException(404, "Диалог не найден")
    dialog.state = "stopped"
    await db.commit()
    await add_campaign_event(db, campaign_id, "info", "dialog_stopped", "Диалог остановлен пользователем.", account_id=dialog.account_id, username=dialog.username)
    return {"status": "stopped"}


@router.post("/campaigns/{campaign_id}/dialogs/{dialog_id}/handoff/{kind}")
async def handoff_dialog(campaign_id: int, dialog_id: int, kind: str, db: AsyncSession = Depends(get_db)):
    runtime = await runtime_for(campaign_id, db, True)
    settings = AutomationSettings(**runtime.settings)
    if kind not in {"manager", "partner"}:
        raise HTTPException(422, "Неизвестный вид передачи")
    destination = (settings.partner_chat or settings.positive_chat) if kind == "partner" else settings.positive_chat
    if not destination:
        raise HTTPException(422, "Укажите чат для пересылки в настройках")
    dialog = await db.get(CampaignDialog, dialog_id)
    if not dialog or dialog.campaign_id != campaign_id:
        raise HTTPException(404, "Диалог не найден")
    dialog.state = kind + "_requested"
    await db.commit()
    await add_campaign_event(db, campaign_id, "info", "handoff_requested", "Запрошена передача диалога.", account_id=dialog.account_id, username=dialog.username, details={"kind": kind})
    return {"status": dialog.state}


@router.get("/telegram-blocklist")
async def blocks(db: AsyncSession = Depends(get_db)):
    return [{"peer_id": b.peer_id, "username": b.username} for b in await db.scalars(select(GlobalBlock))]


@router.post("/telegram-blocklist")
async def add_block(payload: BlockCreate, db: AsyncSession = Depends(get_db)):
    row = await db.get(GlobalBlock, payload.peer_id)
    if not row:
        db.add(GlobalBlock(**payload.model_dump()))
    await db.commit()
    return {"status": "blocked"}


@router.delete("/telegram-blocklist/{peer_id}")
async def remove_block(peer_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(GlobalBlock).where(GlobalBlock.peer_id == peer_id))
    await db.commit()
    return {"status": "removed"}
