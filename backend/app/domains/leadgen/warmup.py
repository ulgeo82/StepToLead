"""Прогрев почты: ящики пула переписываются между собой, как живые люди.

Пул — все активные ящики агентства с включённым прогревом: рабочие (role="outreach") и помощники (role="helper",
дешёвые ящики на Яндексе / Mail.ru / Gmail, которые холодных писем не шлют). Каждую минуту воркер:
  • отправляет прогревочные письма по плану дня (с 2–3 в первый день до 25 к 21-му, потом ~10 в день постоянно);
  • раз в 5 минут заходит в ящик получателя по IMAP, находит письмо по Message-ID — во «Входящих» или в «Спаме»;
    из «Спама» переносит во «Входящие», отмечает прочитанным, иногда — важным; на ~35% отвечает (ветки до 4 писем).
Из того, где оказались письма, считается «здоровье» ящика 0–100. Плюс проверки домена (SPF, DMARC, MX, DKIM по
заголовкам получателя, чёрный список Spamhaus DBL) и недоставленные холодные письма. Плохие цифры → холодная отправка
с ящика на паузе и сообщение в Telegram. Прогревочные письма не попадают во «Входящие» портала и в аналитику.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.domains.leadgen import service
from app.domains.leadgen.core import sequence as core
from app.domains.leadgen.models import LgMailbox, LgTouch, LgWarmupMail

logger = logging.getLogger("uvicorn.error.leadgen.warmup")

TZ = "Europe/Moscow"
WARM_DAYS = 28           # разгон до пика — 4 недели, с 29-го дня «гибридный режим»
QUIET_DAYS = 7           # первая неделя — только прогрев, холодных писем нет
START = 2                # писем в первый день
# Всего писем в день (прогрев + холодные) по дням разгона, для пика 70; для другого пика масштабируется.
ANCHORS = ((1, 2), (7, 10), (14, 25), (21, 45), (28, 65), (29, 70))
COLD_SHARE_START = 0.3   # 8-й день: ~30% от объёма — холодные, остальное прогрев
WARM_FLOOR = 20          # на пике прогрев не опускается ниже — он перекрывает жалобы холодных получателей
HELPER_DAILY = 10        # помощники создают входящий поток новым ящикам
MAX_TARGET = 120
MAX_DEPTH = 3            # письмо + до 3 ответов в ветке
FIND_AFTER = timedelta(minutes=3)
MISSING_AFTER = timedelta(hours=6)
RECHECK_MISSING = timedelta(days=7)   # столько же, сколько окно здоровья
CHECK_EVERY_TICKS = 15   # заходить в ящики по IMAP за прогревом раз в 15 минут — чаще почтовики рвут соединения
WINDOW = core.Window(tz=TZ, start_hour=9, end_hour=20, weekdays=(0, 1, 2, 3, 4, 5, 6))
HEALTH_EVERY = timedelta(hours=6)
MIN_MEASURED = 5
PAUSE_BOUNCE, PAUSE_SPAM = 0.03, 0.20


def _stable(*parts) -> float:
    """Детерминированное «случайное» 0..1 — одинаково между тиками, разное у разных писем."""
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


# ---------------------------------------------------------------- план дня

def age_days(mb: LgMailbox, today: date) -> int | None:
    return (today - mb.warmup_started_on).days if mb.warmup_started_on else None


def _total(day: int, target: int) -> int:
    """Всего писем в этот день разгона: 2 → 10 → 25 → 45 → 65 → пик (масштаб от пика 70)."""
    scale = target / 70
    points = [(d, START if d == 1 else round(v * scale)) for d, v in ANCHORS]
    if day >= points[-1][0]:
        return target
    for (d1, v1), (d2, v2) in zip(points, points[1:]):
        if d1 <= day <= d2:
            return round(v1 + (v2 - v1) * (day - d1) / (d2 - d1))
    return START


def schedule(age: int | None, target: int, cold_peak: int, quiet: int = QUIET_DAYS) -> tuple[int, int]:
    """(прогревочных, холодных) в день. age None — ящик уже живой (старый), сразу гибридный режим."""
    target = max(START, min(MAX_TARGET, target))
    cold_peak = max(0, min(cold_peak, target - WARM_FLOOR))
    if age is None or age + 1 > WARM_DAYS:
        return target - cold_peak, cold_peak
    day = age + 1
    total = _total(day, target)
    if day <= quiet:
        return total, 0
    span = max(1, WARM_DAYS + 1 - (quiet + 1))
    share = COLD_SHARE_START + (cold_peak / target - COLD_SHARE_START) * (day - quiet - 1) / span
    cold = min(cold_peak, round(total * share))
    return total - cold, cold


def _plan(mb: LgMailbox, today: date) -> tuple[int, int]:
    return schedule(age_days(mb, today), mb.warmup_target or 70, mb.daily_limit or 45)


def daily_plan(mb: LgMailbox, today: date) -> int:
    """Прогревочных писем с ящика сегодня."""
    if not mb.is_active or not mb.warmup_enabled:
        return 0
    plan = HELPER_DAILY if mb.role == "helper" else _plan(mb, today)[0]
    if today.weekday() >= 5:  # в выходные живые люди пишут меньше
        plan = max(1, (plan + 1) // 2)
    return plan


def cold_limit(mb: LgMailbox, today: date) -> int:
    """Холодных писем с ящика в день по графику прогрева. Первые 7 дней — ноль."""
    if mb.role == "helper":
        return 0
    if not mb.warmup_enabled:
        return core.mailbox_capacity(mb.daily_limit, mb.warmup_started_on, today, 0)
    return _plan(mb, today)[1]


PHASES = ((7, "Тихий старт · только прогрев"), (14, "Эмуляция человека"), (21, "Входящий поток"), (28, "Выход на пик"))


def stage(mb: LgMailbox, today: date) -> dict:
    if not mb.is_active:
        return {"phase": "off", "title": "Ящик выключен"}
    if not mb.warmup_enabled:
        return {"phase": "off", "title": "Прогрев выключен"}
    if mb.role == "helper":
        return {"phase": "helper", "title": "Помощник прогрева"}
    age = age_days(mb, today)
    if age is not None and age < WARM_DAYS:
        day = age + 1
        name = next(title for until, title in PHASES if day <= until)
        out = {"phase": "warming", "title": f"{name} · день {day} из {WARM_DAYS}", "day": day, "of": WARM_DAYS}
        if day <= QUIET_DAYS:
            out["note"] = f"Холодные письма начнутся с {QUIET_DAYS + 1}-го дня"
        elif day >= 27:
            out["note"] = ("Время контрольного теста: письмо на mail-tester.com (нужно 9.5/10 и выше) и проверка домена "
                           "в MXToolbox. Ниже 9.5 или чёрный список — не выходите на пик")
        return out
    return {"phase": "ready", "title": "Гибридный режим · холодные + прогрев"}


# ---------------------------------------------------------------- тексты

_SUBJECTS = [
    "{Вопрос|Уточнение} по {встрече|созвону} {в четверг|на следующей неделе|завтра}",
    "{Документы|Счёт|Акт} за {сентябрь|октябрь|прошлый месяц}",
    "{План|График} работ на {неделю|месяц}",
    "{Про|По поводу} {поставки|доставки} {материалов|образцов}",
    "{Обновил|Поправил} {презентацию|таблицу|расчёт}",
    "{Как|Когда} {удобнее|лучше} {созвониться|встретиться}?",
    "{Список|Перечень} {задач|вопросов} к {обсуждению|встрече}",
    "{Отпуск|Выходные} — {кто на связи|подменим друг друга}",
    "{Новый|Свежий} {прайс|каталог} от {поставщика|партнёра}",
    "{Отзыв|Комментарий} по {макету|черновику|тексту}",
]
_OPEN = ["{Добрый день|Здравствуйте|Привет}{!|,}", "{Доброе утро|Добрый день}{!|.}", "{Коллеги|Привет}, {добрый день|здравствуйте}{!|.}"]
_BODY = [
    "{Посмотрел|Глянул} {ваш|присланный} {расчёт|вариант} — в целом всё {понятно|логично}. {Есть пара вопросов|Остался один вопрос} по {срокам|бюджету|составу работ}, {обсудим|давайте обсудим} {на созвоне|при встрече|в переписке}?",
    "{Напоминаю|Хотел напомнить} про {документы|счёт|акт сверки}. {Если удобно|Когда будет минутка}, {пришлите|отправьте} {сканы|копии} {до пятницы|на этой неделе}.",
    "{Обновил|Поправил} {таблицу|файл} с {задачами|планом}: {добавил|внёс} {новые пункты|сроки} и {убрал|отметил} {выполненное|сделанное}. {Проверьте, пожалуйста|Гляньте, когда будет время}.",
    "{Подскажите|Уточните}, {вам удобно|получится} {созвониться|встретиться} {во вторник|в среду|в четверг} {после обеда|в первой половине дня}? {Хочу|Нужно} {согласовать|обсудить} {следующие шаги|план на месяц}.",
    "{Поставщик|Партнёр} {прислал|обновил} {прайс|условия}. {Цены|Сроки} {немного выросли|почти не изменились}, {но|а} {доставка|минимальная партия} теперь {быстрее|удобнее}. {Что думаете|Как вам}?",
    "{Прочитал|Посмотрел} {черновик|макет}. {Мне нравится|Хорошо получилось}, {только|но} {я бы|предлагаю} {сократил вступление|поменял заголовок|добавил пример}. {Остальное|В остальном} — {отлично|ок}.",
    "{На следующей неделе|С понедельника} {меня не будет|я в отъезде} пару дней. {Если что-то срочное|По срочным вопросам} — {пишите сюда|звоните}, {постараюсь ответить|отвечу} {вечером|в течение дня}.",
]
_CLOSE = ["{Спасибо|Заранее спасибо}!", "{Хорошего дня|Отличного дня}!", "{На связи|Буду на связи}.", "{С уважением|Спасибо},"]
_REPLY = [
    "{Спасибо|Благодарю}, {получил|увидел}. {Посмотрю|Изучу} {сегодня|до вечера|завтра утром} и {отпишусь|вернусь с ответом}.",
    "{Да|Ок}, {давайте|удобно}. {Предлагаю|Могу} {в 11|в 15|после 16}, {если вам подходит|если не против}.",
    "{Отлично|Супер}, {спасибо|благодарю}! {Всё понятно|Вопросов нет}, {двигаемся дальше|работаем}.",
    "{Хороший вопрос|Да, согласен}. {Думаю|Мне кажется}, {лучше обсудить голосом|проще созвониться} — {так быстрее|займёт 10 минут}.",
    "{Принял|Понял}, {учту|поправлю}. {Пришлю обновлённый вариант|Скину правки} {сегодня|до конца недели}.",
]


def _first_name(mb: LgMailbox) -> str:
    return (mb.sender_name or mb.email.split("@")[0]).split()[0][:40]


def compose(sender: LgMailbox, seed: str) -> tuple[str, str]:
    rng = random.Random(seed)
    subject = core.spin(rng.choice(_SUBJECTS), seed + ":s")
    body = "\n\n".join([core.spin(rng.choice(_OPEN), seed + ":o"), core.spin(rng.choice(_BODY), seed + ":b"),
                        core.spin(rng.choice(_CLOSE), seed + ":c") + "\n" + _first_name(sender)])
    return subject, body


def compose_reply(sender: LgMailbox, seed: str, subject: str) -> tuple[str, str]:
    rng = random.Random(seed)
    body = core.spin(rng.choice(_REPLY), seed + ":r") + "\n\n" + _first_name(sender)
    return (subject if subject.lower().startswith("re:") else f"Re: {subject}")[:300], body


# ---------------------------------------------------------------- пул и отправка

def local_midnight(now: datetime) -> datetime:
    tz = ZoneInfo(TZ)
    return datetime.combine(now.astimezone(tz).date(), time(0), tz).astimezone(timezone.utc)


def in_window(now: datetime) -> bool:
    return core.in_window(now, WINDOW) <= now


async def pool(db: AsyncSession, workspace_id: int) -> list[LgMailbox]:
    return list((await db.scalars(select(LgMailbox).where(
        LgMailbox.workspace_id == workspace_id, LgMailbox.is_active.is_(True), LgMailbox.warmup_enabled.is_(True))
        .order_by(LgMailbox.id))).all())


async def pool_addresses(db: AsyncSession, workspace_id: int) -> set[str]:
    """Все ящики агентства (и выключенные): их письма друг другу — прогрев, не ответы лидов."""
    return {e.lower() for e in (await db.scalars(select(LgMailbox.email).where(LgMailbox.workspace_id == workspace_id))).all()}


async def _sent_today(db: AsyncSession, mailbox_id: int, now: datetime) -> tuple[int, datetime | None]:
    row = (await db.execute(select(func.count(LgWarmupMail.id), func.max(LgWarmupMail.sent_at)).where(
        LgWarmupMail.from_mailbox_id == mailbox_id, LgWarmupMail.sent_at >= local_midnight(now),
        LgWarmupMail.status != "failed"))).one()
    return int(row[0] or 0), row[1]


def _domain(mb: LgMailbox) -> str:
    return mb.email.split("@")[-1].lower()


async def _pick_recipient(db: AsyncSession, sender: LgMailbox, boxes: list[LgMailbox], now: datetime) -> LgMailbox | None:
    options = [b for b in boxes if b.id != sender.id and (sender.role != "helper" or b.role != "helper")]
    age = age_days(sender, now.astimezone(ZoneInfo(TZ)).date())
    if sender.role != "helper" and age is not None and age < QUIET_DAYS:
        # «Тихий старт»: только на авторитетные почтовики (Gmail, Яндекс, Mail.ru), если такие есть в пуле
        big = [b for b in options if _provider(b) in BIG_PROVIDERS]
        options = big or options
    if not options:
        return None
    since = local_midnight(now)
    received = dict((await db.execute(select(LgWarmupMail.to_mailbox_id, func.count()).where(
        LgWarmupMail.sent_at >= since, LgWarmupMail.to_mailbox_id.in_([b.id for b in options]))
        .group_by(LgWarmupMail.to_mailbox_id))).all())
    # Чаще — на другой домен и тем, кому сегодня писали меньше.
    return min(options, key=lambda b: (_domain(b) == _domain(sender), received.get(b.id, 0),
                                       _stable(sender.id, b.id, now.date())))


def _due(sent: int, plan: int, last: datetime | None, now: datetime, key) -> bool:
    if sent >= plan:
        return False
    if last is None:
        return True
    end = datetime.combine(now.astimezone(ZoneInfo(TZ)).date(), time(WINDOW.end_hour), ZoneInfo(TZ))
    left_minutes = max(1.0, (end - now).total_seconds() / 60)
    interval = left_minutes / (plan - sent + 1)
    gap = max(4.0, interval * (0.6 + 0.8 * _stable(key, sent)))
    return service._aware(last) + timedelta(minutes=gap) <= now


async def _deliver(db: AsyncSession, transport, row: LgWarmupMail, sender: LgMailbox, recipient: LgMailbox,
                   now: datetime) -> bool:
    from app.domains.leadgen.sequences import OutgoingMail
    row.message_id = row.message_id or make_msgid(domain=_domain(sender))
    mail = OutgoingMail(sender.email, sender.sender_name, recipient.email, row.subject, row.body, row.message_id,
                        row.in_reply_to)
    try:
        await transport.send(sender, decrypt_secret(sender.password_encrypted), mail)
    except Exception as exc:  # noqa: BLE001 — ящик недоступен; ошибку видно на экране прогрева
        row.status, row.error, row.sent_at = "failed", f"{type(exc).__name__}: {exc}"[:300], now
        sender.warmup_error = row.error
        return False
    row.status, row.sent_at, row.error = "sent", now, None
    sender.warmup_error = None
    return True


async def send_due(db: AsyncSession, transport, workspace_id: int, *, now: datetime | None = None) -> int:
    """Отправить, что положено к этой минуте: запланированные ответы и новые письма по плану дня."""
    now = now or service.utcnow()
    if not in_window(now):
        return 0
    boxes = await pool(db, workspace_id)
    if len(boxes) < 2:
        return 0
    by_id = {b.id: b for b in boxes}
    sent = 0
    replies = (await db.scalars(select(LgWarmupMail).where(
        LgWarmupMail.workspace_id == workspace_id, LgWarmupMail.status == "scheduled",
        LgWarmupMail.send_after <= now).order_by(LgWarmupMail.send_after).limit(20))).all()
    for row in replies:
        sender, recipient = by_id.get(row.from_mailbox_id), by_id.get(row.to_mailbox_id)
        if not sender or not recipient:
            row.status, row.error = "failed", "Ящик выключен или убран из прогрева"
            continue
        count, _ = await _sent_today(db, sender.id, now)
        if count >= daily_plan(sender, now.astimezone(ZoneInfo(TZ)).date()) + 5:
            continue  # ответ подождёт до завтра
        sent += await _deliver(db, transport, row, sender, recipient, now)
    today = now.astimezone(ZoneInfo(TZ)).date()
    for sender in boxes:
        count, last = await _sent_today(db, sender.id, now)
        if not _due(count, daily_plan(sender, today), last, now, sender.id):
            continue
        recipient = await _pick_recipient(db, sender, boxes, now)
        if recipient is None:
            continue
        seed = f"{sender.id}:{recipient.id}:{now.isoformat()}"
        subject, body = compose(sender, seed)
        row = LgWarmupMail(workspace_id=workspace_id, from_mailbox_id=sender.id, to_mailbox_id=recipient.id,
                           subject=subject, body=body, status="sent", depth=0)
        db.add(row)
        sent += await _deliver(db, transport, row, sender, recipient, now)
    await db.flush()
    return sent


# ---------------------------------------------------------------- получатель: найти, спасти из спама, ответить

def reply_rate(sender: LgMailbox | None, today: date) -> float:
    """Доля писем, на которые отвечает получатель. Молодому ящику (первые 2 недели) нужна живая
    двусторонняя переписка — отвечаем на 55%, дальше на 40%."""
    if sender is None:
        return 0.0
    age = age_days(sender, today)
    return 0.55 if sender.role != "helper" and age is not None and age < 14 else 0.4

def imap_result(mb: LgMailbox, now: datetime, error: Exception | None = None) -> None:
    """Запомнить, удался ли вход в ящик по IMAP: сбой дольше часа — тревога в «Мониторинге»."""
    if error is None:
        mb.imap_failing_since = None
        mb.warmup_error = None
        if (mb.last_error or "").startswith("IMAP:"):
            mb.last_error = None
        return
    text = f"IMAP: {type(error).__name__}: {error}"[:500]
    mb.warmup_error = text
    mb.imap_failing_since = mb.imap_failing_since or now


async def pending_for(db: AsyncSession, box: LgMailbox, now: datetime) -> list[LgWarmupMail]:
    """Прогревочные письма, которые надо поискать в этом ящике. «Не дошли» за 3 дня тоже перепроверяем:
    письмо могло найтись позже (раньше мешал сбой IMAP)."""
    return list((await db.scalars(select(LgWarmupMail).where(
        LgWarmupMail.to_mailbox_id == box.id, LgWarmupMail.sent_at <= now - FIND_AFTER,
        or_(LgWarmupMail.status == "sent",
            (LgWarmupMail.status == "missing") & (LgWarmupMail.sent_at >= now - RECHECK_MISSING)))
        .order_by(LgWarmupMail.id.desc()).limit(200))).all())


def flags_for(rows: list[LgWarmupMail]) -> set[str]:
    return {r.message_id for r in rows if r.message_id and _stable(r.message_id, "flag") < 0.15}


async def apply_found(db: AsyncSession, box: LgMailbox, rows: list[LgWarmupMail], found: dict, now: datetime,
                      stats: dict | None = None) -> dict:
    stats = stats if stats is not None else {"inbox": 0, "spam": 0, "missing": 0, "replies": 0}
    today = now.astimezone(ZoneInfo(TZ)).date()
    for row in rows:
        hit = found.get(row.message_id or "")
        if hit is None:
            if row.status == "sent" and service._aware(row.sent_at) <= now - MISSING_AFTER:
                row.status = "missing"
                stats["missing"] += 1
            continue
        was_missing = row.status == "missing"
        row.status, row.found_at, row.rescued, row.auth = hit["folder"], now, hit["folder"] == "spam", hit.get("auth")
        stats[hit["folder"]] += 1
        sender = await db.get(LgMailbox, row.from_mailbox_id)
        if not was_missing and row.depth < MAX_DEPTH and _stable(row.message_id, "reply") < reply_rate(sender, today):
            subject, body = compose_reply(box, f"{row.id}:reply", row.subject)
            delay = timedelta(minutes=10 + int(110 * _stable(row.message_id, "delay")))
            db.add(LgWarmupMail(workspace_id=box.workspace_id, from_mailbox_id=box.id, to_mailbox_id=sender.id,
                                subject=subject, body=body, in_reply_to=row.message_id, depth=row.depth + 1,
                                status="scheduled", send_after=now + delay))
            stats["replies"] += 1
    await db.flush()
    return stats


async def check_due(db: AsyncSession, transport, workspace_id: int, *, now: datetime | None = None) -> dict:
    """Отдельная проверка прогрева по всем ящикам (в воркере она идёт внутри общего входа — см. poll_mailbox)."""
    now = now or service.utcnow()
    stats = {"inbox": 0, "spam": 0, "missing": 0, "replies": 0}
    for box in await pool(db, workspace_id):
        rows = await pending_for(db, box, now)
        if not rows:
            continue
        try:
            found = await transport.warm_locate(box, decrypt_secret(box.password_encrypted),
                                                [r.message_id for r in rows if r.message_id], flags_for(rows))
        except Exception as exc:  # noqa: BLE001
            imap_result(box, now, exc)
            continue
        imap_result(box, now)
        await apply_found(db, box, rows, found, now, stats)
    await db.flush()
    return stats


# ---------------------------------------------------------------- здоровье

def _dns_checks(domain: str) -> dict:
    """SPF, DMARC, MX и Spamhaus DBL. Без сети — None (не знаем), а не «плохо»."""
    import dns.exception
    import dns.resolver
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6

    def txt(name: str) -> list[str] | None:
        try:
            return [b"".join(r.strings).decode(errors="replace") for r in resolver.resolve(name, "TXT")]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException:
            return None

    out: dict = {}
    spf = txt(domain)
    out["spf"] = None if spf is None else any(t.lower().startswith("v=spf1") for t in spf)
    out["spf_spaces"] = any(t != t.lstrip() and t.lstrip().lower().startswith("v=spf1") for t in spf or [])
    dmarc = txt(f"_dmarc.{domain}")
    # Запись должна начинаться ровно с «v=DMARC1»: пробелы в начале строгие почтовики не принимают.
    out["dmarc"] = None if dmarc is None else any(t.lower().startswith("v=dmarc1") for t in dmarc)
    out["dmarc_spaces"] = any(t != t.lstrip() and t.lstrip().lower().startswith("v=dmarc1") for t in dmarc or [])
    out["dmarc_policy"] = next((m.group(1) for t in dmarc or [] for m in [re.search(r"\bp=(\w+)", t)] if m), None)
    try:
        out["mx"] = bool(resolver.resolve(domain, "MX"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        out["mx"] = False
    except dns.exception.DNSException:
        out["mx"] = None
    try:
        answer = [r.to_text() for r in resolver.resolve(f"{domain}.dbl.spamhaus.org", "A")]
        # 127.0.1.x — в списке; 127.255.255.x — Spamhaus не отвечает публичным DNS (не знаем)
        out["blacklisted"] = None if any(a.startswith("127.255.255.") for a in answer) else any(a.startswith("127.0.1.") for a in answer)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        out["blacklisted"] = False
    except dns.exception.DNSException:
        out["blacklisted"] = None
    return out


async def dns_checks(domain: str) -> dict:
    return await asyncio.to_thread(_dns_checks, domain)


@dataclass
class Health:
    score: int
    measured: int
    inbox_rate: float | None
    spam_rate: float | None
    cold_sent: int
    bounces: int
    bounce_rate: float | None
    checks: list[dict]


FIXES = {
    "spf": ("SPF", "Добавьте TXT-запись домена: v=spf1 include:<ваш почтовый сервис> ~all (точную строку даёт Яндекс 360 / Mail.ru для бизнеса)."),
    "dmarc": ("DMARC", "Добавьте TXT-запись _dmarc: v=DMARC1; p=none; rua=mailto:<ваш адрес>. Через 2–3 недели можно p=quarantine."),
    "mx": ("MX", "У домена нет MX-записи — письма на него не доходят, а почтовики считают такой домен подозрительным."),
    "dkim": ("DKIM", "Включите DKIM-подпись в настройках почты домена и добавьте TXT-запись, которую даст почтовый сервис."),
    "blacklist": ("Чёрный список", "Домен в Spamhaus DBL. Остановите холодные письма и подайте заявку на удаление на spamhaus.org."),
    "bounce": ("Недоставленные", "Больше 3% холодных писем не доставлено — чистите базу (проверка адресов) перед отправкой."),
    "spam": ("Попадание в спам", "Больше 20% прогревочных писем уходят в «Спам». Снизьте объём холодных писем и проверьте тексты."),
}


def _check(key: str, ok: bool | None, detail: str | None = None) -> dict:
    title, fix = FIXES[key]
    return {"key": key, "title": title, "ok": ok, "detail": detail, "fix": None if ok in (True, None) else fix}


async def compute_health(db: AsyncSession, mb: LgMailbox, now: datetime, dns_result: dict | None) -> Health:
    since = now - timedelta(days=7)
    rows = (await db.execute(select(LgWarmupMail.status, LgWarmupMail.auth).where(
        LgWarmupMail.from_mailbox_id == mb.id, LgWarmupMail.sent_at >= since,
        LgWarmupMail.status.in_(("inbox", "spam", "missing"))))).all()
    measured = len(rows)
    inbox = sum(1 for s, _ in rows if s == "inbox")
    spam = sum(1 for s, _ in rows if s == "spam")
    dkim = [a.get("dkim") for _, a in rows if a and a.get("dkim")]
    cold = dict((await db.execute(select(LgTouch.status, func.count()).where(
        LgTouch.mailbox_id == mb.id, LgTouch.direction == "out", LgTouch.channel == "email",
        LgTouch.happened_at >= since, LgTouch.status.in_(("sent", "bounced"))).group_by(LgTouch.status))).all())
    cold_sent = int(cold.get("sent", 0)) + int(cold.get("bounced", 0))
    bounces = int(cold.get("bounced", 0))
    d = dns_result or {}
    checks = [
        _check("spf", d.get("spf"), "в начале значения лишние пробелы — уберите их" if d.get("spf_spaces") else None),
        _check("dmarc", d.get("dmarc"), "запись есть, но в начале значения лишние пробелы — удалите их, "
                                         "значение должно начинаться ровно с v=DMARC1" if d.get("dmarc_spaces")
               else f"политика p={d['dmarc_policy']}" if d.get("dmarc_policy") else None),
        _check("mx", d.get("mx")),
        _check("dkim", None if not dkim else sum(1 for x in dkim if x == "pass") >= len(dkim) * 0.8,
               f"подпись прошла в {sum(1 for x in dkim if x == 'pass')} из {len(dkim)}" if dkim else "нет данных от получателей"),
        _check("blacklist", None if d.get("blacklisted") is None else not d["blacklisted"]),
    ]
    inbox_rate = inbox / measured if measured else None
    spam_rate = spam / measured if measured else None
    bounce_rate = bounces / cold_sent if cold_sent else None
    if cold_sent >= 20:
        checks.append(_check("bounce", bounce_rate <= PAUSE_BOUNCE, f"{bounces} из {cold_sent} за 7 дней"))
    if measured >= MIN_MEASURED:
        checks.append(_check("spam", spam_rate <= PAUSE_SPAM, f"{spam} из {measured} за 7 дней"))
    score = round(inbox_rate * 100) if measured >= MIN_MEASURED else 85
    penalty = {"spf": 15, "dmarc": 10, "mx": 15, "dkim": 20, "blacklist": 40, "bounce": 25, "spam": 0}
    score -= sum(penalty[c["key"]] for c in checks if c["ok"] is False)
    return Health(max(0, min(100, score)), measured, inbox_rate, spam_rate, cold_sent, bounces, bounce_rate, checks)


IMAP_ALERT_AFTER = timedelta(hours=1)
STUCK_AFTER = timedelta(hours=1)


async def unchecked_reason(db: AsyncSession, mb: LgMailbox, now: datetime) -> str | None:
    """Почему здоровье сейчас нельзя посчитать честно: нет входа в сам ящик или письма не проверяются у получателей."""
    if mb.imap_failing_since and service._aware(mb.imap_failing_since) <= now - IMAP_ALERT_AFTER:
        return "Портал не может зайти в ящик по IMAP — проверьте пароль приложения и что IMAP включён"
    stuck = await db.scalar(select(func.count(LgWarmupMail.id)).where(
        LgWarmupMail.from_mailbox_id == mb.id, LgWarmupMail.status == "sent",
        LgWarmupMail.sent_at <= now - STUCK_AFTER, LgWarmupMail.sent_at >= now - timedelta(days=2)))
    if (stuck or 0) >= 3:
        return f"{stuck} прогревочных писем пока не удалось проверить у получателей (сбой IMAP у других ящиков пула)"
    return None


async def refresh_health(db: AsyncSession, mb: LgMailbox, *, now: datetime | None = None, force_dns: bool = False) -> Health:
    now = now or service.utcnow()
    cached = (mb.health_json or {}).get("dns")
    fresh = mb.health_checked_at and service._aware(mb.health_checked_at) > now - timedelta(hours=24)
    dns_result = cached if (cached and fresh and not force_dns) else await dns_checks(_domain(mb))
    health = await compute_health(db, mb, now, dns_result)
    unchecked = await unchecked_reason(db, mb, now)
    # Не смогли проверить — честно «нет данных», а не 0: наш сбой не должен урезать холодную отправку.
    mb.health = None if unchecked else health.score
    mb.health_json = {"dns": dns_result, "checks": health.checks, "measured": health.measured,
                      "inbox_rate": health.inbox_rate, "spam_rate": health.spam_rate, "cold_sent": health.cold_sent,
                      "bounces": health.bounces, "bounce_rate": health.bounce_rate, "unchecked": unchecked}
    if not fresh or force_dns:
        mb.health_checked_at = now
    if not unchecked:
        await guard(db, mb, health, now)
    await db.flush()
    return health


async def guard(db: AsyncSession, mb: LgMailbox, health: Health, now: datetime) -> None:
    """Автопауза холодных писем с ящика: недоставленные > 3%, спам > 20% или чёрный список."""
    if mb.role == "helper" or mb.cold_paused_at:
        return
    reason = None
    if health.cold_sent >= 30 and (health.bounce_rate or 0) > PAUSE_BOUNCE:
        reason = f"Недоставленных {round(health.bounce_rate * 100)}% за неделю"
    elif health.measured >= 10 and (health.spam_rate or 0) > PAUSE_SPAM:
        reason = f"В «Спам» уходит {round(health.spam_rate * 100)}% прогревочных писем"
    elif any(c["key"] == "blacklist" and c["ok"] is False for c in health.checks):
        reason = "Домен в чёрном списке Spamhaus"
    if reason is None:
        return
    mb.cold_paused_at, mb.cold_paused_reason = now, reason
    from app.services import monitor
    try:
        await monitor.send(db, f"⏸ <b>Холодные письма с {mb.email} на паузе</b>\n{reason}. Прогрев продолжается. "
                               f"Возобновить можно на экране «Аутрич → Прогрев».")
    except Exception:  # noqa: BLE001
        logger.exception("warmup pause telegram failed")


def cold_capacity(mb: LgMailbox, left: int) -> int:
    """Холодные письма с учётом прогрева: помощники и ящики на паузе — 0, слабое здоровье — вполовину."""
    if mb.role == "helper" or mb.cold_paused_at:
        return 0
    if mb.health is not None and mb.health < 50:
        return left // 2
    return left


# ---------------------------------------------------------------- экран

async def card(db: AsyncSession, mb: LgMailbox, now: datetime) -> dict:
    today = now.astimezone(ZoneInfo(TZ)).date()
    warm_sent, _ = await _sent_today(db, mb.id, now)
    from app.domains.leadgen.sequences import _sent_today as cold_today
    cold_sent = await cold_today(db, mb.id, now, TZ)
    start = local_midnight(now) - timedelta(days=13)
    rows = (await db.execute(select(LgWarmupMail.sent_at, LgWarmupMail.status).where(
        LgWarmupMail.from_mailbox_id == mb.id, LgWarmupMail.sent_at >= start))).all()
    days = {}
    tz = ZoneInfo(TZ)
    for i in range(14):
        d = (today - timedelta(days=13 - i)).isoformat()
        days[d] = {"day": d, "sent": 0, "inbox": 0, "spam": 0, "missing": 0}
    for sent_at, status in rows:
        d = service._aware(sent_at).astimezone(tz).date().isoformat()
        if d in days and status != "failed":
            days[d]["sent"] += 1
            if status in ("inbox", "spam", "missing"):
                days[d][status] += 1
    h = mb.health_json or {}
    from app.domains.leadgen.routes import unicode_email
    return {"id": mb.id, "email": unicode_email(mb.email), "sender_name": mb.sender_name, "role": mb.role, "is_active": mb.is_active,
            "warmup_enabled": mb.warmup_enabled, "warmup_target": mb.warmup_target, "stage": stage(mb, today),
            "today": {"warm_plan": daily_plan(mb, today), "warm_sent": warm_sent,
                      "cold_plan": cold_limit(mb, today) if mb.is_active else 0, "cold_sent": cold_sent},
            "peak": {"total": mb.warmup_target, "cold": mb.daily_limit},
            "health": {"score": mb.health, "measured": h.get("measured", 0), "inbox_rate": h.get("inbox_rate"),
                       "spam_rate": h.get("spam_rate"), "bounce_rate": h.get("bounce_rate"),
                       "cold_sent": h.get("cold_sent", 0), "checks": h.get("checks", []),
                       "checked_at": mb.health_checked_at.isoformat() if mb.health_checked_at else None,
                       "unchecked": h.get("unchecked")},
            "cold_paused": {"at": mb.cold_paused_at.isoformat(), "reason": mb.cold_paused_reason} if mb.cold_paused_at else None,
            "error": mb.warmup_error or mb.last_error, "days": list(days.values())}


async def overview(db: AsyncSession, workspace_id: int, now: datetime | None = None) -> dict:
    now = now or service.utcnow()
    boxes = (await db.scalars(select(LgMailbox).where(LgMailbox.workspace_id == workspace_id)
                              .order_by(LgMailbox.role, LgMailbox.id))).all()
    active = [b for b in boxes if b.is_active and b.warmup_enabled]
    providers = {_provider(b) for b in active}
    tips = []
    if len(active) < 2:
        tips.append("Для прогрева нужно минимум 2 ящика с включённым прогревом — им не с кем переписываться.")
    elif len(active) < 4:
        tips.append("В пуле меньше 4 ящиков — прогрев работает слабо. Добавьте 2–3 ящика-помощника на Яндексе, Mail.ru или Gmail.")
    if active and len(providers) < 2:
        tips.append("Все ящики пула на одном почтовом сервисе. Помощники на других сервисах (Яндекс, Mail.ru, Gmail) дают более честную картину.")
    return {"pool": {"total": len(active), "outreach": sum(1 for b in active if b.role != "helper"),
                     "helpers": sum(1 for b in active if b.role == "helper"), "providers": sorted(providers)},
            "running": len(active) >= 2, "in_window": in_window(now), "window": f"{WINDOW.start_hour}:00–{WINDOW.end_hour}:00 МСК",
            "tips": tips, "items": [await card(db, b, now) for b in boxes]}


BIG_PROVIDERS = {"Яндекс", "Mail.ru", "Gmail"}


def _provider(mb: LgMailbox) -> str:
    host = (mb.smtp_host or "").lower()
    for key, name in (("yandex", "Яндекс"), ("mail.ru", "Mail.ru"), ("gmail", "Gmail"), ("google", "Gmail"),
                      ("outlook", "Outlook"), ("office365", "Outlook"), ("timeweb", "Timeweb"), ("beget", "Beget")):
        if key in host:
            return name
    return host.split(".", 1)[-1] if "." in host else host or "—"


async def run_tick(db: AsyncSession, transport, *, now: datetime | None = None, check: bool = False) -> None:
    """Из воркера цепочек: отправка каждую минуту, здоровье — после проверки получателей (раз в 15 минут)."""
    now = now or service.utcnow()
    workspaces = (await db.scalars(select(LgMailbox.workspace_id).where(
        LgMailbox.is_active.is_(True), LgMailbox.warmup_enabled.is_(True)).distinct())).all()
    for ws in workspaces:
        await send_due(db, transport, ws, now=now)
        if check:  # поиск прогревочных писем идёт внутри общего входа в ящик (sequences.poll_mailbox)
            for mb in await pool(db, ws):
                if mb.role != "helper" and (not mb.health_checked_at or
                                            service._aware(mb.health_checked_at) <= now - HEALTH_EVERY or mb.health is None):
                    await refresh_health(db, mb, now=now)
