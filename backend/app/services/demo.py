"""Demo portal for sales meetings: a fictional kitchen manufacturer with 4 months of believable data.

Ad spend and clicks (Direct, VK, Avito, Yandex Maps), ~3–5 leads a day with attribution, a pipeline with won and
lost deals, sales, tasks, calls with AI analysis and a few chats. Everything is invented (names, phones, brand).
The project has status "demo": background jobs that talk to real APIs (ad sync, Avito, chats, SLA, monitor)
skip it, so the fake cabinets never raise alerts. Delete it like any company when it is no longer needed.
"""
import random
import secrets
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import hash_password
from app.core.crypto import encrypt_secret
from app.models.access import GrowthCalculation
from app.models.crm import CrmActivity, CrmContact, CrmStage, CrmStageHistory, CrmTask
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdMetricDaily, ClientLead, ClientLeadAttribution, ClientSale,
                                  ClientWorkspace, LeadInboundSource, PortalProjectAccess, PortalUser, ProjectEconomics)
from app.models.messaging import Conversation, Message, MessagingChannel
from app.models.system import AppSetting
from app.models.telephony import Call, TelephonyConnection
from app.services.project_scope import default_project

DAYS = 120
FIRST = ["Анна", "Ольга", "Ирина", "Марина", "Екатерина", "Наталья", "Светлана", "Татьяна", "Юлия", "Елена", "Дмитрий", "Алексей",
         "Сергей", "Андрей", "Максим", "Игорь", "Павел", "Роман", "Виктория", "Ксения", "Денис", "Артём", "Галина", "Людмила"]
LAST = ["Смирнова", "Кузнецова", "Попова", "Васильева", "Петрова", "Соколова", "Михайлова", "Новикова", "Фёдорова", "Морозова",
        "Волкова", "Алексеева", "Лебедева", "Семёнова", "Егорова", "Павлова", "Козлова", "Степанова", "Николаева", "Орлова"]
PRODUCTS = [("Кухня угловая", 180, 320), ("Кухня прямая", 120, 230), ("Кухня с островом", 320, 560), ("Кухня П-образная", 260, 420),
            ("Шкаф-купе", 70, 140), ("Гардеробная", 110, 240)]
LOST = ["Дорого", "Выбрал конкурента", "Не выходит на связь", "Долгие сроки", "Отложил покупку"]
CALL_SUMMARIES = [
    ("Клиент выбирает угловую кухню в новостройку, ключи через месяц. Записан на бесплатный замер.", "Замер в субботу в 11:00", 85),
    ("Обсудили фасады МДФ и рассрочку. Клиент сравнивает с двумя салонами, просил КП до пятницы.", "Отправить КП до пятницы", 70),
    ("Уточнили размеры, бюджет до 250 тыс. Менеджер не отработал возражение по срокам.", "Перезвонить после обсуждения с семьёй", 55),
    ("Клиент подтвердил договор, обсудили предоплату 50% и дату монтажа.", "Подписать договор во вторник", 95),
]


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


async def create_demo(db: AsyncSession) -> dict:
    """Builds the demo company. Commits. Returns the owner's login and a one-time password."""
    now = datetime.now(timezone.utc)
    n = int(await db.scalar(select(func.count(ClientWorkspace.id)).where(ClientWorkspace.name.like("Демо · %"))) or 0) + 1
    ws = ClientWorkspace(name=f"Демо · Кухни «Северная»{'' if n == 1 else f' {n}'}", plan="system", website="https://example.ru",
                         contact_phone="+7 900 000-00-00")
    db.add(ws); await db.flush()
    rnd = _rng(ws.id * 7919)
    project = await default_project(db, ws.id)
    project.name = "Кухни на заказ"
    project.status = "demo"
    db.add(AppSetting(key=f"demo:{ws.id}", value={"created_at": now.isoformat()}))

    password = secrets.token_urlsafe(10)
    owner = PortalUser(workspace_id=ws.id, username=f"demo{ws.id}", display_name="Демо-собственник", role="client_owner",
                       password_hash=hash_password(password), active=True, must_change_password=False)
    managers = [PortalUser(workspace_id=ws.id, username=f"demo{ws.id}-{slug}", display_name=name, role="sales_manager",
                           password_hash=hash_password(secrets.token_urlsafe(16)), active=True, must_change_password=False)
                for slug, name in (("anna", "Анна Ковалёва"), ("igor", "Игорь Петров"), ("maria", "Мария Белова"))]
    db.add_all([owner, *managers]); await db.flush()
    for m in managers:
        db.add(PortalProjectAccess(user_id=m.id, project_id=project.id))

    from app.api.routes.crm import activity, create_deal_fact, pipeline_for
    pipeline = await pipeline_for(db, project.id, commit=False)
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id, CrmStage.archived_at.is_(None))
                               .order_by(CrmStage.position))).all()
    by_type = {t: [s for s in stages if s.analytics_type == t] for t in ("LEAD", "QUALIFIED", "WON", "LOST")}

    def conn(platform, name, ext):
        row = AdConnection(workspace_id=ws.id, project_id=project.id, platform=platform, name=name, external_account_id=ext,
                           access_token_encrypted=encrypt_secret("demo"), status="connected", currency="RUB",
                           last_synced_at=now - timedelta(minutes=rnd.randint(3, 40)), config={"demo": True})
        db.add(row)
        return row
    direct = conn("yandex", "Яндекс Директ · Кухни", "severnaya-direct")
    vk = conn("vk_ads", "VK Реклама · Кухни", "9100200")
    avito = conn("avito_items", "Авито · Кухни на заказ", "48210993")
    ymaps = conn("yandex_maps", "Яндекс Карты · Северная", "card")
    ymaps.config = {"demo": True, "utm_source": "yandex_maps", "card_url": "https://yandex.ru/maps/"}
    site = LeadInboundSource(workspace_id=ws.id, project_id=project.id, name="Сайт · квиз", token_hash=secrets.token_hex(32),
                             token_prefix="demo", active=False, auto_assign=True, auto_accept=True)
    db.add(site); await db.flush()

    # ---- ad statistics: slow growth over the period, weekends a bit lower
    campaigns = {direct.id: [("1101", "Поиск · Кухни на заказ", .55), ("1102", "РСЯ · Ретаргетинг", .2), ("1103", "Мастер кампаний", .25)],
                 vk.id: [("vk1", "Лид-форма · Кухни", 1.0)], avito.id: [("av1", "Продвижение объявлений", 1.0)]}
    plan = {direct.id: (7500, 23), vk.id: (3000, 15), avito.id: (2200, 9)}  # spend per day, cost per click
    start = (now - timedelta(days=DAYS)).date()
    for i in range(DAYS + 1):
        day = start + timedelta(days=i)
        trend = 0.8 + 0.4 * i / DAYS
        weekend = 0.8 if day.weekday() >= 5 else 1.0
        for cid, (base, cpc) in plan.items():
            spend = round(base * trend * weekend * rnd.uniform(.85, 1.15), 2)
            clicks = max(1, int(spend / (cpc * rnd.uniform(.85, 1.2))))
            db.add(AdMetricDaily(connection_id=cid, date=day, spend=spend, impressions=clicks * rnd.randint(25, 45), clicks=clicks, leads=0, raw={}))
            for ext, cname, share in campaigns[cid]:
                cs = round(spend * share, 2)
                db.add(AdCampaignMetricDaily(connection_id=cid, date=day, external_campaign_id=ext, campaign_name=cname, spend=cs,
                                             impressions=int(clicks * share * rnd.randint(25, 45)), clicks=int(clicks * share), raw={}))
    from app.services import maps
    month = date(start.year, start.month, 1)
    while month <= now.date():
        await maps.set_month(db, ymaps, month.strftime("%Y-%m"), budget=15000,
                             totals={"impressions": rnd.randint(6000, 9000), "clicks": rnd.randint(500, 800), "calls": rnd.randint(30, 55),
                                     "routes": rnd.randint(60, 110), "site": rnd.randint(40, 70)})
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)

    # ---- economics for ROMI
    calc = GrowthCalculation(name="Демо", phone=None, telegram=None, email=None, status="converted",
                             inputs={"gross_margin": 35, "average_order_value": 260000}, results={"metrics": {"breakEvenCustomersRounded": 3}})
    db.add(calc); await db.flush()
    db.add(ProjectEconomics(project_id=project.id, growth_calculation_id=calc.id, allowable_cac=9000))

    # ---- telephony and chats (inactive towards real services: project status demo, no recordings to download)
    tel = TelephonyConnection(workspace_id=ws.id, project_id=project.id, provider="mango", name="Mango Office · демо",
                              public_id=secrets.token_urlsafe(24), api_key_encrypted=encrypt_secret("demo"), api_salt_encrypted=encrypt_secret("demo"),
                              active=True, status="connected", last_event_at=now, config={"demo": True})
    wa = MessagingChannel(workspace_id=ws.id, project_id=project.id, kind="whatsapp", name="WhatsApp продаж", active=True,
                          status="connected", config={"instance_id": "0", "demo": True}, secret_encrypted=encrypt_secret("demo"), last_polled_at=now)
    db.add_all([tel, wa]); await db.flush()

    # ---- leads → deals
    channels = [(direct, .45, "yandex"), (avito, .22, "avito"), (vk, .15, "vk"), (ymaps, .09, "yandex_maps"), (None, .09, "site")]
    deals = []
    for i in range(DAYS + 1):
        day = start + timedelta(days=i)
        for _ in range(max(0, int(rnd.gauss(2.6 + 1.6 * i / DAYS, 1.1)))):
            created = datetime.combine(day, time(rnd.randint(8, 21), rnd.randint(0, 59)), tzinfo=timezone.utc)
            if created > now:
                continue
            pick = rnd.random(); acc = 0
            for row, share, utm in channels:
                acc += share
                if pick <= acc:
                    break
            name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)[0]}."
            phone = f"79{rnd.randint(10, 99)}{rnd.randint(1000000, 9999999)}"
            product, lo, hi = rnd.choice(PRODUCTS)
            amount = rnd.randint(lo, hi) * 1000
            contact = CrmContact(workspace_id=ws.id, project_id=project.id, name=name, phones=[f"+{phone}"], emails=[], phone_normalized=phone)
            db.add(contact); await db.flush()
            owner_id = rnd.choice(managers).id
            deal = await create_deal_fact(db, project, contact, pipeline, by_type["LEAD"][0], f"{product} · {name}", owner_id, amount,
                                          None, "INTEGRATION", created_at=created)
            lead = await db.get(ClientLead, deal.lead_id)
            lead.source = row.name if row else "Сайт · квиз"
            snapshot = {"utm_source": utm, "utm_medium": "cpc" if row and row.platform != "yandex_maps" else "organic"}
            if row:
                snapshot.update({"connection_id": row.id, "platform": row.platform, "ad_account": row.name})
                ext = rnd.choice(campaigns.get(row.id, [(None, None, 0)]))[0]
                if ext:
                    snapshot["external_campaign_id"] = ext
            deal.attribution_snapshot = snapshot
            db.add(ClientLeadAttribution(lead_id=lead.id, connection_id=row.id if row else None, source_id=None if row else site.id,
                                         external_campaign_id=snapshot.get("external_campaign_id"), utm_source=utm,
                                         utm_medium=snapshot["utm_medium"]))
            deal.first_response_at = created + timedelta(minutes=max(2, int(rnd.gauss(11, 6))))
            age = (now - created).days
            roll = rnd.random()
            won_p, lost_p, qual_p = (0.14, 0.42, 0.3) if age > 30 else (0.06, 0.22, 0.45) if age > 7 else (0.0, 0.08, 0.3)
            if roll < won_p:
                outcome = "WON"
            elif roll < won_p + lost_p:
                outcome = "LOST"
            elif roll < won_p + lost_p + qual_p:
                outcome = "QUALIFIED"
            else:
                outcome = "LEAD"
            moved = min(now, created + timedelta(days=rnd.randint(1, max(1, min(age, 25)) if age else 1), hours=rnd.randint(0, 6)))
            stage = rnd.choice(by_type[outcome]) if outcome != "LEAD" else rnd.choice(by_type["LEAD"])
            if stage.id != deal.stage_id:
                db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id, to_stage_id=stage.id, actor_id=owner_id))
                activity(db, deal, None, "STAGE_CHANGED", {"from": by_type["LEAD"][0].name, "to": stage.name, "analytics_type": stage.analytics_type}, touch=False)
                deal.stage_id = stage.id
            deal.stage_entered_at = deal.last_activity_at = moved
            if outcome in {"QUALIFIED", "WON"}:
                lead.status, lead.quality, lead.qualified_at = "qualified", "target", created + timedelta(hours=rnd.randint(2, 48))
            if outcome == "WON":
                db.add(ClientSale(lead_id=lead.id, deal_id=deal.id, project_id=project.id, amount=amount, occurred_at=moved,
                                  comment="Договор подписан", confirmed_by_id=owner_id))
                lead.status = "won"; deal.closed_at = moved
                activity(db, deal, None, "SALE_CREATED", {"amount": amount}, touch=False)
            if outcome == "LOST":
                reason = rnd.choice(LOST)
                lead.status, lead.lost_reason, deal.closed_at = "lost", reason, moved
                deal.lost_comment = reason
                if rnd.random() < .25:
                    lead.quality, lead.quality_reason = "non_target", rnd.choice(["Не та услуга", "Не наш регион", "Спам или ошибка"])
            if outcome in {"LEAD", "QUALIFIED"}:
                due = now + timedelta(hours=rnd.randint(-30, 72))
                db.add(CrmTask(workspace_id=ws.id, project_id=project.id, deal_id=deal.id, contact_id=contact.id,
                               type_code=rnd.choice(["CALL", "CALL", "MEETING", "MESSAGE"]),
                               title=rnd.choice(["Перезвонить по проекту", "Замер кухни", "Отправить КП", "Уточнить размеры"]),
                               responsible_user_id=owner_id, due_at=due, status="OPEN"))
            deals.append((deal, contact, created, outcome))

    # ---- calls for the last two weeks, some analysed by AI
    for deal, contact, created, outcome in [d for d in deals if (now - d[2]).days <= 14]:
        for k in range(rnd.randint(1, 2)):
            started = min(now - timedelta(minutes=5), created + timedelta(minutes=rnd.randint(3, 60 * 24 * 3)))
            answered = rnd.random() > .15
            summary, step, score = rnd.choice(CALL_SUMMARIES)
            call = Call(workspace_id=ws.id, project_id=project.id, connection_id=tel.id, entry_id=secrets.token_hex(8),
                        direction=rnd.choice(["in", "out"]), status="answered" if answered else "missed", client_phone=contact.phones[0].lstrip("+"),
                        line_number="78460000000", user_id=deal.responsible_user_id, started_at=started,
                        answered_at=started + timedelta(seconds=8) if answered else None, ended_at=started + timedelta(minutes=4),
                        duration_sec=rnd.randint(60, 420) if answered else 0, wait_sec=rnd.randint(4, 30),
                        contact_id=contact.id, deal_id=deal.id, processed=True,
                        ai_status="done" if answered and rnd.random() < .6 else None,
                        meta={"ai": {"summary": summary, "next_step": step, "score": score, "client_mood": "позитив", "need": deal.name.split(" · ")[0],
                                     "checklist": [], "advice": "Назовите конкретную дату следующего шага и подтвердите её сообщением.",
                                     "transcript": [], "provider": "демо"}} if answered else {})
            db.add(call)

    # ---- a few chats on the freshest deals
    for deal, contact, created, outcome in sorted(deals, key=lambda d: d[2])[-6:]:
        conv = Conversation(workspace_id=ws.id, project_id=project.id, channel_id=wa.id, external_chat_id=f"{contact.phone_normalized}@c.us",
                            title=contact.name, phone=contact.phones[0], contact_id=contact.id, deal_id=deal.id,
                            assigned_user_id=deal.responsible_user_id, status="open", unread_count=rnd.randint(0, 2), meta={})
        db.add(conv); await db.flush()
        lines = [("in", "Здравствуйте! Увидела вашу рекламу, интересует кухня. Сколько будет стоить?"),
                 ("out", "Добрый день! Подскажите размеры помещения и есть ли проект? Замер и дизайн бесплатно."),
                 ("in", "Примерно 3,2 на 1,8, новостройка. Можно замер на выходных?")]
        t = created
        for direction, text in lines:
            t += timedelta(minutes=rnd.randint(3, 40))
            db.add(Message(conversation_id=conv.id, direction=direction, text=text, sent_at=min(t, now), attachments=[],
                           status="received" if direction == "in" else "sent",
                           author_name=None if direction == "in" else "Анна Ковалёва"))
        conv.last_message_at, conv.last_message_preview, conv.last_direction = min(t, now), lines[-1][1], "in"
        conv.waiting_since = min(t, now)
    await db.commit()
    return {"workspace_id": ws.id, "name": ws.name, "username": owner.username, "password": password, "deals": len(deals)}
