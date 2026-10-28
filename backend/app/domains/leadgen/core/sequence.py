"""Цепочки касаний: шаги, шаблоны, рабочие часы, лимиты ящиков с прогревом, разбор входящих. Без сети и БД."""
from __future__ import annotations

import random
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
# Переменные: стандартные (латиница) и свои из столбцов файла кампании — можно по-русски: {{боль}}, {{имя}}.
_VAR = re.compile(r"\{\{\s*([0-9a-zа-яё_]+)\s*(?:\|([^}]*))?\}\}", re.I)


def var_name(header: str) -> str:
    """Заголовок столбца -> имя переменной: «Боль клиента» -> боль_клиента."""
    return re.sub(r"[^0-9a-zа-яё]+", "_", (header or "").strip().lower().replace("ё", "е")).strip("_")[:40]
# Спинтакс как в Instantly: {Добрый день|Здравствуйте} — у каждого получателя свой вариант (стабильно).
_SPIN = re.compile(r"(?<!\{)\{([^{}]*\|[^{}]*)\}(?!\})")


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
    gap_minutes: int = 10        # пауза между письмами с одного ящика (±30% случайно), как «delay between emails»
    daily_new: int = 0           # сколько новых компаний в день получают первое письмо; 0 — без ограничения


def parse_steps(raw: list[dict], extra: tuple | list | set = (), strict: bool = True) -> list[Step]:
    """strict=False — при отправке (переменные уже проверены при сохранении, у кампании есть свои)."""
    allowed = set(VARIABLES) | {v.lower() for v in extra}
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
            for name, _ in _VAR.findall(text) if strict else ():
                if name.lower() not in allowed:
                    raise SequenceError(f"Шаг {i}: неизвестная переменная {{{{{name}}}}}")
        steps.append(Step(channel, delay, subject, body, new_thread))
    return steps


def parse_window(raw: dict | None) -> Window:
    raw = raw or {}
    w = Window(tz=raw.get("tz") or "Europe/Moscow", start_hour=int(raw.get("start_hour", 10)),
               end_hour=int(raw.get("end_hour", 18)), weekdays=tuple(sorted(set(raw.get("weekdays", (0, 1, 2, 3, 4))))),
               gap_minutes=int(raw["gap_minutes"]) if raw.get("gap_minutes") is not None else 10,
               daily_new=int(raw.get("daily_new", 0) or 0))
    if not 0 <= w.gap_minutes <= 240:
        raise SequenceError("Пауза между письмами — от 0 до 240 минут")
    if not 0 <= w.daily_new <= 2000:
        raise SequenceError("Новых компаний в день — от 0 до 2000")
    try:
        ZoneInfo(w.tz)
    except Exception as exc:  # noqa: BLE001
        raise SequenceError(f"Неизвестный часовой пояс {w.tz!r}") from exc
    if not (0 <= w.start_hour < w.end_hour <= 24) or not w.weekdays or not set(w.weekdays) <= set(range(7)):
        raise SequenceError("Неверное окно отправки")
    return w


def spin(template: str, seed: str | int | None = None) -> str:
    """{a|b|c} -> один вариант. seed — чтобы у одного получателя текст не менялся между предпросмотром и отправкой."""
    rng = random.Random(seed)
    for _ in range(20):  # вложенные варианты раскрываем изнутри наружу
        new = _SPIN.sub(lambda m: rng.choice(m.group(1).split("|")), template)
        if new == template:
            break
        template = new
    return template


def render(template: str, ctx: dict, seed: str | int | None = None) -> str:
    """{{company}} или {{director_first_name|коллеги}} — значение по умолчанию после |. {a|b} — спинтакс."""
    template = spin(template, seed)

    def sub(m: re.Match) -> str:
        value = ctx.get(m.group(1).lower())
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


def gap_for(base_minutes: int, key: int) -> timedelta:
    """Пауза между письмами с разбросом 0.7–1.3: письма уходят не по таймеру, как у живого человека."""
    return timedelta(minutes=base_minutes * (0.7 + (key * 7919 % 61) / 100))


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
_QUOTE_START = re.compile(
    r"^(>|-{2,}|_{3,}|On .+ wrote:|.*пишет:$|.*написал\(а\):$|От:|From:"
    r"|Отправлено из|Sent from my"                               # подписи мобильных почт
    r"|\d{1,2}\.\d{1,2}\.\d{2,4},? \d{1,2}:\d{2}.*:\s*$"           # Яндекс: «28.10.2026, 23:10, "Егор" <e@x.ru>:»
    r"|.*<[^<>\s]+@[^<>\s]+>\s*:\s*$)",                           # Gmail: «… Егор <e@x.ru>:»
    re.I | re.M)
_HTML_HINT = re.compile(r"<(br|p|div|blockquote|html|body|span|b|i)\b[^>]*>", re.I)


def html_to_text(html: str) -> str:
    """HTML письма -> читаемый текст. Цитату (<blockquote>) отрезаем: это наше же письмо."""
    import html as _html
    text = html or ""
    cut = re.search(r"<blockquote", text, re.I)
    if cut:
        text = text[: cut.start()]
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h\d)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def looks_html(text: str) -> bool:
    return bool(_HTML_HINT.search(text or ""))


def reply_text(body: str) -> str:
    """Свежая часть ответа без цитаты исходного письма (и без HTML, если письмо пришло в HTML)."""
    body = html_to_text(body) if looks_html(body) else (body or "")
    m = _QUOTE_START.search(body)
    return (body[: m.start()] if m else body).strip()


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


# ---------------------------------------------------------------- форматирование писем
# Простая разметка в редакторе (кнопки Ж / К / Ч / ссылка / список): **жирный**, *курсив*, __подчёркнутый__,
# [текст](https://…), строки «- …» / «1. …» — списки. Есть разметка — письмо уходит HTML + простой текст;
# нет — только простой текст (так лучше доставляемость холодных писем).
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:|tel:)[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_UNDER = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_BULLET = re.compile(r"^\s*[-•]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d{1,2}[.)]\s+(.*)$")


def has_markup(text: str) -> bool:
    t = text or ""
    return bool(_LINK.search(t) or _BOLD.search(t) or _UNDER.search(t) or _ITALIC.search(t))


def _inline_html(line: str) -> str:
    import html as _html
    out, last = [], 0
    for m in _LINK.finditer(line):   # ссылки — до экранирования, чтобы не испортить адрес
        out.append(_html.escape(line[last:m.start()]))
        out.append(f'<a href="{_html.escape(m.group(2), quote=True)}">{_html.escape(m.group(1))}</a>')
        last = m.end()
    out.append(_html.escape(line[last:]))
    text = "".join(out)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _UNDER.sub(r"<u>\1</u>", text)
    return _ITALIC.sub(r"<i>\1</i>", text)


def to_html(text: str) -> str:
    blocks: list[str] = []
    para: list[str] = []
    lst: tuple[str, list[str]] | None = None

    def flush_para():
        if para:
            blocks.append("<p style=\"margin:0 0 12px\">" + "<br>".join(para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal lst
        if lst:
            tag, items = lst
            blocks.append(f"<{tag} style=\"margin:0 0 12px;padding-left:22px\">" + "".join(f"<li>{i}</li>" for i in items) + f"</{tag}>")
            lst = None

    for raw in (text or "").splitlines():
        bullet, number = _BULLET.match(raw), _NUMBER.match(raw)
        if bullet or number:
            flush_para()
            tag = "ul" if bullet else "ol"
            if lst is None or lst[0] != tag:
                flush_list()
                lst = (tag, [])
            lst[1].append(_inline_html((bullet or number).group(1)))
        elif not raw.strip():
            flush_para(); flush_list()
        else:
            flush_list()
            para.append(_inline_html(raw))
    flush_para(); flush_list()
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#1f2937">'
            + "".join(blocks) + "</div>")


def to_plain(text: str) -> str:
    t = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) != m.group(2) else m.group(2), text or "")
    t = _BOLD.sub(r"\1", t)
    t = _UNDER.sub(r"\1", t)
    t = _ITALIC.sub(r"\1", t)
    return re.sub(r"(?m)^[ \t]*[-•][ \t]+", "— ", t)
