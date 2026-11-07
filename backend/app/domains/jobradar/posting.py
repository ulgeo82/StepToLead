"""Пост вакансии в Telegram (через бота агентства) и кнопки 👍/👎."""
from __future__ import annotations

import html

from app.domains.jobradar import sources as registry
from app.domains.jobradar.sources import Vacancy

FALLBACK = {"hh": "hh.ru", "trudvsem": "Работа России", "habr": "Хабр Карьера", "getmatch": "getmatch",
            "flru": "FL.ru", "telegram": "Telegram", "superjob": "SuperJob"}
FORMATS = {"remote": "удалёнка", "office": "офис", "hybrid": "гибрид"}


def source_name(code: str) -> str:
    return registry.names().get(code) or FALLBACK.get(code, code)

SNIPPET = 280


def salary(v: Vacancy) -> str:
    if v.kind == "project":
        budget = (v.extra or {}).get("budget")
        return f"💰 бюджет {budget:,} ₽".replace(",", " ") if isinstance(budget, int) and budget else "💰 бюджет по договорённости"
    if not getattr(v, "salary_known", True) and not v.salary_from and not v.salary_to:
        return "💰 з/п на сайте"
    if not v.salary_from and not v.salary_to:
        return "💰 з/п не указана"
    cur = "₽" if v.currency in ("RUR", "RUB", "", None) else html.escape(v.currency)
    fmt = lambda n: f"{n:,}".replace(",", " ")
    if v.salary_from and v.salary_to:
        return f"💰 {fmt(v.salary_from)}–{fmt(v.salary_to)} {cur}"
    return f"💰 от {fmt(v.salary_from)} {cur}" if v.salary_from else f"💰 до {fmt(v.salary_to)} {cur}"


def render(v: Vacancy, *, score: int | None = None, why: str = "", sources: list[str] | None = None,
           lead: bool = False) -> str:
    e = html.escape
    extra = v.extra or {}
    lines = [f"🧩 Проект: <b>{e(v.title)}</b>" if v.kind == "project" else f"<b>{e(v.title)}</b>"]
    if v.company:
        lines.append(f"🏢 {e(v.company)}")
    work = FORMATS.get(str(extra.get("work_format") or "").lower())
    place = "🌍 удалёнка" if v.remote else (f"📍 {e(v.city)}" if v.city else "")
    if work and work != "удалёнка":
        place = " · ".join(x for x in (place, f"🏢 {work}") if x)
    lines.append(" · ".join(x for x in (salary(v), place) if x))
    if extra.get("deadline"):
        lines.append(f"⏳ срок: {e(str(extra['deadline'])[:60])}")
    if extra.get("stack"):
        stack = extra["stack"] if isinstance(extra["stack"], list) else [extra["stack"]]
        lines.append("🧰 " + e(", ".join(str(x) for x in stack[:8])))
    if score is not None:
        lines.append(f"🎯 {score}/100" + (f" — {e(why)}" if why else ""))
    if lead:
        lines.append("📇 Компания добавлена в «Базу компаний» лидогенерации")
    if v.snippet:
        text = v.snippet if len(v.snippet) <= SNIPPET else v.snippet[:SNIPPET - 1].rstrip() + "…"
        lines.append(f"\n<i>{e(text)}</i>")
    names = ", ".join(source_name(s) for s in (sources or [v.source]))
    label = "Открыть пост" if v.source == "telegram" else "Открыть проект" if v.kind == "project" else "Открыть вакансию"
    link = f'<a href="{e(v.url, quote=True)}">{label}</a> · ' if v.url.startswith(("https://", "http://")) else ""
    lines.append(f"\n🔗 {link}{e(names)}")
    return "\n".join(lines)


def keyboard(match_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "👍 Подходит", "callback_data": f"jr:{match_id}:1"},
                                 {"text": "👎 Мимо", "callback_data": f"jr:{match_id}:-1"}]]}


def done_keyboard(value: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Подходит" if value > 0 else "❌ Мимо", "callback_data": "jr:done"}]]}


async def send(chat_id: str, text: str, markup: dict | None = None) -> int:
    """Через общий telegram_api портала: та же очередь, пауза между отправками и повтор при 429."""
    from app.services.notifications import telegram_api
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    result = await telegram_api("sendMessage", payload, timeout=20)
    return int((result or {}).get("message_id") or 0)
