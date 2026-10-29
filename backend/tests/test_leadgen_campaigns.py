"""Кампании как в Instantly: загрузка CSV со своими переменными, проверка адресов, ящики кампании, деление лимита,
пауза, остановка по компании при ответе, шаг «позвонить» без сделки не ломает цепочку."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.router import api_router
from app.core.access import COOKIE
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db import get_db
from app.domains.leadgen import campaigns, routes, sequences, service
from app.domains.leadgen.models import LgCampaign, LgDnc, LgEnrollment, LgMailbox, LgSequence, LgTouch
from test_leadgen_sequences import NOW, ORIGIN, FakeTransport, FlowBase

CSV = (
    "Email;Компания;Сайт;Имя;Боль клиента\n"
    "info@kuhni-a.ru;Кухни А;kuhni-a.ru;Анна;дорогие заявки\n"
    "sales@kuhni-b.ru;Кухни Б;https://kuhni-b.ru/;;\n"
    "не-адрес;Кухни В;kuhni-v.ru;;\n"
    "info@kuhni-a.ru;Кухни А дубль;kuhni-a.ru;;\n"
    "zakaz@nomail.ru;Пустой домен;nomail.ru;;\n"
    "info@blocked.ru;Блок;blocked.ru;;\n"
)
STEPS = [
    {"channel": "email", "delay_days": 0, "subject": "Вопрос для {{company}}",
     "body": "Здравствуйте, {{director_first_name|коллеги}}! Про {{боль_клиента|заявки}}."},
    {"channel": "call", "delay_days": 1, "body": "Позвонить"},
    {"channel": "email", "delay_days": 1, "body": "Напомню о себе."},
]


def box(email, limit=45):
    return LgMailbox(workspace_id=7, email=email, sender_name="Егор", smtp_host="smtp.x", imap_host="imap.x",
                     login=email, password_encrypted=encrypt_secret("secret"), daily_limit=limit, warmup_started_on=None,
                     warmup_enabled=True, warmup_target=70, role="outreach", is_active=True)


async def no_mx_for_nomail(domains):
    return {d: d != "nomail.ru" for d in domains}


class CampaignApiTests(FlowBase):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = (settings.agency_workspace_id, routes.mail_transport_factory)
        settings.agency_workspace_id = 7
        routes.mail_transport_factory = FakeTransport
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)
        self.base = "/api/admin/leadgen"

    def tearDown(self):
        settings.agency_workspace_id, routes.mail_transport_factory = self._old
        super().tearDown()

    def test_full_flow_upload_vars_and_launch(self):
        b = self.base
        mb_id = self.run_db(lambda db: _add(db, box("egor@team-a.ru"))).id
        self.run_db(lambda db: _add(db, LgDnc(workspace_id=7, kind="domain", value_norm="blocked.ru", reason="refused")))
        tpl = self.client.post(f"{b}/sequences", headers=ORIGIN, json={"name": "Шаблон", "steps": [{"channel": "email", "subject": "Тема", "body": "Привет, {{company}}"}]}).json()
        c = self.client.post(f"{b}/campaigns", headers=ORIGIN,
                             json={"name": "Кухни Самара", "template_sequence_id": tpl["id"], "mailbox_ids": []}).json()
        self.assertEqual(c["status"], "draft")
        self.assertNotEqual(c["sequence_id"], tpl["id"])   # своя копия цепочки
        # цепочки кампаний не показываются среди шаблонов
        self.assertEqual([s["id"] for s in self.client.get(f"{b}/sequences").json()["items"]], [tpl["id"]])

        files = {"file": ("leads.csv", CSV.encode("utf-8"), "text/csv")}
        pv = self.client.post(f"{b}/campaigns/{c['id']}/leads/preview", headers=ORIGIN, files=files).json()
        fields = {col["header"]: (col["field"], col.get("var")) for col in pv["columns"]}
        self.assertEqual(fields["Email"][0], "email")
        self.assertEqual(fields["Компания"][0], "company")
        self.assertEqual(fields["Сайт"][0], "website")
        self.assertEqual(fields["Имя"][0], "first_name")
        self.assertEqual(fields["Боль клиента"], ("var", "боль_клиента"))
        mapping = [{"index": col["index"], "field": col["field"], "var": col.get("var")} for col in pv["columns"]]
        with patch.object(campaigns, "mail_domains", new=no_mx_for_nomail):
            rep = self.client.post(f"{b}/campaigns/{c['id']}/leads/import", headers=ORIGIN, files=files,
                                   data={"mapping": json.dumps(mapping)}).json()
        self.assertEqual(rep["added"], 2, rep)
        self.assertEqual(rep["reasons"], {"некорректный адрес": 1, "повтор в файле": 1, "у домена нет почты": 1,
                                          "домен в стоп-листе": 1})
        self.assertIn("боль_клиента", rep["campaign"]["variables"])

        # нельзя запустить без ящика; с ящиком — можно
        self.assertEqual(self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"status": "active"}).status_code, 409)
        self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"mailbox_ids": [mb_id]})
        # свою переменную можно сохранить в цепочке кампании (в шаблоне — нельзя)
        seq_payload = {"name": "x", "steps": STEPS, "window": {"tz": "Europe/Moscow", "gap_minutes": 0}}
        self.assertEqual(self.client.put(f"{b}/sequences/{c['sequence_id']}", headers=ORIGIN, json=seq_payload).status_code, 200)
        self.assertEqual(self.client.put(f"{b}/sequences/{tpl['id']}", headers=ORIGIN, json=seq_payload).status_code, 422)
        seq_payload["steps"][0]["body"] += "\n{{sender_name}}"
        self.client.put(f"{b}/sequences/{c['sequence_id']}", headers=ORIGIN, json=seq_payload)
        check = self.client.get(f"{b}/campaigns/{c['id']}/check").json()
        self.assertEqual(check["leads"], 2)
        self.assertEqual(check["missing"], [])   # sender_name берётся из ящика — не ложная тревога
        self.assertEqual(check["problems"], [])
        self.assertEqual(self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"status": "active"}).json()["status"], "active")

        leads = self.client.get(f"{b}/campaigns/{c['id']}/leads").json()
        anna = next(x for x in leads["items"] if x["email"] == "info@kuhni-a.ru")
        prev = self.client.post(f"{b}/sequences/preview", headers=ORIGIN,
                                json={"enrollment_id": anna["id"], "steps": STEPS}).json()
        self.assertIn("Здравствуйте, Анна! Про дорогие заявки.", prev["steps"][0]["body"])
        # сделок в CRM при загрузке не создаём
        self.assertTrue(all(x["crm_deal_id"] is None for x in leads["items"]))
        # удалить кампанию с письмами нельзя, без писем — можно
        self.assertEqual(self.client.delete(f"{b}/campaigns/{c['id']}", headers=ORIGIN).status_code, 204)


async def _add(db, obj):
    db.add(obj)
    await db.flush()
    return obj


class CampaignSendingTests(FlowBase):
    async def campaign(self, db, name, mailbox_ids, n, steps=STEPS[:1], prefix="c"):
        seq = LgSequence(workspace_id=7, name=name, steps=steps, campaign_owned=True,
                         window={"tz": "Europe/Moscow", "gap_minutes": 0})
        db.add(seq)
        await db.flush()
        camp = LgCampaign(workspace_id=7, name=name, status="active", sequence_id=seq.id, mailbox_ids=mailbox_ids,
                          variables=["боль_клиента"])
        db.add(camp)
        await db.flush()
        for i in range(n):
            c = (await service.upsert_company(db, 7, service.FindingIn(
                source="import", name=f"{prefix}{i}", domain=f"{prefix}{i}.ru",
                contacts=[{"kind": "email", "value": f"info@{prefix}{i}.ru"}]), now=NOW)).company
            r = await sequences.enroll(db, seq, c, now=NOW, campaign=camp, vars={"боль_клиента": f"боль {i}"})
            assert r.ok, r
        return camp

    def test_campaign_mailboxes_vars_and_pause(self):
        t = FakeTransport()

        async def fn(db):
            a, other = box("a@team-a.ru"), box("other@team-b.ru")
            db.add_all([a, other])
            await db.flush()
            camp = await self.campaign(db, "К1", [a.id], 2)
            await sequences.process_due(db, t, now=NOW)
            camp.status = "paused"
            c3 = (await service.upsert_company(db, 7, service.FindingIn(
                source="import", name="z", domain="z.ru", contacts=[{"kind": "email", "value": "info@z.ru"}]), now=NOW)).company
            seq = await db.get(LgSequence, camp.sequence_id)
            await sequences.enroll(db, seq, c3, now=NOW, campaign=camp)
            paused = await sequences.process_due(db, t, now=NOW)
            return paused
        paused = self.run_db(fn)
        self.assertEqual({m.sender for m in t.sent}, {"a@team-a.ru"})   # только ящики кампании
        self.assertIn("Про боль 0.", next(m.body for m in t.sent if m.to == "info@c0.ru"))
        self.assertEqual(paused["postponed"], 1)
        self.assertEqual(len(t.sent), 2)

    def test_mailbox_limit_split_equally(self):
        t = FakeTransport()

        async def fn(db):
            shared = box("shared@team-a.ru", limit=4)   # 4 холодных в день на двоих
            db.add(shared)
            await db.flush()
            k1 = await self.campaign(db, "К1", [shared.id], 5, prefix="a")
            k2 = await self.campaign(db, "К2", [shared.id], 5, prefix="b")
            await sequences.process_due(db, t, now=NOW)
            by = dict((await db.execute(select(LgTouch.campaign_id, func.count()).where(
                LgTouch.direction == "out", LgTouch.status == "sent").group_by(LgTouch.campaign_id))).all())
            return by, k1.sequence_id, k2.sequence_id
        by, s1, s2 = self.run_db(fn)
        self.assertEqual((by.get(s1), by.get(s2)), (2, 2))

    def test_reply_stops_company_and_call_without_deal_is_skipped(self):
        t = FakeTransport()

        async def fn(db):
            a = box("a@team-a.ru")
            db.add(a)
            await db.flush()
            camp = await self.campaign(db, "К1", [a.id], 1, steps=STEPS)
            await sequences.process_due(db, t, now=NOW)                         # письмо 1
            from datetime import timedelta
            await sequences.process_due(db, t, now=NOW + timedelta(days=1, hours=1))   # «позвонить» без сделки — пропуск
            enr = await db.scalar(select(LgEnrollment).where(LgEnrollment.campaign_id == camp.id))
            step_after_call, status_after_call = enr.current_step, enr.status
            # второй лид той же компании в другой цепочке — должен остановиться при ответе
            seq2 = LgSequence(workspace_id=7, name="другая", steps=STEPS[:1], window={"gap_minutes": 0})
            db.add(seq2)
            await db.flush()
            other = LgEnrollment(workspace_id=7, sequence_id=seq2.id, company_id=enr.company_id, status="paused",
                                 current_step=0)
            db.add(other)
            await db.flush()
            mail = sequences.IncomingMail(uid=1, sender="info@c0.ru", subject="Re: Вопрос", body="Интересно, расскажите",
                                          in_reply_to=t.sent[0].message_id)
            await sequences.handle_inbound(db, a, mail, now=NOW + timedelta(days=1, hours=2))
            return step_after_call, status_after_call, enr.status, other.status, other.stop_reason
        step, st, replied, other, reason = self.run_db(fn)
        self.assertEqual((step, st), (2, "active"))
        self.assertEqual(replied, "replied")
        self.assertEqual((other, reason), ("stopped", "company_replied"))


class InboxCampaignTests(CampaignApiTests):
    def test_inbox_filter_cc_handoff_and_manual_crm(self):
        b = self.base
        t = FakeTransport()

        async def seed(db):
            a = box("egor@team-a.ru")
            db.add(a)
            await db.flush()
            k = await CampaignSendingTests.campaign(self, db, "Кухни", [a.id], 1)
            await sequences.process_due(db, t, now=NOW)
            await sequences.handle_inbound(db, a, sequences.IncomingMail(
                uid=1, sender="info@c0.ru", subject="Re: Вопрос", body="Интересно, пришлите условия",
                message_id="<r1@c0.ru>", in_reply_to=t.sent[0].message_id), now=NOW)
            return k.id
        camp_id = self.run_db(seed)
        routes.mail_transport_factory = lambda: t
        data = self.client.get(f"{b}/inbox?status=all&campaign_id={camp_id}").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["campaigns"][0]["unhandled"], 1)
        item = data["items"][0]
        self.assertEqual(item["campaign"]["name"], "Кухни")
        self.assertIsNone(item["crm_deal_id"])                       # сделки нет, пока не завели вручную
        self.assertEqual(self.client.get(f"{b}/inbox?status=all&campaign_id=999").json()["total"], 0)
        r = self.client.post(f"{b}/inbox/{item['id']}/reply", headers=ORIGIN,
                             json={"body": "Спасибо! Подключаю рабочую почту.", "cc": "Egor@SteptoLead.ru"}).json()
        self.assertEqual(t.sent[-1].cc, "egor@steptolead.ru")
        handed = t.sent[-1].body
        self.assertTrue(handed.startswith("Спасибо! Подключаю рабочую почту."))
        self.assertIn("Предыдущая переписка", handed)
        self.assertIn("> Интересно, пришлите условия", handed)              # ответ лида
        self.assertIn("> Здравствуйте, коллеги! Про боль 0.", handed)        # наше первое письмо
        self.assertLess(handed.index("пришлите условия"), handed.index("Про боль 0"))   # новые сверху
        self.assertIn("info@c0.ru", handed)
        self.assertIn("Cc", str(sequences.build_message(t.sent[-1])))
        self.assertEqual(r["item"]["thread"][-1]["cc"], "egor@steptolead.ru")
        self.assertEqual(r["item"]["thread"][-1]["body"], "Спасибо! Подключаю рабочую почту.")   # в портале — без цитаты
        crm = self.client.post(f"{b}/inbox/{item['id']}/to-crm", headers=ORIGIN).json()
        self.assertIsNotNone(crm["crm_deal_id"])
        from app.models.crm import CrmDeal, CrmStage

        async def stage(db):
            deal = await db.get(CrmDeal, crm["crm_deal_id"])
            return (await db.get(CrmStage, deal.stage_id)).name
        self.assertIn(self.run_db(stage), ("Ответил", "Интерес"))


class CampaignWindowTests(CampaignApiTests):
    def test_schedule_in_recipients_timezone(self):
        b = self.base
        mb = self.run_db(lambda db: _add(db, box("egor@team-a.ru")))
        c = self.client.post(f"{b}/campaigns", headers=ORIGIN, json={"name": "США", "mailbox_ids": [mb.id]}).json()
        self.assertEqual(c["window"]["tz"], "Europe/Moscow")
        r = self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"window": {
            "tz": "America/New_York", "start_hour": 9, "end_hour": 17, "weekdays": [0, 1, 2, 3, 4], "gap_minutes": 5}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["window"]["tz"], r.json()["window"]["start_hour"]), ("America/New_York", 9))
        bad = self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"window": {"tz": "Mars/Olympus"}})
        self.assertEqual(bad.status_code, 422)

        async def enroll(db):
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            camp = await db.get(LgCampaign, c["id"])
            seq = await db.get(LgSequence, camp.sequence_id)
            comp = (await service.upsert_company(db, 7, service.FindingIn(
                source="import", name="NY", domain="ny-test.com", contacts=[{"kind": "email", "value": "info@ny-test.com"}]),
                now=NOW)).company
            await sequences.enroll(db, seq, comp, now=NOW, campaign=camp)
            enr = await db.scalar(select(LgEnrollment).where(LgEnrollment.campaign_id == camp.id))
            return service._aware(enr.next_at).astimezone(ZoneInfo("America/New_York"))
        local = self.run_db(enroll)
        # NOW = среда 15:00 МСК = 08:00 в Нью-Йорке -> первое письмо в 09:00 по Нью-Йорку, не ночью
        self.assertEqual((local.hour, local.minute), (9, 0))


class InboxConversationTests(CampaignApiTests):
    YANDEX_HTML = ('Интересуюсь <br /><br />--<br />Отправлено из мобильной Яндекс Почты<br /><br /><b>Кому</b> x &lt;x@y.ru&gt;;'
                   '<br />28.10.2026, 23:10, "Егор StepToLead" &lt;e_dementev@steptolead.site&gt;:<br /><blockquote><p>Здравствуйте!'
                   ' Если неактуально — ответьте «нет», больше не напишу.</p></blockquote>')

    def test_two_replies_are_one_conversation_and_html_is_readable(self):
        b = self.base
        t = FakeTransport()

        async def seed(db):
            from datetime import timedelta
            a = box("egor@team-a.ru")
            db.add(a)
            await db.flush()
            await CampaignSendingTests.campaign(self, db, "Тест", [a.id], 1)
            await sequences.process_due(db, t, now=NOW)
            for uid, text in ((1, self.YANDEX_HTML), (2, "Позвоните мне завтра в 11, +7 900 123-45-67")):
                await sequences.handle_inbound(db, a, sequences.IncomingMail(
                    uid=uid, sender="info@c0.ru", subject="Re: Вопрос", body=text, message_id=f"<r{uid}@c0.ru>",
                    in_reply_to=t.sent[0].message_id), now=NOW + timedelta(minutes=uid))
        self.run_db(seed)
        data = self.client.get(f"{b}/inbox?status=unhandled").json()
        self.assertEqual(data["total"], 1)                     # один лид — одна строка
        self.assertEqual(data["unhandled"], 1)
        item = data["items"][0]
        self.assertEqual(item["messages"], 2)
        self.assertTrue(item["hot"])
        full = self.client.get(f"{b}/inbox/{item['id']}").json()
        bodies = [m["body"] for m in full["thread"] if m["direction"] == "in"]
        self.assertEqual(bodies[0], "Интересуюсь")             # без HTML, подписи и цитаты
        self.assertNotIn("<br", " ".join(bodies))
        # разобрали переписку — разобраны оба письма
        self.client.patch(f"{b}/inbox/{item['id']}", headers=ORIGIN, json={"handled": True})
        self.assertEqual(self.client.get(f"{b}/inbox?status=unhandled").json()["total"], 0)
        self.assertEqual(self.client.get(f"{b}/inbox-counter").json()["unhandled"], 0)


class FormattingTests(unittest.TestCase):
    def test_markup_goes_as_html_with_plain_fallback(self):
        from app.domains.leadgen.core import sequence as core
        body = "Здравствуйте, **Иван**!\n\n- цена заявки\n- потери\n\n[Сайт](https://steptolead.ru) и *курсив*, 5*3"
        msg = sequences.build_message(sequences.OutgoingMail("a@x.ru", "Егор", "b@y.ru", "Тема", body, "<1@x>"))
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        plain, html = [p.get_content() for p in msg.iter_parts()]
        self.assertIn("Здравствуйте, Иван!", plain)
        self.assertIn("Сайт (https://steptolead.ru)", plain)
        self.assertIn("<b>Иван</b>", html)
        self.assertIn('<a href="https://steptolead.ru">Сайт</a>', html)
        self.assertIn("<ul", html)
        self.assertIn("5*3", html)                       # звёздочка в числах — не курсив
        self.assertIn("<i>курсив</i>", html)
        # без разметки — как раньше, только простой текст
        simple = sequences.build_message(sequences.OutgoingMail("a@x.ru", "Егор", "b@y.ru", "Т", "Просто текст", "<2@x>"))
        self.assertEqual(simple.get_content_type(), "text/plain")
        # опасная ссылка не превращается в <a>, HTML экранируется
        self.assertNotIn("<script", core.to_html("<script>x</script> [клик](javascript:alert(1))"))
        self.assertNotIn("href=\"javascript", core.to_html("[клик](javascript:alert(1))"))
