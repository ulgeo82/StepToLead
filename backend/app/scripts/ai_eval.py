"""Explicit, billable offline evaluation. Never sends chat replies or starts campaigns."""
import argparse
import asyncio
import html
import json
import random
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import select
from app.db import SessionLocal
from app.models.marketing import ClientWorkspace, PortalUser
from app.models.crm import CrmContact
from app.models.messaging import Conversation, Message
from app.models.telephony import Call
from app.services import ai, ai_usage, call_ai


def mask(text, names=()):
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[ТЕЛЕФОН]", text)
    text = re.sub(r"(?<!\w)@[\w]{3,}", "[КОНТАКТ]", text)
    for name in sorted(set(names), key=len, reverse=True):
        if len(name.strip()) >= 2:
            text = re.sub(r"(?<!\w)" + re.escape(name.strip()) + r"(?!\w)", "[ИМЯ]", text, flags=re.I)
    # Mask self-introductions and capitalized personal names conservatively.
    text = re.sub(r"(?i)(меня зовут|имя клиента|имя менеджера)\s*[:—-]?\s*[А-ЯЁ][а-яё]+", r"\1 [ИМЯ]", text)
    text = re.sub(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?\b", "[ИМЯ]", text)
    text = re.sub(r"\b(?:Егор|Данила|Марина|Анна|Ольга|Иван|Александр|Алексей|Дмитрий|Сергей|Михаил|Елена|Наталья)\b", "[ИМЯ]", text, flags=re.I)
    return text


async def collect(args):
    cases = []; names = []
    async with SessionLocal() as db:
        if await db.get(ClientWorkspace, args.workspace) is None:
            raise ValueError("Компания не найдена")
        contacts = (await db.scalars(select(CrmContact).where(CrmContact.workspace_id == args.workspace))).all()
        users = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == args.workspace))).all()
        names = [part for row in contacts for part in (row.name or "").split()] + [part for row in users for part in (row.display_name or "").split()]
        chats = (await db.scalars(select(Conversation).where(Conversation.workspace_id == args.workspace)
                                 .order_by(Conversation.id.desc()).limit(args.chats))).all()
        for chat in chats:
            rows = (await db.scalars(select(Message).where(Message.conversation_id == chat.id, Message.status != "failed")
                                    .order_by(Message.sent_at.desc(), Message.id.desc()).limit(30))).all()
            messages = [{"role": "user" if m.direction == "in" else "assistant", "content": mask(m.text or "", names + [chat.title or ""])}
                        for m in reversed(rows) if m.direction in ("in", "out") and m.text]
            if messages:
                cases.append({"id": f"chat-{chat.id}", "feature": "chat", "messages": messages,
                              "prompt": ai.suggest_prompt("Тестовая компания", "чате", {}, None, 1500)})
                cases.append({"id": f"summary-{chat.id}", "feature": "summary", "messages": messages, "prompt": ai.SUMMARY_PROMPT})
        calls = (await db.scalars(select(Call).where(Call.workspace_id == args.workspace)
                                 .order_by(Call.id.desc()).limit(args.calls))).all()
        for call in calls:
            segments = ((call.meta or {}).get("ai") or {}).get("transcript") or (call.meta or {}).get("eval_transcript")
            if not segments:
                if not call.recording_path:
                    continue
                audio = await call_ai._audio(call)
                if call_ai.stt_provider() == "yandex":
                    op = await call_ai.yandex_start(audio, workspace_id=args.workspace, audio_seconds=call.duration_sec)
                    for _ in range(120):
                        segments = await call_ai.yandex_poll(op, workspace_id=args.workspace)
                        if segments is not None:
                            break
                        await asyncio.sleep(1)
                    if segments is None:
                        raise call_ai.STTError("Не удалось дождаться расшифровки для сравнения")
                else:
                    segments = await call_ai.whisper(audio, workspace_id=args.workspace, audio_seconds=call.duration_sec)
                call.meta = {**(call.meta or {}), "eval_transcript": segments}
                await db.commit()
            cases.append({"id": f"calls-{call.id}", "feature": "calls",
                          "messages": [{"role": "user", "content": mask(call_ai.transcript_text(segments), names)}],
                          "prompt": call_ai.analysis_prompt("Тестовая компания", call_ai.DEFAULT_CHECKLIST, "")})
    return cases


async def run(args):
    cases = await collect(args)
    directory = Path(__file__).resolve().parents[2] / "eval_reports" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    directory.mkdir(parents=True, mode=0o700)
    key = {}; sections = []
    for case in cases:
        models = list(args.models); random.SystemRandom().shuffle(models)
        key[case["id"]] = {}
        blocks = []
        for index, model in enumerate(models):
            letter = chr(65 + index); key[case["id"]][letter] = model
            started = time.monotonic(); ids = []; token = ai_usage.captured.set(ids)
            try:
                if case["feature"] == "calls":
                    answer = json.dumps(await call_ai.analyze_text(case["prompt"], case["messages"], workspace_id=args.workspace, model=model), ensure_ascii=False, indent=2)
                else:
                    answer = await ai.complete(case["prompt"], case["messages"], feature=case["feature"], workspace_id=args.workspace, model=model)
            except (ai.AIError, call_ai.STTError) as exc:
                answer = f"Ошибка: {exc}"
            finally:
                ai_usage.captured.reset(token)
            from app.models.ai import AiUsage
            async with SessionLocal() as db:
                usage = (await db.scalars(select(AiUsage).where(AiUsage.id.in_(ids)))).all() if ids else []
            stats = {"duration_ms": round((time.monotonic() - started) * 1000), "tokens_in": sum(r.tokens_in for r in usage), "tokens_out": sum(r.tokens_out for r in usage)}
            # Model names returned inside answers must not break the blind test.
            for candidate in args.models:
                answer = answer.replace(candidate, "[МОДЕЛЬ]")
            blocks.append(f"<article><h3>{letter}</h3><table><tr><th>Время, мс</th><th>Вход</th><th>Выход</th></tr><tr><td>{stats['duration_ms']}</td><td>{stats['tokens_in']}</td><td>{stats['tokens_out']}</td></tr></table><pre>{html.escape(mask(answer, ()))}</pre></article>")
        source = "\n".join(f"{'Клиент' if m['role'] == 'user' else 'Менеджер'}: {m['content']}" for m in case["messages"])
        sections.append(f"<section><h2>{html.escape(case['id'])}</h2><pre>{html.escape(source)}</pre>{''.join(blocks)}</section>")
    (directory / "report.html").write_text("<!doctype html><meta charset='utf-8'><title>Слепое сравнение ИИ</title><style>body{max-width:1100px;margin:auto;font-family:Arial}pre{white-space:pre-wrap}article,section{border:1px solid #ccc;padding:16px;margin:16px}td,th{padding:8px}</style><h1>Слепое сравнение ИИ</h1><p>Автоматическая маскировка консервативная, но не гарантирует полную анонимность. Оценивайте ответы до открытия key.json.</p>" + "".join(sections), encoding="utf-8")
    (directory / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(cases)} кейсов. Отчёт: {directory / 'report.html'}")


def main():
    parser = argparse.ArgumentParser(description="Платное слепое сравнение ИИ без отправки сообщений клиентам")
    parser.add_argument("--workspace", type=int, required=True)
    parser.add_argument("--chats", type=int, default=20)
    parser.add_argument("--calls", type=int, default=20)
    parser.add_argument("--models", required=True)
    args = parser.parse_args()
    args.models = list(dict.fromkeys(m.strip() for m in args.models.split(",") if m.strip()))
    if not 1 <= len(args.models) <= 26 or not 0 <= args.chats <= 100 or not 0 <= args.calls <= 100:
        parser.error("Нужно 1–26 моделей, 0–100 переписок и звонков")
    try:
        asyncio.run(run(args))
    except (ValueError, ai.AIError, call_ai.STTError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
