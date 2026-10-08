"""«Входящие»: разметка ответов (правила / ИИ / человек), последствия меток, ответ из портала, API."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db import Base, get_db
from app.domains.leadgen import analytics, inbox, routes, sequences, service
from app.domains.leadgen.models import LgCompany, LgDnc, LgEnrollment, LgMailbox, LgSequence, LgTouch
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmDeal, CrmStage, CrmTask
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)  # среда, 12:00 по Москве
ORIGIN = {"Origin": "http://localhost:3000"}


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, mb, pw, mail):
        self.sent.append(mail)


def fake_model(answer):
    async def complete(system, messages, **kw):
        if kw.get("validator"):
            kw["validator"](answer)
        return answer
    return complete


class RuleTests(unittest.TestCase):
    def test_rule_label(self):
        cases = {
            "Не интересно, спасибо": "not_interested",
            "Нам это не нужно": "not_interested",
            "Уже работаем с агентством": "not_interested",
            "Напишите через месяц, сейчас сезон": "later",
            "Давайте созвонимся в четверг": "interested",
            "Интересно. Сколько стоит?": "interested",
            "А вы кто вообще?": "question",
            "Ок": "other",
        }
        for text, label in cases.items():
            self.assertEqual(inbox.rule_label(text), label, text)
        self.assertEqual(inbox.initial_label("auto_reply", "В отпуске"), ("auto", "rule"))
        self.assertEqual(inbox.initial_label("unsubscribe", "нет"), ("unsubscribe", "rule"))

    def test_parse(self):
        self.assertEqual(inbox.parse_answer('{"label":"LATER","summary":"  после  сезона "}'),
                         {"label": "later", "summary": "после сезона"})
        with self.assertRaises(inbox.LabelParseError):
            inbox.parse_answer('{"label":"maybe"}')


class Base_(unittest.TestCase):
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
        self.transport = Transport()

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

    def seed_replies(self):
        """Три компании в цепочке; ответы: интерес, отказ, автоответ."""
        async def fn(db):
            mb = LgMailbox(workspace_id=7, email="egor@team.ru", sender_name="Егор", smtp_host="s", imap_host="i",
                           login="egor@team.ru", password_encrypted=encrypt_secret("p"), daily_limit=30)
            seq = LgSequence(workspace_id=7, name="Кухни", window={"tz": "Europe/Moscow"},
                             steps=[{"channel": "email", "subject": "Привет", "body": "Текст"},
                                    {"channel": "email", "delay_days": 3, "body": "Напомню"}])
            db.add_all([mb, seq])
            await db.flush()
            ids = {}
            for name in ("a", "b", "c"):
                c = (await service.upsert_company(db, 7, service.FindingIn(
                    source="site", name=name.upper(), domain=f"{name}.ru", niche="Кухни", city=f"Город {name}",
                    contacts=[{"kind": "email", "value": f"info@{name}.ru"}]), now=NOW)).company
                await sequences.enroll(db, seq, c, now=NOW)
                ids[name] = c.id
            await sequences.process_due(db, self.transport, now=NOW)
            by_to = {m.to: m for m in self.transport.sent}
            replies = {"a": ("Интересно, пришлите цены", {}), "b": ("Не интересно, спасибо", {}),
                       "c": ("Я в отпуске до 20-го", {"Auto-Submitted": "auto-replied"})}
            for uid, (name, (text, headers)) in enumerate(replies.items(), start=1):
                kind = await sequences.handle_inbound(db, mb, sequences.IncomingMail(
                    uid=uid, sender=f"info@{name}.ru", subject="Re: Привет", body=text, headers=headers,
                    message_id=f"<in{uid}@{name}.ru>", in_reply_to=by_to[f"info@{name}.ru"].message_id),
                    now=NOW + timedelta(hours=1))
                self.assertNotEqual(kind, "unmatched")
            return ids
        return self.run_db(fn)

    async def touch_of(self, db, company_id):
        return await db.scalar(select(LgTouch).where(LgTouch.company_id == company_id, LgTouch.direction == "in"))


class InboxTests(Base_):
    def test_rules_listing_and_effects(self):
        ids = self.seed_replies()

        async def check(db):
            data = await inbox.listing(db, 7)
            self.assertEqual(data["unhandled"], 2)  # автоответ разобран сам
            self.assertEqual({i["company"]: i["label"] for i in data["items"]}, {"A": "interested", "B": "not_interested"})
            self.assertEqual(data["counts"], {"interested": 1, "not_interested": 1})
            self.assertEqual(data["items"][0]["address"], "info@b.ru" if data["items"][0]["company"] == "B" else "info@a.ru")
            all_ = await inbox.listing(db, 7, status="all", label="auto")
            self.assertEqual([i["company"] for i in all_["items"]], ["C"])
            self.assertEqual((await inbox.listing(db, 7, q="пришлите"))["total"], 1)

            a, b = await db.get(LgCompany, ids["a"]), await db.get(LgCompany, ids["b"])
            ta, tb = await self.touch_of(db, a.id), await self.touch_of(db, b.id)
            # Метка от правил ничего не закрывает: B всё ещё «ответил», а не «отказ».
            self.assertEqual(b.stage, "replied")
            res = await inbox.apply_label(db, ta, a, "interested", now=NOW)
            task = await db.get(CrmTask, res.task_id)
            self.assertEqual((task.type_code, task.deal_id), ("CALL", a.crm_deal_id))
            again = await inbox.apply_label(db, ta, a, "interested", now=NOW)
            self.assertIsNone(again.task_id)  # повтор не плодит задачи

            await inbox.apply_label(db, tb, b, "not_interested", now=NOW)
            self.assertEqual((b.stage, b.score), ("rejected", 0))
            dnc = await db.scalar(select(LgDnc).where(LgDnc.kind == "company", LgDnc.value_norm == str(b.id)))
            self.assertEqual(dnc.reason, "refused")
            self.assertIsNotNone(dnc.until)
            deal = await db.get(CrmDeal, b.crm_deal_id)
            self.assertEqual((await db.get(CrmStage, deal.stage_id)).name, "Отказ")
            self.assertEqual((await inbox.listing(db, 7))["unhandled"], 0)
        self.run_db(check)

    def test_later_stops_chain_and_reminds(self):
        ids = self.seed_replies()

        async def fn(db):
            a = await db.get(LgCompany, ids["a"])
            t = await self.touch_of(db, a.id)
            enr = await db.scalar(select(LgEnrollment).where(LgEnrollment.company_id == a.id))
            enr.status = "active"  # будто цепочка ещё идёт
            res = await inbox.apply_label(db, t, a, "later", remind_days=45, now=NOW)
            task = await db.get(CrmTask, res.task_id)
            self.assertEqual(service._aware(task.due_at), NOW + timedelta(days=45))
            self.assertEqual((enr.status, enr.stop_reason), ("stopped", "later"))
        self.run_db(fn)

    def test_ai_refines_but_never_overrides_user(self):
        ids = self.seed_replies()

        async def fn(db):
            n = await inbox.classify_pending(db, complete=fake_model('{"label": "question", "summary": "Спрашивают цену"}'))
            self.assertEqual(n, 2)
            ta = await self.touch_of(db, ids["a"])
            self.assertEqual((ta.label, ta.label_source, ta.summary), ("question", "ai", "Спрашивают цену"))
            await inbox.apply_label(db, ta, await db.get(LgCompany, ids["a"]), "interested", now=NOW)
            res = await inbox.classify(db, ta, complete=fake_model('{"label": "other"}'))
            self.assertEqual((res["status"], ta.label), ("skipped", "interested"))
            # ИИ не ответил по формату -> письмо больше не дёргаем
            tb = await self.touch_of(db, ids["b"])
            tb.label_source = "rule"
            self.assertEqual(await inbox.classify_pending(db, complete=fake_model("не знаю")), 0)
            self.assertEqual(tb.label_source, "rule_only")
        self.run_db(fn)

    def test_reply_threads_and_skips_analytics(self):
        ids = self.seed_replies()

        async def fn(db):
            a = await db.get(LgCompany, ids["a"])
            t = await self.touch_of(db, a.id)
            out = await inbox.send_reply(db, self.transport, t, a, "Пришлю сегодня", user_id=1, now=NOW + timedelta(hours=2))
            mail = self.transport.sent[-1]
            self.assertEqual((mail.to, mail.subject, mail.in_reply_to), ("info@a.ru", "Re: Привет", "<in1@a.ru>"))
            self.assertEqual((out.campaign_kind, out.user_id, out.sequence_id), ("manual_reply", 1, t.sequence_id))
            self.assertIsNotNone(t.handled_at)
            full = await inbox.row_out(db, t, a, full=True)
            self.assertEqual([(x["direction"], x["manual"]) for x in full["thread"]],
                             [("out", False), ("in", False), ("out", True)])
            report = await analytics.sequences_report(db, 7, NOW - timedelta(days=1))
            self.assertEqual([e["email"] for e in report[0]["emails"]], [1])  # ручной ответ — не «письмо 2»
            # Отписавшимся не отвечаем
            b = await db.get(LgCompany, ids["b"])
            tb = await self.touch_of(db, b.id)
            await inbox.apply_label(db, tb, b, "unsubscribe", now=NOW)
            with self.assertRaises(inbox.ReplyError):
                await inbox.send_reply(db, self.transport, tb, b, "Ну пожалуйста")
        self.run_db(fn)


class ApiTests(Base_):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = (settings.agency_workspace_id, routes.session_factory, routes.mail_transport_factory,
                     routes.inbox_available, routes.inbox_complete)
        settings.agency_workspace_id = 7
        routes.session_factory = self.sessions
        routes.mail_transport_factory = lambda: self.transport
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        (settings.agency_workspace_id, routes.session_factory, routes.mail_transport_factory,
         routes.inbox_available, routes.inbox_complete) = self._old
        super().tearDown()

    def test_inbox_api(self):
        self.seed_replies()
        base = "/api/admin/leadgen/inbox"
        data = self.client.get(base).json()
        self.assertEqual((data["total"], data["unhandled"]), (2, 2))
        self.assertEqual(len(data["labels"]), len(inbox.LABELS))
        item = next(i for i in data["items"] if i["company"] == "A")
        full = self.client.get(f"{base}/{item['id']}").json()
        self.assertTrue(full["can_reply"])
        self.assertEqual(full["body"], "Интересно, пришлите цены")
        self.assertEqual(self.client.get(f"{base}/99999").status_code, 404)

        r = self.client.patch(f"{base}/{item['id']}", headers=ORIGIN, json={"label": "interested"})
        self.assertEqual((r.json()["label_source"], r.json()["effect"], r.json()["handled"]), ("user", "task:call", True))
        r = self.client.patch(f"{base}/{item['id']}", headers=ORIGIN, json={"handled": False})
        self.assertFalse(r.json()["handled"])
        self.assertEqual(self.client.patch(f"{base}/{item['id']}", headers=ORIGIN, json={"label": "spam"}).status_code, 422)

        r = self.client.post(f"{base}/{item['id']}/reply", headers=ORIGIN, json={"body": "Отправляю цены"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.transport.sent[-1].body, "Отправляю цены")
        self.assertEqual(self.client.post(f"{base}/{item['id']}/reply", headers=ORIGIN, json={"body": ""}).status_code, 422)

        routes.inbox_available = lambda: False
        self.assertEqual(self.client.post(f"{base}/{item['id']}/classify", headers=ORIGIN).status_code, 400)
        routes.inbox_available = lambda: True
        self.assertEqual(self.client.post(f"{base}/{item['id']}/classify", headers=ORIGIN).status_code, 409)  # метку ставил человек
        other = next(i for i in data["items"] if i["company"] == "B")
        routes.inbox_complete = fake_model('{"label": "later", "summary": "Просят после сезона"}')
        r = self.client.post(f"{base}/{other['id']}/classify", headers=ORIGIN)
        self.assertEqual((r.json()["label"], r.json()["summary"]), ("later", "Просят после сезона"))
        self.assertEqual(self.client.get(base, params={"label": "later"}).json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
