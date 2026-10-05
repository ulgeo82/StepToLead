"""AI call analysis: speech-to-text of the Mango recording → summary, next step and a sales-script score.

Pipeline (driven by the telephony worker, one step per minute, nothing blocks a request):
  answered call with a downloaded recording (≥ MIN_SECONDS) → ai_status "queued"
  → speech recognition: Yandex SpeechKit v3 async (operation polled on the next ticks) or a Whisper-style API (sync)
  → LLM analysis (services.ai.complete) → meta["ai"] = {summary, need, next_step, checklist, score, ...}, ai_status "done"
  → a CALL_ANALYZED entry in the deal timeline.
Only answered calls of projects with analysis on (portal_state.ai.calls, on by default) are processed.
"""
import base64
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.crm import CrmDeal
from app.models.marketing import PortalUser, Project
from app.models.telephony import Call
from app.services import ai

logger = logging.getLogger("uvicorn.error.call_ai")
MIN_SECONDS = 25
MAX_BYTES = 9 * 1024 * 1024  # inline audio limit for SpeechKit async requests
STT_URL = "https://stt.api.cloud.yandex.net/stt/v3"
OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"
DEFAULT_CHECKLIST = [
    "Поздоровался, представился и назвал компанию",
    "Выяснил потребность клиента (что нужно, для чего, сроки, бюджет)",
    "Рассказал о решении с выгодой для клиента",
    "Отработал возражения или сомнения",
    "Договорился о конкретном следующем шаге с датой",
]


class STTError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def stt_provider() -> str:
    name = (settings.stt_provider or "").strip().lower()
    if not name and ai.provider() == "yandex":
        name = "yandex"
    if not name and ai.provider() == "openai":
        name = "openai"
    return name


def stt_key() -> str:
    return settings.stt_api_key or settings.llm_api_key


def stt_configured() -> bool:
    name = stt_provider()
    if name == "yandex":
        return bool(stt_key() and settings.yandex_folder_id)
    return name == "openai" and bool(stt_key())


def available() -> bool:
    return stt_configured() and ai.configured("calls")


def project_settings(project: Project | None) -> dict:
    data = dict(((project.portal_state if project else None) or {}).get("ai") or {})
    checklist = [line.strip(" -•\t") for line in str(data.get("call_checklist") or "").splitlines() if line.strip(" -•\t")]
    return {"enabled": data.get("calls", True) is not False, "checklist": checklist[:12] or DEFAULT_CHECKLIST}


# --------------------------------------------------------------------------- speech to text

def _yandex_headers() -> dict:
    return {"Authorization": f"Api-Key {stt_key()}", "x-folder-id": settings.yandex_folder_id}


async def _yandex_start(audio: bytes) -> str:
    if len(audio) > MAX_BYTES:
        raise STTError("Запись длиннее ~40 минут — такие звонки не распознаём")
    body = {"content": base64.b64encode(audio).decode(),
            "recognitionModel": {"model": "general", "audioFormat": {"containerAudio": {"containerAudioType": "WAV" if audio[:4] == b"RIFF" else "MP3"}},
                                 "textNormalization": {"textNormalization": "TEXT_NORMALIZATION_ENABLED", "literatureText": True},
                                 "languageRestriction": {"restrictionType": "WHITELIST", "languageCode": ["ru-RU"]}}}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{STT_URL}/recognizeFileAsync", headers=_yandex_headers(), json=body)
    if response.status_code >= 400:
        logger.warning("speechkit start status=%s", response.status_code)
        raise STTError("SpeechKit отклонил запись" + (" — проверьте ключ и права ai.speechkit-stt.user" if response.status_code in {401, 403} else ""))
    op = (response.json() or {}).get("id")
    if not op:
        raise STTError("SpeechKit не вернул номер операции")
    return op


def parse_yandex(raw: str) -> list[dict]:
    """getRecognition streams one JSON object per line; keep the normalized (refined) utterances."""
    refined, plain = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        result = item.get("result", item)
        channel = str(result.get("channelTag") or "0")
        refinement = ((result.get("finalRefinement") or {}).get("normalizedText") or {}).get("alternatives") or []
        final = (result.get("final") or {}).get("alternatives") or []
        for target, alternatives in ((refined, refinement), (plain, final)):
            if alternatives and (alternatives[0].get("text") or "").strip():
                target.append({"ch": channel, "start": int(alternatives[0].get("startTimeMs") or 0) // 1000,
                               "text": alternatives[0]["text"].strip()})
    segments = refined or plain
    return sorted(segments, key=lambda s: s["start"])


async def _yandex_poll(op: str) -> list[dict] | None:
    """None while recognition is still running."""
    async with httpx.AsyncClient(timeout=60) as client:
        status = await client.get(f"{OPERATIONS_URL}/{op}", headers=_yandex_headers())
        if status.status_code >= 400:
            raise STTError("SpeechKit: операция распознавания не найдена")
        data = status.json() or {}
        if not data.get("done"):
            return None
        if data.get("error"):
            raise STTError("SpeechKit не смог распознать запись: проверьте формат и доступ к сервису")
        result = await client.get(f"{STT_URL}/getRecognition", params={"operation_id": op}, headers=_yandex_headers())
    if result.status_code >= 400:
        raise STTError("SpeechKit не отдал результат распознавания")
    return parse_yandex(result.text)


async def yandex_poll(op, *, workspace_id=None):
    """Account final STT result/errors, not every still-running status check."""
    from app.services import ai_usage
    started = time.monotonic(); output = None; error = None
    try:
        output = await _yandex_poll(op)
        if output == []:
            raise STTError("SpeechKit не распознал речь в записи")
        return output
    except httpx.HTTPError:
        error = "SpeechKit недоступен или не ответил вовремя"
        raise STTError(error) from None
    except (STTError, ValueError, KeyError, TypeError) as exc:
        error = str(exc) if isinstance(exc, STTError) else "SpeechKit вернул некорректный ответ"
        raise STTError(error) from None
    finally:
        if output is not None or error:
            # Retrieving an already-paid operation must not be blocked by a new daily limit.
            uid = await ai_usage.reserve("stt", "yandex", "general", workspace_id, check_limits=False)
            await ai_usage.finish(uid, duration_ms=int((time.monotonic() - started) * 1000), error=error,
                                  tokens_out=ai_usage.estimate(transcript_text(output)) if output else 0)


async def _whisper(audio: bytes) -> list[dict]:
    base = (settings.stt_base_url or settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {stt_key()}"},
                                     files={"file": ("call.wav" if audio[:4] == b"RIFF" else "call.mp3", audio,
                                                     "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg")},
                                     data={"model": settings.stt_model or "whisper-1", "language": "ru",
                                           "response_format": settings.stt_response_format})
    if response.status_code >= 400:
        logger.warning("whisper status=%s", response.status_code)
        raise STTError(ai.failure(response.status_code))
    data = response.json() or {}
    segments = [{"ch": "0", "start": int(s.get("start") or 0), "text": (s.get("text") or "").strip()}
                for s in data.get("segments") or [] if (s.get("text") or "").strip()]
    return segments or ([{"ch": "0", "start": 0, "text": data["text"].strip()}] if (data.get("text") or "").strip() else [])


async def tracked_stt(operation, audio, workspace_id=None, audio_seconds=0):
    from app.services import ai_usage
    if not stt_configured():
        raise STTError("Распознавание речи не подключено: укажите провайдера и ключ")
    uid = await ai_usage.reserve("stt", stt_provider(), settings.stt_model or ("general" if stt_provider() == "yandex" else "whisper-1"),
                                 workspace_id, audio_seconds=audio_seconds)
    started = time.monotonic(); error = None; output = []
    try:
        output = await operation(audio)
        if isinstance(output, list) and not output:
            raise STTError("В записи не удалось распознать речь")
        return output
    except httpx.HTTPError:
        error = "Сервис распознавания речи недоступен или не ответил вовремя"
        raise STTError(error) from None
    except (STTError, ValueError, KeyError, TypeError) as exc:
        error = str(exc) if isinstance(exc, STTError) else "Сервис распознавания вернул некорректный ответ"
        raise STTError(error) from None
    finally:
        await ai_usage.finish(uid, duration_ms=int((time.monotonic() - started) * 1000), error=error,
                              tokens_out=ai_usage.estimate(transcript_text(output)) if isinstance(output, list) else 0)


async def whisper(audio, *, workspace_id=None, audio_seconds=0):
    return await tracked_stt(_whisper, audio, workspace_id, audio_seconds)


async def yandex_start(audio, *, workspace_id=None, audio_seconds=0):
    return await tracked_stt(_yandex_start, audio, workspace_id, audio_seconds)


def transcript_text(segments: list[dict]) -> str:
    stereo = len({s["ch"] for s in segments}) > 1
    lines = []
    for s in segments:
        stamp = f"{s['start'] // 60}:{s['start'] % 60:02d}"
        lines.append(f"[{stamp}] {'Голос ' + str(int(s['ch']) + 1) + ': ' if stereo and s['ch'].isdigit() else ''}{s['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- analysis

def analysis_prompt(project_name: str, checklist: list[str], knowledge: str) -> str:
    items = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(checklist))
    return (f"Ты руководитель отдела продаж компании «{project_name}». Тебе дают расшифровку телефонного звонка менеджера с клиентом "
            "(распознавание речи, возможны ошибки; если есть «Голос 1/2» — это разные стороны, определи сам, кто менеджер).\n"
            "Оцени звонок и верни ТОЛЬКО JSON без пояснений и markdown, строго такого вида:\n"
            '{"summary": "2–4 предложения: о чём звонок и чем закончился", "need": "что хочет клиент, с параметрами (размеры, бюджет, сроки), если названы", '
            '"next_step": "о каком следующем шаге договорились, с датой, или пусто", "objections": ["возражения клиента"], '
            '"client_mood": "позитив | нейтрально | негатив", "checklist": [{"item": "пункт", "ok": true, "comment": "коротко почему"}], '
            '"advice": "1–2 конкретных совета менеджеру, что улучшить"}\n'
            f"Пункты чек-листа (оцени каждый, порядок сохрани):\n{items}\n"
            "Оценивай только явно подтверждённые действия, не додумывай факты. "
            "Для составного пункта ok=true только если подтверждены ВСЕ его части; иначе ok=false и укажи, чего не хватает. "
            "Если возражений не было, пункт об их отработке ok=false, комментарий: «Не применимо: возражений не было». "
            "Не считай отсутствие возражений доказательством их отработки.\n"
            "Если звонок не про продажу (ошибся номером, спам, автоответчик) — summary объясняет это, checklist пустой.\n"
            + (f"\nСправка о компании (для контекста):\n{knowledge[:3000]}\n" if knowledge else ""))


def parse_json(text: str) -> dict:
    value = re.sub(r"```(?:json)?", "", text or "", flags=re.I).strip()
    for match in re.finditer(r"\{", value):
        try:
            data, _ = json.JSONDecoder().raw_decode(value[match.start():])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ai.AIError("ИИ вернул некорректный JSON анализа звонка")


def validate_analysis(raw):
    data = parse_json(raw)
    for key in ("summary", "need", "next_step", "client_mood", "advice"):
        if not isinstance(data.get(key), str):
            raise ai.AIError("ИИ вернул JSON без обязательных полей анализа звонка")
    if not data["summary"].strip() or data["client_mood"].lower() not in {"позитив", "нейтрально", "негатив"}:
        raise ai.AIError("ИИ вернул некорректный JSON: пустое резюме или неизвестное настроение")
    if not isinstance(data.get("objections"), list) or not all(isinstance(x, str) for x in data["objections"]):
        raise ai.AIError("ИИ вернул некорректный список возражений")
    if not isinstance(data.get("checklist"), list) or not all(isinstance(x, dict) and isinstance(x.get("item"), str)
            and isinstance(x.get("ok"), bool) and isinstance(x.get("comment"), str) for x in data["checklist"]):
        raise ai.AIError("ИИ вернул некорректный чек-лист звонка")
    return data


async def analyze_text(prompt, messages, *, workspace_id=None, model=None):
    for attempt in range(2):
        try:
            raw = await ai.complete(prompt, messages, max_tokens=1200, temperature=0.1, feature="calls",
                                    workspace_id=workspace_id, model=model, validator=validate_analysis)
            return validate_analysis(raw)
        except ai.AIError as exc:
            if attempt or not ("JSON" in str(exc) or "список возражений" in str(exc) or "чек-лист" in str(exc)):
                raise
            prompt = "Предыдущий ответ был некорректным. Верни только валидный JSON по указанной схеме, без обёрток и пояснений.\n" + prompt
    raise ai.AIError("ИИ не смог сформировать корректный анализ звонка")


def clean_analysis(data: dict, checklist: list[str]) -> dict:
    def text(key, limit):
        return str(data.get(key) or "").strip()[:limit]
    items = []
    for i, row in enumerate(data.get("checklist") or []):
        if isinstance(row, dict):
            items.append({"item": str(row.get("item") or (checklist[i] if i < len(checklist) else ""))[:200],
                          "ok": bool(row.get("ok")), "comment": str(row.get("comment") or "")[:300]})
    items = items[:len(checklist) or 12]
    score = round(sum(1 for i in items if i["ok"]) / len(items) * 100) if items else None
    mood = text("client_mood", 20).lower()
    return {"summary": text("summary", 1200), "need": text("need", 800), "next_step": text("next_step", 400),
            "objections": [str(o)[:200] for o in (data.get("objections") or []) if str(o).strip()][:6],
            "client_mood": mood if mood in {"позитив", "нейтрально", "негатив"} else "нейтрально",
            "checklist": items, "score": score, "advice": text("advice", 600)}


async def analyze(db: AsyncSession, call: Call, segments: list[dict]) -> None:
    """LLM part. Saves the result and a deal timeline entry. Does not commit."""
    from app.api.routes.crm import activity
    project = await db.get(Project, call.project_id)
    conf = project_settings(project)
    text = transcript_text(segments)
    meta = dict(call.meta or {})
    if len(text) < 40:
        call.ai_status = "skipped"
        meta["ai"] = {"reason": "В записи почти нет речи", "transcript": segments, "at": now().isoformat()}
        call.meta = meta
        return
    knowledge = str(((project.portal_state or {}).get("ai") or {}).get("knowledge") or "")
    data = await analyze_text(analysis_prompt(project.name, conf["checklist"], knowledge),
                            [{"role": "user", "content": f"Расшифровка звонка ({call.duration_sec // 60} мин):\n{text[-14000:]}"}],
                            workspace_id=call.workspace_id)
    result = clean_analysis(data, conf["checklist"])
    meta["ai"] = {**result, "transcript": segments[:600], "at": now().isoformat(), "provider": ai.provider_name()}
    call.meta, call.ai_status = meta, "done"
    if call.deal_id:
        deal = await db.get(CrmDeal, call.deal_id)
        user = await db.get(PortalUser, call.user_id) if call.user_id else None
        if deal:
            activity(db, deal, None, "CALL_ANALYZED", {"call_id": call.id, "summary": result["summary"][:600],
                                                       "next_step": result["next_step"], "score": result["score"],
                                                       "manager": user.display_name if user else None}, touch=False)


# --------------------------------------------------------------------------- worker step

def _fail(call: Call, message: str) -> None:
    meta = dict(call.meta or {})
    info = dict(meta.get("ai") or {})
    tries = int(info.get("tries") or 0) + 1
    info.update({"tries": tries, "error": message[:300]})
    info.pop("op", None)
    meta["ai"] = info
    call.meta = meta
    call.ai_status = "error" if tries >= 3 else "queued"


async def _audio(call: Call) -> bytes:
    import asyncio
    from app.services import telephony
    path = telephony.recording_file(call)
    if path is None:
        raise STTError("Записи разговора нет")
    return await asyncio.to_thread(path.read_bytes)


async def step(db: AsyncSession, call: Call) -> None:
    """Advance one call through the pipeline. Does not commit."""
    info = dict((call.meta or {}).get("ai") or {})
    try:
        if call.ai_status == "stt":
            if datetime.fromisoformat(info.get("since") or now().isoformat()) < now() - timedelta(minutes=40):
                raise STTError("Распознавание речи не завершилось за 40 минут")
            segments = await yandex_poll(info["op"], workspace_id=call.workspace_id)
            if segments is None:
                return
            await analyze(db, call, segments)
            return
        audio = await _audio(call)
        if stt_provider() == "yandex":
            op = await yandex_start(audio, workspace_id=call.workspace_id, audio_seconds=call.duration_sec)
            call.meta = {**(call.meta or {}), "ai": {**info, "op": op, "since": now().isoformat(), "error": None}}
            call.ai_status = "stt"
            return
        await analyze(db, call, await whisper(audio, workspace_id=call.workspace_id, audio_seconds=call.duration_sec))
    except (STTError, ai.AIError) as exc:
        _fail(call, str(exc))
        if isinstance(exc, ai.AIError) or "Дневной лимит" in str(exc):
            call.ai_status = "error"
    except httpx.HTTPError:
        _fail(call, "Сервис распознавания недоступен")


async def enqueue_new(db: AsyncSession) -> None:
    rows = (await db.scalars(select(Call).where(
        Call.ai_status.is_(None), Call.status == "answered", Call.recording_path.is_not(None),
        Call.duration_sec >= MIN_SECONDS, Call.started_at >= now() - timedelta(days=2)).limit(50))).all()
    projects: dict[int, dict] = {}
    for call in rows:
        if call.project_id not in projects:
            from app.services import plans
            conf = project_settings(await db.get(Project, call.project_id))
            projects[call.project_id] = {**conf, "enabled": conf["enabled"] and await plans.has(db, call.workspace_id, "ai_calls")}
        if (call.meta or {}).get("internal"):
            call.ai_status = "skipped"
        else:
            call.ai_status = "queued" if projects[call.project_id]["enabled"] else "skipped"


async def process(db: AsyncSession, limit: int = 4) -> int:
    """One worker tick. Commits. Returns the number of calls touched."""
    if not available():
        return 0
    await enqueue_new(db)
    await db.commit()
    rows = (await db.scalars(select(Call).where(or_(Call.ai_status == "queued", Call.ai_status == "stt"))
                             .order_by(Call.id).limit(limit))).all()
    for call in rows:
        await step(db, call)
        await db.commit()
    return len(rows)


def public(call: Call, *, full: bool = False) -> dict | None:
    """What the UI shows about the analysis of one call."""
    if not call.ai_status:
        return None
    info = (call.meta or {}).get("ai") or {}
    data = {"status": call.ai_status, "score": info.get("score"), "summary": info.get("summary"),
            "error": info.get("error") if call.ai_status == "error" else None}
    if full:
        data.update({k: info.get(k) for k in ("need", "next_step", "objections", "client_mood", "checklist", "advice", "reason", "provider", "at")})
        data["transcript"] = transcript_text(info.get("transcript") or [])
    return data
