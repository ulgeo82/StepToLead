"""Admin-only diagnostics. Credentials never enter responses or logs."""
import io
import time
import wave
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.access import require_admin, check_origin
from app.core.config import settings
from app.db import get_db
from app.models.marketing import ClientWorkspace
from app.models.ai import AiUsage
from app.services import ai, ai_usage, call_ai

router = APIRouter(prefix="/admin/ai", tags=["admin-ai"], dependencies=[Depends(require_admin)])
FEATURES = ("chat", "summary", "calls", "campaigns")
TEST_DIALOG = ("Клиент: У меня стоматология в Самаре. Сайт есть. Бюджет на рекламу — 40 000 рублей в месяц.\n"
               "Клиент: Хочу увеличить количество записей на первичный приём.\n"
               "Менеджер: Предлагаю бесплатную консультацию на 20 минут.\n"
               "Клиент: Сначала пришлите условия. Время пока не выбрал.")


def diagnostic_input(feature):
    """Use production prompt builders with fictional knowledge, never client data."""
    messages = [{"role": "user", "content": TEST_DIALOG}]
    if feature == "calls":
        return call_ai.analysis_prompt("Тестовая компания", call_ai.DEFAULT_CHECKLIST, ""), messages
    if feature == "summary":
        return ai.SUMMARY_PROMPT, messages
    prompt = ai.suggest_prompt("Тестовая компания", "чате", {
        "knowledge": "Консультация бесплатная, длится 20 минут. Условия продвижения обсуждаются после анализа сайта. Фиксированных цен и гарантий результата нет."}, None, 600)
    if feature == "campaigns":
        from app.services.campaign_runner import reply_instruction
        prompt = reply_instruction(prompt)
    return prompt, messages


@router.get("")
async def overview(db: AsyncSession = Depends(get_db)):
    return {"functions": [{"feature": f, "provider": ai.provider(), "model": ai.model_for(f),
                           "key_set": bool(settings.llm_api_key), "configured": ai.configured(f)} for f in FEATURES],
            "stt": {"feature": "stt", "provider": call_ai.stt_provider(), "model": settings.stt_model or
                    ("general" if call_ai.stt_provider() == "yandex" else "whisper-1"),
                    "key_set": bool(call_ai.stt_key()), "configured": call_ai.stt_configured()},
            "limits": {"global": settings.ai_daily_token_limit, "workspace": settings.ai_workspace_daily_token_limit},
            "workspaces": [{"id": w.id, "name": w.name} for w in (await db.scalars(select(ClientWorkspace))).all()]}


@router.get("/usage")
async def usage(days: int = 1, db: AsyncSession = Depends(get_db)):
    if days not in (1, 7, 30):
        raise HTTPException(422, "Выберите период: 1, 7 или 30 дней")
    return {"days": days, "timezone": "UTC", "groups": await ai_usage.report(db, days)}


class TestIn(BaseModel):
    workspace_id: int | None = None


async def scope(db, workspace_id):
    if workspace_id is not None and await db.get(ClientWorkspace, workspace_id) is None:
        raise HTTPException(404, "Компания не найдена")


@router.post("/test/{feature}")
async def test(feature: str, payload: TestIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if feature not in FEATURES:
        raise HTTPException(404, "Неизвестная функция ИИ")
    await scope(db, payload.workspace_id)
    async def run():
        system, messages = diagnostic_input(feature)
        if feature == "calls":
            return await call_ai.analyze_text(system, messages, workspace_id=payload.workspace_id)
        return await ai.complete(system, messages, feature=feature, workspace_id=payload.workspace_id,
                                 max_tokens={"chat": 400, "summary": 500, "campaigns": 300}[feature])
    try:
        return await measured(run, db)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None


async def measured(run, db):
    # Report only measurements created by this invocation, not concurrent requests.
    from app.services.ai_usage import captured
    ids = []
    token = captured.set(ids)
    started = time.monotonic()
    try:
        answer = await run()
    finally:
        captured.reset(token)
    rows = (await db.scalars(select(AiUsage).where(AiUsage.id.in_(ids)))).all() if ids else []
    return {"ok": True, "answer": answer, "duration_ms": round((time.monotonic() - started) * 1000),
            "tokens_in": sum(r.tokens_in for r in rows), "tokens_out": sum(r.tokens_out for r in rows),
            "audio_seconds": sum(r.audio_seconds for r in rows)}


@router.post("/stt")
async def stt(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    check_origin(request)
    audio = await file.read(9 * 1024 * 1024 + 1)
    if len(audio) > 9 * 1024 * 1024:
        raise HTTPException(413, "Запись должна быть меньше 9 МБ")
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            seconds = wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        raise HTTPException(422, "Для теста загрузите WAV с русской речью") from None
    if not 0 < seconds <= 60:
        raise HTTPException(422, "Для теста нужна запись от 1 до 60 секунд")
    async def run():
        if call_ai.stt_provider() == "yandex":
            import asyncio
            op = await call_ai.yandex_start(audio, audio_seconds=seconds)
            for _ in range(20):
                result = await call_ai.yandex_poll(op)
                if result is not None:
                    return call_ai.transcript_text(result)
                await asyncio.sleep(1)
            raise call_ai.STTError("SpeechKit ещё обрабатывает запись, повторите тест позже")
        return call_ai.transcript_text(await call_ai.whisper(audio, audio_seconds=seconds))
    try:
        return await measured(run, db)
    except (ai.AIError, call_ai.STTError) as exc:
        raise HTTPException(422, str(exc)) from None
