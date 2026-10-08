"""Цепочки касаний: ядро (шаблоны, окно, прогрев, разбор входящих) и сценарий целиком на подставной почте."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db import Base, get_db
from app.domains.leadgen import routes, sequences, service
from app.domains.leadgen.core import sequence as core
from app.domains.leadgen.models import LgAd, LgCompany, LgContact, LgDnc, LgEnrollment, LgMailbox, LgSequence, LgTouch
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmActivity, CrmDeal, CrmStage, CrmTask
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)   # среда, 15:00 по Москве
ORIGIN = {"Origin": "http://localhost:3000"}
STEPS = [
    {"channel": "email", "delay_days": 0, "subject": "Заявки {{company}}", "body": "Здравствуйте, {{director_first_name|коллеги}}! Видел «{{ad_title}}»."},
    {"channel": "call", "delay_days": 2, "body": "Позвонить в {{company}}"},
    {"channel": "email", "delay_days": 3, "body": "Напомню о себе."},
]


class FakeTransport:
    def __init__(self, refuse=()):
        self.sent, self.refuse, self.inbox = [], set(refuse), []

    async def send(self, mailbox, password, mail):
        assert password == "secret"
        if mail.to in self.refuse:
            raise sequences.RecipientRefused(mail.to)
        self.sent.append(mail)

    async def fetch(self, mailbox, password, after_uid):
        return [m for m in self.inbox if not after_uid or m.uid > after_uid]

    async def check(self, mailbox, password):
        return None


class CoreTests(unittest.TestCase):
    def test_steps_validation(self):
        self.assertEqual(len(core.parse_steps(STEPS)), 3)
        bad = [
            [], [{"channel": "fax", "body": "x"}], [{"channel": "email", "body": "x"}],
            [{"channel": "email", "subject": "s", "body": "{{price}}"}],
            [{"channel": "call", "body": "x", "delay_days": 90}],
        ]
        for steps in bad:
            with self.assertRaises(core.SequenceError, msg=steps):
                core.parse_steps(steps)

    def test_render_and_names(self):
        ctx = {"company": "Кухни Северная", "director_first_name": None}
        self.assertEqual(core.render("Привет, {{director_first_name|коллеги}} из {{company}}", ctx),
                         "Привет, коллеги из Кухни Северная")
        self.assertEqual(core.first_name("Иванов Иван Иванович"), "Иван")
        self.assertIsNone(core.first_name("ООО"))

    def test_window(self):
        w = core.parse_window({"tz": "Europe/Moscow"})
        friday_evening = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)  # 19:00 МСК пятница
        self.assertEqual(core.in_window(friday_evening, w), datetime(2026, 10, 12, 10, 0, tzinfo=w_tz(w)))
        self.assertEqual(core.in_window(NOW, w), NOW)
        with self.assertRaises(core.SequenceError):
            core.parse_window({"start_hour": 18, "end_hour": 10})

    def test_warmup_and_rotation(self):
        today = date(2026, 10, 7)
        self.assertEqual(core.mailbox_capacity(30, today, today, 0), 5)
        self.assertEqual(core.mailbox_capacity(30, today - timedelta(days=10), today, 3), 7)
        self.assertEqual(core.mailbox_capacity(30, today - timedelta(days=40), today, 10), 20)
        self.assertEqual(core.mailbox_capacity(30, None, today, 31), 0)
        self.assertEqual(core.pick_mailbox([(1, 2), (2, 9), (3, 0)]), 2)
        self.assertIsNone(core.pick_mailbox([(1, 0)]))

    def test_classify_inbound(self):
        c = core.classify_inbound
        self.assertEqual(c("MAILER-DAEMON@mx.ru", "Undelivered Mail", ""), "bounce")
        self.assertEqual(c("a@b.ru", "Автоответ: в отпуске", ""), "auto_reply")
        self.assertEqual(c("a@b.ru", "Re: x", "", {"Auto-Submitted": "auto-replied"}), "auto_reply")
        self.assertEqual(c("a@b.ru", "Re: x", "Нет, спасибо\n\n> старое письмо"), "reply")  # отказ решает человек
        self.assertEqual(c("a@b.ru", "Re: x", "Не пишите нам больше"), "unsubscribe")
        self.assertEqual(c("a@b.ru", "Re: x", "Интересно, давайте созвонимся\n\nСреда, Иван пишет:\n> нет"), "reply")
        self.assertEqual(core.bounced_address("Final-Recipient: rfc822; Info@Dead.ru\n"), "info@dead.ru")


def w_tz(w):
    from zoneinfo import ZoneInfo
    return ZoneInfo(w.tz)


class FlowBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=7, name="StepToLead Agency"))
                db.add(AdminUser(id=1, username="admin", password_hash=hash_password("x" * 16), role="admin"))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.commit()
        asyncio.run(seed())

    def tearDown(self):
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                result = await fn(db)
                await db.commit()
                return result
        return asyncio.run(go())

    async def setup_world(self, db, n=1, warmup=None, steps=STEPS):
        mb = LgMailbox(workspace_id=7, email="egor@steptolead-team.ru", sender_name="Егор", smtp_host="smtp.x",
                       imap_host="imap.x", login="egor@steptolead-team.ru", password_encrypted=encrypt_secret("secret"),
                       daily_limit=30, warmup_started_on=warmup)
        seq = LgSequence(workspace_id=7, name="Кухни", steps=steps, window={"tz": "Europe/Moscow"})
        db.add_all([mb, seq])
        await db.flush()
        companies = []
        for i in range(n):
            c = (await service.upsert_company(db, 7, service.FindingIn(
                source="site", name=f"Кухни {i}", domain=f"kuhni{i}.ru", city="Екатеринбург",
                contacts=[{"kind": "email", "value": f"info@kuhni{i}.ru"}]), now=NOW)).company
            c.director_name = "Иванов Иван Иванович"
            db.add(LgAd(company_id=c.id, content_hash=f"h{i}", title="Кухни за 14 дней", text="Замер"))
            companies.append(c)
        await db.flush()
        return mb, seq, companies


class FlowTests(FlowBase):
    def test_full_sequence_email_task_followup(self):
        t = FakeTransport()

        async def fn(db):
            mb, seq, (c,) = await self.setup_world(db)
            r = await sequences.enroll(db, seq, c, now=NOW)
            again = await sequences.enroll(db, seq, c, now=NOW)
            s1 = await sequences.process_due(db, t, now=NOW)
            enr = await db.get(LgEnrollment, r.enrollment_id)
            deal = await db.get(CrmDeal, c.crm_deal_id)
            stage1 = (await db.get(CrmStage, deal.stage_id)).name
            nothing = await sequences.process_due(db, t, now=NOW + timedelta(days=1))
            s2 = await sequences.process_due(db, t, now=NOW + timedelta(days=2, hours=1))
            task = await db.scalar(select(CrmTask).where(CrmTask.deal_id == deal.id))
            s3 = await sequences.process_due(db, t, now=NOW + timedelta(days=6))
            return r, again, s1, nothing, s2, s3, enr, stage1, task, mb
        r, again, s1, nothing, s2, s3, enr, stage1, task, mb = self.run_db(fn)
        self.assertTrue(r.ok)
        self.assertEqual(again.reason, "already_in_sequence")
        self.assertEqual((s1["emails"], nothing["emails"], s2["tasks"], s3["emails"]), (1, 0, 1, 1))
        first, second = t.sent
        self.assertEqual((first.to, first.subject, first.sender_name), ("info@kuhni0.ru", "Заявки Кухни 0", "Егор"))
        self.assertIn("Здравствуйте, Иван! Видел «Кухни за 14 дней».", first.body)
        self.assertEqual((second.subject, second.in_reply_to), ("Re: Заявки Кухни 0", first.message_id))
        self.assertEqual(stage1, "Написали")
        self.assertEqual((task.type_code, task.title), ("CALL", "Позвонить: Кухни 0"))
        self.assertEqual((enr.status, enr.current_step, enr.mailbox_id), ("finished", 3, mb.id))

    def test_reply_stops_sequence_and_moves_deal(self):
        t = FakeTransport()

        async def fn(db):
            mb, seq, (c,) = await self.setup_world(db)
            await sequences.enroll(db, seq, c, now=NOW)
            await sequences.process_due(db, t, now=NOW)
            t.inbox.append(sequences.IncomingMail(uid=10, sender="info@kuhni0.ru", subject="Re: Заявки",
                                                  body="Давайте созвонимся завтра\n\n> Здравствуйте",
                                                  in_reply_to=t.sent[0].message_id))
            counts = await sequences.poll_mailbox(db, t, mb, now=NOW + timedelta(hours=2))
            later = await sequences.process_due(db, t, now=NOW + timedelta(days=10))
            enr = await db.scalar(select(LgEnrollment))
            deal = await db.get(CrmDeal, c.crm_deal_id)
            stage = (await db.get(CrmStage, deal.stage_id)).name
            note = await db.scalar(select(CrmActivity.payload).where(CrmActivity.deal_id == deal.id)
                                   .order_by(CrmActivity.id.desc()).limit(1))
            inbound = await db.scalar(select(LgTouch).where(LgTouch.direction == "in"))
            return counts, later, enr, c, stage, note, inbound, mb
        counts, later, enr, c, stage, note, inbound, mb = self.run_db(fn)
        self.assertEqual(counts, {"reply": 1})
        self.assertEqual((later["emails"], later["tasks"]), (0, 0))
        self.assertEqual((enr.status, c.stage, stage), ("replied", "replied", "Ответил"))
        self.assertIn("Давайте созвонимся завтра", note["text"])
        self.assertEqual(inbound.body, "Давайте созвонимся завтра")
        self.assertEqual(mb.imap_last_uid, 10)

    def test_unsubscribe_and_bounce(self):
        t = FakeTransport()

        async def fn(db):
            mb, seq, (a, b) = await self.setup_world(db, n=2)
            for c in (a, b):
                await sequences.enroll(db, seq, c, now=NOW)
            await sequences.process_due(db, t, now=NOW)
            t.inbox += [
                sequences.IncomingMail(uid=1, sender="info@kuhni0.ru", subject="Re: Заявки", body="Не пишите нам больше"),
                sequences.IncomingMail(uid=2, sender="mailer-daemon@mx.ru", subject="Undelivered Mail Returned",
                                       body="Final-Recipient: rfc822; info@kuhni1.ru\nStatus: 5.1.1"),
            ]
            counts = await sequences.poll_mailbox(db, t, mb, now=NOW)
            enrs = {e.company_id: e for e in (await db.execute(select(LgEnrollment))).scalars()}
            dnc = {(d.kind, d.value_norm, d.reason) for d in (await db.execute(select(LgDnc))).scalars()}
            contact_b = await db.scalar(select(LgContact).where(LgContact.company_id == b.id))
            deal_a = await db.get(CrmDeal, a.crm_deal_id)
            stage_a = (await db.get(CrmStage, deal_a.stage_id)).name
            return counts, enrs, dnc, a, b, contact_b, stage_a
        counts, enrs, dnc, a, b, contact_b, stage_a = self.run_db(fn)
        self.assertEqual(counts, {"unsubscribe": 1, "bounce": 1})
        self.assertEqual((enrs[a.id].status, enrs[a.id].stop_reason), ("stopped", "unsubscribed"))
        self.assertEqual(enrs[b.id].status, "bounced")
        self.assertIn(("email", "info@kuhni0.ru", "unsubscribed"), dnc)
        self.assertIn(("company", str(a.id), "unsubscribed"), dnc)
        self.assertIn(("email", "info@kuhni1.ru", "bounced"), dnc)
        self.assertEqual((a.stage, a.score, stage_a), ("dnc", 0, "Отказ"))
        self.assertTrue(contact_b.bounced)

    def test_warmup_limit_postpones_and_refused_recipient(self):
        t = FakeTransport(refuse={"info@kuhni6.ru"})

        async def fn(db):
            mb, seq, cs = await self.setup_world(db, n=7, warmup=NOW.date())
            for c in cs:
                await sequences.enroll(db, seq, c, now=NOW)
            stats = await sequences.process_due(db, t, now=NOW)
            statuses = [e.status for e in (await db.execute(select(LgEnrollment).order_by(LgEnrollment.id))).scalars()]
            return stats, statuses
        stats, statuses = self.run_db(fn)
        self.assertEqual(len(t.sent), 5)                      # прогрев: 5 писем в первый день
        self.assertEqual((stats["emails"], stats["postponed"]), (5, 2))
        self.assertEqual(statuses.count("active"), 7)

    def test_refused_recipient_is_bounced(self):
        t = FakeTransport(refuse={"info@kuhni0.ru"})

        async def fn(db):
            mb, seq, (c,) = await self.setup_world(db)
            await sequences.enroll(db, seq, c, now=NOW)
            await sequences.process_due(db, t, now=NOW)
            return await db.scalar(select(LgEnrollment)), await db.scalar(select(func.count(LgDnc.id)))
        enr, dnc = self.run_db(fn)
        self.assertEqual((enr.status, dnc), ("bounced", 1))

    def test_enroll_without_email_and_with_dnc(self):
        async def fn(db):
            mb, seq, (c,) = await self.setup_world(db)
            no_email = (await service.upsert_company(db, 7, service.FindingIn(
                source="site", domain="phoneonly.ru", contacts=[{"kind": "phone", "value": "89120001111"}]), now=NOW)).company
            r1 = await sequences.enroll(db, seq, no_email, now=NOW)
            db.add(LgDnc(workspace_id=7, kind="domain", value_norm="kuhni0.ru", reason="refused"))
            await db.flush()
            r2 = await sequences.enroll(db, seq, c, now=NOW)
            return r1, r2
        r1, r2 = self.run_db(fn)
        self.assertEqual((r1.reason, r2.reason), ("no_email", "dnc:domain"))


class SequenceApiTests(FlowBase):
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

    def tearDown(self):
        settings.agency_workspace_id, routes.mail_transport_factory = self._old
        super().tearDown()

    def test_mailboxes_sequences_enroll(self):
        base = "/api/admin/leadgen"
        box = {"email": "egor@steptolead-team.ru", "smtp_host": "smtp.yandex.ru", "imap_host": "imap.yandex.ru",
               "password": "secret", "sender_name": "Егор"}
        self.assertEqual(self.client.post(f"{base}/mailboxes", headers=ORIGIN,
                                          json={**box, "email": "egor@steptolead.ru"}).status_code, 422)
        seq = self.client.post(f"{base}/sequences", headers=ORIGIN, json={"name": "Кухни", "steps": STEPS}).json()
        cid = self.run_db(lambda db: service.upsert_company(db, 7, service.FindingIn(
            source="site", domain="kuhni.ru", contacts=[{"kind": "email", "value": "info@kuhni.ru"}]), now=NOW)).company.id
        r = self.client.post(f"{base}/sequences/{seq['id']}/enroll", headers=ORIGIN, json={"company_ids": [cid]})
        self.assertEqual(r.status_code, 409)  # нет ящика
        mb = self.client.post(f"{base}/mailboxes", headers=ORIGIN, json=box)
        self.assertEqual(mb.status_code, 201, mb.text)
        self.assertNotIn("password", mb.json())
        self.assertEqual(mb.json()["left_today"], 5)
        self.assertEqual(self.client.post(f"{base}/mailboxes/{mb.json()['id']}/check", headers=ORIGIN).json()["ok"], True)
        bad = self.client.post(f"{base}/sequences", headers=ORIGIN,
                               json={"name": "x", "steps": [{"channel": "email", "body": "{{price}}", "subject": "s"}]})
        self.assertEqual(bad.status_code, 422)
        r = self.client.post(f"{base}/sequences/{seq['id']}/enroll", headers=ORIGIN, json={"company_ids": [cid, 999]}).json()
        self.assertEqual((r["enrolled"], r["skipped"]), (1, 1))
        items = self.client.get(f"{base}/sequences/{seq['id']}/enrollments").json()["items"]
        self.assertEqual((items[0]["status"], items[0]["steps_total"]), ("active", 3))
        eid = items[0]["id"]
        self.assertEqual(self.client.post(f"{base}/enrollments/{eid}", headers=ORIGIN, json={"action": "pause"}).json()["status"], "paused")
        self.assertEqual(self.client.post(f"{base}/enrollments/{eid}", headers=ORIGIN, json={"action": "resume"}).json()["status"], "active")
        prev = self.client.post(f"{base}/sequences/preview", headers=ORIGIN, json={"company_id": cid, "steps": STEPS}).json()
        self.assertIn("коллеги", prev["steps"][0]["body"])
        listed = self.client.get(f"{base}/sequences").json()
        self.assertEqual(listed["items"][0]["counts"], {"active": 1})
        self.assertEqual(self.client.delete(f"{base}/mailboxes/{mb.json()['id']}", headers=ORIGIN).status_code, 204)


if __name__ == "__main__":
    unittest.main()
