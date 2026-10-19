"""Выгрузка базы компаний в Excel: одна строка = одна компания, все контакты и контекст рекламы."""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen.core.normalize import display_domain
from app.domains.leadgen.models import LgAd, LgCompany, LgContact

MAX_ROWS = 5000
STAGE_NAMES = {"new": "Новая", "enriched": "Обогащена", "ready": "Готова", "in_outreach": "В аутриче",
               "replied": "Ответила", "converted": "Клиент", "rejected": "Отказ", "dnc": "Стоп-лист"}
FIT_NAMES = {"fit": "Подходит", "maybe": "Возможно", "no": "Не подходит"}
LEGAL_NAMES = {"active": "Действует", "liquidated": "Ликвидирована / банкротство"}

COLUMNS = [
    ("Компания", 28), ("Сайт", 26), ("Город", 14), ("Ниша", 18), ("Скоринг", 9), ("Почему такой скоринг", 40),
    ("Оценка ЦА", 12), ("Причина (ИИ)", 34), ("Стадия", 12),
    ("Телефоны", 22), ("WhatsApp", 18), ("Telegram", 18), ("Email", 28), ("Личные email (не писать)", 26),
    ("ИНН", 14), ("Юрлицо", 30), ("Руководитель", 26), ("Статус юрлица", 16), ("Выручка, ₽", 14),
    ("Что рекламирует", 44), ("Текст объявления", 60), ("Ключей с показами", 10), ("Спецразмещение", 10),
    ("Впервые замечена", 12), ("Списки", 30), ("Карточка в портале", 30),
]
# «Для рассылки»: только то, что нужно, чтобы написать или позвонить.
PRESETS = {"full": [name for name, _ in COLUMNS],
           "outreach": ["Компания", "Сайт", "Город", "Ниша", "Телефоны", "WhatsApp", "Telegram", "Email",
                        "Руководитель", "Что рекламирует", "Оценка ЦА", "Карточка в портале"]}
WRAP = {"Почему такой скоринг", "Причина (ИИ)", "Что рекламирует", "Текст объявления", "Списки"}


def _cell(value):
    """Текст с сайтов и из объявлений бывает с управляющими символами — openpyxl на них падает (500).
    Строку, начинающуюся с «=», Excel считает формулой: экранируем, чтобы чужой текст не исполнялся."""
    if not isinstance(value, str):
        return value
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    value = ILLEGAL_CHARACTERS_RE.sub(" ", value)[:32000]
    return " " + value if value[:1] in ("=", "+", "-", "@") and not value[1:2].isdigit() else value


def _reasons(reasons) -> str:
    """Причины скоринга: у части причин вес не задан (None) — показываем без веса, а не падаем."""
    parts = []
    for r in reasons or []:
        if not isinstance(r, dict) or not r.get("label"):
            continue
        weight = r.get("weight")
        parts.append(f"{r['label']} ({'+' if weight > 0 else ''}{weight})" if isinstance(weight, (int, float)) else str(r["label"]))
    return "; ".join(parts)


def _join(values) -> str:
    return ", ".join(dict.fromkeys(v for v in values if v))


async def build_xlsx(db: AsyncSession, companies: list[LgCompany], *, base_url: str = "", preset: str = "full",
                     lists: dict[int, list[dict]] | None = None) -> bytes:
    """Данные — из базы (асинхронно), сам файл — в отдельном потоке: не блокирует ответы портала."""
    ids = [c.id for c in companies]
    contacts: dict[int, list[LgContact]] = defaultdict(list)
    ads: dict[int, list[LgAd]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for row in (await db.execute(select(LgContact).where(LgContact.company_id.in_(chunk))
                                     .order_by(LgContact.is_primary.desc(), LgContact.id))).scalars():
            contacts[row.company_id].append(row)
        for row in (await db.execute(select(LgAd).where(LgAd.company_id.in_(chunk))
                                     .order_by(LgAd.last_seen_at.desc()))).scalars():
            ads[row.company_id].append(row)
    return await asyncio.to_thread(_render, companies, contacts, ads, base_url, preset, lists or {})


def _values(c, contacts, ads, portal, lists) -> dict:
    cs = [x for x in contacts[c.id] if not x.bounced]
    ad_list = ads[c.id]
    top = ad_list[0] if ad_list else None
    keywords = {k for a in ad_list for k in (a.keywords or [])}
    return {
        "Компания": c.display_name or display_domain(c.domain),
        "Сайт": display_domain(c.domain),
        "Город": c.city,
        "Ниша": c.niche,
        "Скоринг": c.score,
        "Почему такой скоринг": _reasons(c.score_reasons),
        "Оценка ЦА": FIT_NAMES.get(c.fit_label or "", ""),
        "Причина (ИИ)": c.fit_reason,
        "Стадия": STAGE_NAMES.get(c.stage, c.stage),
        "Телефоны": _join(x.value_norm for x in cs if x.kind == "phone"),
        "WhatsApp": _join(x.value_norm for x in cs if x.kind == "whatsapp"),
        "Telegram": _join(("@" + x.value_norm.lstrip("@")) for x in cs if x.kind == "telegram"),
        "Email": _join(x.value_norm for x in cs if x.kind == "email" and not x.is_personal),
        "Личные email (не писать)": _join(x.value_norm for x in cs if x.kind == "email" and x.is_personal),
        "ИНН": c.inn,
        "Юрлицо": c.legal_name,
        "Руководитель": " — ".join(v for v in (c.director_name, c.director_post) if v) or None,
        "Статус юрлица": LEGAL_NAMES.get(c.legal_status or "", c.legal_status),
        "Выручка, ₽": c.revenue_rub,
        "Что рекламирует": top.title if top else None,
        "Текст объявления": top.text if top else None,
        "Ключей с показами": len(keywords) or None,
        "Спецразмещение": "да" if any(a.placement == "premium" for a in ad_list) else None,
        "Впервые замечена": c.first_seen_at.date() if c.first_seen_at else None,
        "Списки": _join(item["name"] for item in lists.get(c.id, [])),
        "Карточка в портале": f"{portal}/admin/leadgen/companies?company={c.id}" if portal else None,
    }


def _render(companies, contacts, ads, base_url: str, preset: str = "full", lists: dict | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    widths = dict(COLUMNS)
    names = PRESETS.get(preset) or PRESETS["full"]
    pos = {name: i for i, name in enumerate(names, start=1)}
    wb = Workbook()
    ws = wb.active
    ws.title = "Компании"
    ws.append(names)
    head_fill = PatternFill("solid", fgColor="1F3A68")
    for idx, name in enumerate(names, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(name, 18)
    ws.row_dimensions[1].height = 32

    portal = (base_url or "").rstrip("/")
    for c in companies:
        values = _values(c, contacts, ads, portal, lists or {})
        ws.append([_cell(values[name]) for name in names])
        r = ws.max_row
        if c.domain and "Сайт" in pos:
            site = ws.cell(row=r, column=pos["Сайт"])
            site.hyperlink, site.style = f"https://{c.domain}", "Hyperlink"
        if portal and "Карточка в портале" in pos:
            card = ws.cell(row=r, column=pos["Карточка в портале"])
            card.hyperlink, card.style = card.value, "Hyperlink"
        for name in WRAP & pos.keys():
            ws.cell(row=r, column=pos[name]).alignment = Alignment(wrap_text=True, vertical="top")
        if "Впервые замечена" in pos:
            ws.cell(row=r, column=pos["Впервые замечена"]).number_format = "DD.MM.YYYY"
        if "Выручка, ₽" in pos:
            ws.cell(row=r, column=pos["Выручка, ₽"]).number_format = "# ##0"

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(names))}{max(ws.max_row, 1)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
