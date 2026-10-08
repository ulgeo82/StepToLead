"""Цепочки касаний: шаги, шаблоны, рабочие часы, лимиты ящиков с прогревом, разбор входящих. Без сети и БД."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CHANNELS = ("email", "call", "whatsapp", "telegram")
TASK_CHANNELS = {"call": ("CALL", "Позвонить"), "whatsapp": ("MESSAGE", "Написать в WhatsApp"),
                 "telegram": ("MESSAGE", "Написать в Telegram")}
VARIABLES = ("company", "domain", "city", "niche", "ad_title", "ad_text", "director_name", "director_first_name",
             "sender_name")
MAX_STEPS = 10
_VAR = re.compile(r"\{\{\s*([a-z_]+)\s*(?:\|([^}]*))?\}\}")


class SequenceError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    channel: str
    delay_days: int = 0          # пауза после предыдущего шага (у первого — от старта)
    subject: str | None = None   # только email
    body: str = ""
    new_thread: bool = False     # email: новое письмо, а не ответ в ту же ветку


@dataclass(frozen=True)
class Window:
    tz: str = "Europe/Moscow"
    start_hour: int = 10
    end_hour: int = 18
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)   # пн–пт


def parse_steps(raw: list[dict]) -> list[Step]:
    if not raw or len(raw) > MAX_STEPS:
        raise SequenceError(f"В цепочке должно быть от 1 до {MAX_STEPS} шагов")
    steps = []
    for i, item in enumerate(raw, 1):
        channel = item.get("channel")
        if channel not in CHANNELS:
            raise SequenceError(f"Шаг {i}: неизвестный канал {channel!r}")
        delay = int(item.get("delay_days", 0))
        if not 0 <= delay <= 60:
            raise SequenceError(f"Шаг {i}: пауза от 0 до 60 дней")
        subject = (item.get("subject") or "").strip() or None
        body = (item.get("body") or "").strip()
        new_thread = bool(item.get("new_thread"))
        if channel == "email":
            if not body:
                raise SequenceError(f"Шаг {i}: у письма нужен текст")
            if not subject and (not steps or new_thread or not any(s.channel == "email" for s in steps)):
                raise SequenceError(f"Шаг {i}: у первого письма в ветке нужна тема")
        for text in (subject or "", body):
            for name, _ in _VAR.findall(text):
                if name not in VARIABLES:
                    raise SequenceError(f"Шаг {i}: неизвестная переменная {{{{{name}}}}}")
        steps.append(Step(channel, delay, subject, body, new_thread))
    return steps


def parse_window(raw: dict | None) -> Window:
    raw = raw or {}
    w = Window(tz=raw.get("tz") or "Europe/Moscow", start_hour=int(raw.get("start_hour", 10)),
               end_hour=int(raw.get("end_hour", 18)), weekdays=tuple(sorted(set(raw.get("weekdays", (0, 1, 2, 3, 4))))))
    try:
        ZoneInfo(w.tz)
    except Exception as exc:  # noqa: BLE001
        raise SequenceError(f"Неизвестный часовой пояс {w.tz!r}") from exc
    if not (0 <= w.start_hour < w.end_hour <= 24) or not w.weekdays or not set(w.weekdays) <= set(range(7)):
        raise SequenceError("Неверное окно отправки")
    return w


def render(template: str, ctx: dict) -> str:
    """{{company}} или {{director_first_name|коллеги}} — значение по умолчанию после |."""
    def sub(m: re.Match) -> str:
        value = ctx.get(m.group(1))
        return str(value).strip() if value not in (None, "") else (m.group(2) or "").strip()
    return re.sub(r"[ \t]{2,}", " ", _VAR.sub(sub, template)).strip()


def first_name(full: str | None) -> str | None:
    """«Иванов Иван Иванович» -> «Иван»; «Иван Петров» -> «Иван». Без уверенности — None."""
    parts = (full or "").split()
    if len(parts) == 3:
        return parts[1]
    if len(parts) == 2:
        return parts[0]
    return None


def in_window(moment: datetime, w: Window) -> datetime:
    """Ближайший момент не раньше moment, попадающий в рабочее окно. Возвращается в UTC (так и хранится в БД)."""
    tz = ZoneInfo(w.tz)
    local = moment.astimezone(tz)
    for _ in range(15):
        start = datetime.combine(local.date(), time(w.start_hour), tz)
        end = datetime.combine(local.date(), time(0), tz) + timedelta(hours=w.end_hour)
        if local.weekday() in w.weekdays and local < end:
            return max(local, start).astimezone(timezone.utc)
        local = datetime.combine(local.date() + timedelta(days=1), time(w.start_hour), tz)
    raise SequenceError("Окно отправки пустое")


def step_due(previous_at: datetime, step: Step, w: Window) -> datetime:
    return in_window(previous_at + timedelta(days=step.delay_days), w)


WARMUP = ((7, 5), (14, 10), (21, 20))   # (до дня, писем в день) — дальше дневной лимит ящика


def mailbox_capacity(daily_limit: int, warmup_started: date | None, today: date, sent_today: int) -> int:
    limit = daily_limit
    if warmup_started is not None:
        age = (today - warmup_started).days
        for until, cap in WARMUP:
            if age < until:
                limit = min(limit, cap)
                break
    return max(0, limit - sent_today)


def pick_mailbox(candidates: list[tuple[int, int]]) -> int | None:
    """candidates: [(mailbox_id, capacity_left)] -> ящик с наибольшим запасом (равномерная ротация)."""
    usable = [c for c in candidates if c[1] > 0]
    return max(usable, key=lambda c: (c[1], -c[0]))[0] if usable else None


# ---------------------------------------------------------------- входящие

_BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail delivery", "maildelivery")
_BOUNCE_SUBJ = ("undeliver", "delivery status notification", "returned mail", "недоставлен", "не доставлен",
                "failure notice", "delivery failure", "mail delivery failed")
_AUTO_SUBJ = ("автоответ", "auto-reply", "autoreply", "automatic reply", "out of office", "нет на месте",
              "в отпуске", "автоматический ответ")
# Только явная просьба больше не писать. Вежливый отказ («не интересно») — обычный ответ:
# цепочка по нему тоже останавливается, а закрыть сделку решает человек во «Входящих».
_UNSUB = ("отпиш", "не пишите", "больше не пиш", "удалите", "unsubscribe", "не присылайте", "прекратите")
_FINAL_RCPT = re.compile(r"(?:Final|Original)-Recipient:\s*rfc822;\s*<?([^\s>;]+@[^\s>;]+)", re.I)
_QUOTE_START = re.compile(r"^(>|-{2,}|_{3,}|On .+ wrote:|.*пишет:$|.*написал\(а\):$|От:|From:)", re.I | re.M)


def reply_text(body: str) -> str:
    """Свежая часть ответа без цитаты исходного письма."""
    m = _QUOTE_START.search(body or "")
    return (body[: m.start()] if m else body or "").strip()


def classify_inbound(sender: str, subject: str, body: str, headers: dict | None = None) -> str:
    """bounce | auto_reply | unsubscribe | reply"""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    s, subj = (sender or "").lower(), (subject or "").lower()
    if any(b in s for b in _BOUNCE_FROM) or any(b in subj for b in _BOUNCE_SUBJ) \
            or "delivery-status" in (headers.get("content-type") or "").lower():
        return "bounce"
    auto = (headers.get("auto-submitted") or "").lower()
    if (auto and auto != "no") or headers.get("x-autoreply") or any(a in subj for a in _AUTO_SUBJ):
        return "auto_reply"
    fresh = reply_text(body).lower()
    if any(u in fresh for u in _UNSUB):
        return "unsubscribe"
    return "reply"


def bounced_address(body: str) -> str | None:
    m = _FINAL_RCPT.search(body or "")
    return m.group(1).lower() if m else None
