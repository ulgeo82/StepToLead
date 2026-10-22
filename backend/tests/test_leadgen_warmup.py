"""Прогрев почты: план дня, переписка пула, спасение из «Спама», ответы, здоровье и автопауза холодных писем."""
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.router import api_router
from app.core.access import COOKIE
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db import get_db
from app.domains.leadgen import routes, sequences, warmup
from app.domains.leadgen.models import LgMailbox, LgTouch, LgWarmupMail
from test_leadgen_sequences import NOW, ORIGIN, FakeTransport, FlowBase

DNS_OK = {"spf": True, "dmarc": True, "dmarc_policy": "none", "mx": True, "blacklisted": False}


class WarmTransport(FakeTransport):
    def __init__(self, spam=()):
        super().__init__()
        self.spam = set(spam)   # адреса получателей, у которых письма падают в «Спам»
        self.located = []

    async def warm_locate(self, mailbox, password, message_ids, flag=frozenset()):
        assert password == "secret"
        self.located.append((mailbox.email, list(message_ids)))
        sent = {m.message_id: m for m in self.sent}
        return {mid: {"folder": "spam" if mailbox.email in self.spam else "inbox", "auth": {"dkim": "pass", "spf": "pass"}}
                for mid in message_ids if mid in sent and sent[mid].to == mailbox.email}


def box(email, role="outreach", started=date(2026, 10, 7), host="smtp.yandex.ru"):
    return LgMailbox(workspace_id=7, email=email, sender_name="Егор Д", smtp_host=host, imap_host="imap.x", login=email,
                     password_encrypted=encrypt_secret("secret"), daily_limit=30, warmup_started_on=started, role=role,
                     warmup_enabled=True, warmup_target=25, is_active=True)


class PlanTests(unittest.TestCase):
    def test_ramp_maintenance_helper_weekend(self):
        wed = date(2026, 10, 7)
        mb = SimpleNamespace(is_active=True, warmup_enabled=True, role="outreach", warmup_target=25, warmup_started_on=wed)
        self.assertEqual(warmup.daily_plan(mb, wed), 2)
        mb.warmup_started_on = wed - timedelta(days=20)
        self.assertEqual(warmup.daily_plan(mb, wed), 25)
        mb.warmup_started_on = wed - timedelta(days=40)
        self.assertEqual(warmup.daily_plan(mb, wed), warmup.MAINTENANCE)
        self.assertEqual(warmup.daily_plan(mb, date(2026, 10, 10)), 5)   # суббота — вполовину
        mb.role = "helper"
        self.assertEqual(warmup.daily_plan(mb, wed), warmup.HELPER_DAILY)
        mb.warmup_enabled = False
        self.assertEqual(warmup.daily_plan(mb, wed), 0)

    def test_texts_vary_and_have_no_templates_left(self):
        mb = SimpleNamespace(sender_name="Егор Дементьев", email="egor@x.ru")
        texts = {warmup.compose(mb, f"seed{i}") for i in range(30)}
        self.assertGreater(len(texts), 25)
        for subject, body in texts:
            self.assertNotIn("{", subject + body)
            self.assertTrue(body.endswith("Егор"))
        subject, body = warmup.compose_reply(mb, "r", "План работ")
        self.assertEqual(subject, "Re: План работ")

    def test_cold_capacity(self):
        self.assertEqual(warmup.cold_capacity(SimpleNamespace(role="helper", cold_paused_at=None, health=90), 10), 0)
        self.assertEqual(warmup.cold_capacity(SimpleNamespace(role="outreach", cold_paused_at=NOW, health=90), 10), 0)
        self.assertEqual(warmup.cold_capacity(SimpleNamespace(role="outreach", cold_paused_at=None, health=40), 10), 5)
        self.assertEqual(warmup.cold_capacity(SimpleNamespace(role="outreach", cold_paused_at=None, health=None), 10), 10)


class FlowTests(FlowBase):
    def world(self, db):
        a, b, h = box("egor@team-a.ru"), box("anna@team-b.ru"), box("helper@yandex.ru", role="helper")
        db.add_all([a, b, h])
        return a, b, h

    def test_pool_sends_rescues_and_replies(self):
        t = WarmTransport(spam={"anna@team-b.ru"})

        async def fn(db):
            a, b, h = self.world(db)
            await db.flush()
            first = await warmup.send_due(db, t, 7, now=NOW)
            again = await warmup.send_due(db, t, 7, now=NOW + timedelta(minutes=1))   # пауза между письмами
            night = await warmup.send_due(db, t, 7, now=datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc))  # 23:00 МСК
            helper_mail = next(m for m in t.sent if m.sender == "helper@yandex.ru")
            stats = await warmup.check_due(db, t, 7, now=NOW + timedelta(minutes=5))
            rows = (await db.scalars(select(LgWarmupMail).order_by(LgWarmupMail.id))).all()
            return a, b, first, again, night, helper_mail, stats, rows

        a, b, first, again, night, helper_mail, stats, rows = self.run_db(fn)
        self.assertEqual((first, again, night), (3, 0, 0))
        self.assertNotEqual(helper_mail.to, "helper@yandex.ru")   # помощник пишет только рабочим ящикам
        self.assertEqual({m.sender for m in t.sent}, {"egor@team-a.ru", "anna@team-b.ru", "helper@yandex.ru"})
        self.assertEqual(stats["inbox"] + stats["spam"], 3)
        to_anna = [r for r in rows if r.to_mailbox_id == b.id and r.depth == 0]
        self.assertTrue(to_anna and all(r.status == "spam" and r.rescued for r in to_anna))
        replies = [r for r in rows if r.status == "scheduled"]
        self.assertEqual(len(replies), stats["replies"])
        for r in replies:
            self.assertEqual(r.depth, 1)
            self.assertTrue(r.subject.startswith("Re: "))
            self.assertIsNotNone(r.in_reply_to)

        # ответы уходят, когда пришло их время, в ту же ветку
        if replies:
            later = max(r.send_after for r in replies) + timedelta(minutes=1)
            sent = self.run_db(lambda db: warmup.send_due(db, t, 7, now=later.replace(tzinfo=timezone.utc)))
            self.assertGreaterEqual(sent, len(replies))
            self.assertTrue(any(m.in_reply_to for m in t.sent))

    def test_missing_after_six_hours(self):
        t = WarmTransport()

        async def fn(db):
            a, b, h = self.world(db)
            await db.flush()
            db.add(LgWarmupMail(workspace_id=7, from_mailbox_id=a.id, to_mailbox_id=b.id, message_id="<lost@x>",
                                subject="s", body="b", status="sent", sent_at=NOW - timedelta(hours=7)))
            await db.flush()
            await warmup.check_due(db, t, 7, now=NOW)
            return await db.scalar(select(LgWarmupMail.status).where(LgWarmupMail.message_id == "<lost@x>"))
        self.assertEqual(self.run_db(fn), "missing")

    def test_inbound_from_pool_is_not_a_lead_reply(self):
        t = WarmTransport()
        t.inbox.append(sequences.IncomingMail(uid=5, sender="anna@team-b.ru", subject="Re: План", body="Ок"))

        async def fn(db):
            a, b, h = self.world(db)
            await db.flush()
            counts = await sequences.poll_mailbox(db, t, a, now=NOW)
            touches = await db.scalar(select(func.count(LgTouch.id)))
            return counts, touches, a.imap_last_uid
        counts, touches, uid = self.run_db(fn)
        self.assertEqual(counts, {"warmup": 1})
        self.assertEqual((touches, uid), (0, 5))

    def test_health_and_auto_pause(self):
        async def fn(db):
            a, b, h = self.world(db)
            await db.flush()
            for i in range(10):
                db.add(LgWarmupMail(workspace_id=7, from_mailbox_id=a.id, to_mailbox_id=b.id, message_id=f"<m{i}@x>",
                                    subject="s", body="b", status="spam" if i < 6 else "inbox",
                                    sent_at=NOW - timedelta(days=1), auth={"dkim": "pass"}))
            await db.flush()
            with patch.object(warmup, "dns_checks", new=AsyncMock(return_value=DNS_OK)), \
                    patch("app.services.monitor.send", new=AsyncMock()) as notify:
                health = await warmup.refresh_health(db, a, now=NOW)
                await warmup.refresh_health(db, a, now=NOW)   # повторно — без второго сообщения
            good = await warmup.compute_health(db, b, NOW, {**DNS_OK, "spf": False})
            return a, health, notify.await_count, good
        a, health, notified, good = self.run_db(fn)
        self.assertEqual(health.score, 40)
        self.assertIsNotNone(a.cold_paused_at)
        self.assertIn("Спам", a.cold_paused_reason)
        self.assertEqual(notified, 1)
        self.assertEqual(warmup.cold_capacity(a, 20), 0)
        # мало данных — базовые 85 минус штраф за SPF и подсказка, как чинить
        spf = next(c for c in good.checks if c["key"] == "spf")
        self.assertEqual((good.score, spf["ok"]), (70, False))
        self.assertIn("v=spf1", spf["fix"])


class ApiTests(FlowBase):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = settings.agency_workspace_id
        settings.agency_workspace_id = 7
        app = FastAPI()
        app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        settings.agency_workspace_id = self._old
        super().tearDown()

    def test_helper_mailbox_and_overview(self):
        base = "/api/admin/leadgen"
        common = {"smtp_host": "smtp.yandex.ru", "imap_host": "imap.yandex.ru", "password": "secret"}
        r = self.client.post(f"{base}/mailboxes", headers=ORIGIN, json={**common, "email": "egor@team-a.ru"})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.post(f"{base}/mailboxes", headers=ORIGIN, json={**common, "email": "h@yandex.ru", "role": "helper"})
        self.assertEqual(r.json()["role"], "helper")
        self.assertEqual(r.json()["left_today"], 0)   # помощник не шлёт холодных
        data = self.client.get(f"{base}/warmup").json()
        self.assertEqual((data["pool"]["total"], data["pool"]["helpers"], data["running"]), (2, 1, True))
        self.assertTrue(any("меньше 4" in tip for tip in data["tips"]))
        card = data["items"][0]
        self.assertEqual(len(card["days"]), 14)
        self.assertIn("warm_plan", card["today"])
        mid = card["id"]
        self.assertEqual(self.client.patch(f"{base}/mailboxes/{mid}", headers=ORIGIN,
                                           json={"warmup_enabled": False}).json()["warmup_enabled"], False)
        with patch.object(warmup, "dns_checks", new=AsyncMock(return_value=DNS_OK)):
            checked = self.client.post(f"{base}/warmup/{mid}/check", headers=ORIGIN).json()
        self.assertEqual(checked["health"]["score"], 85)
        resumed = self.client.post(f"{base}/warmup/{mid}/resume-cold", headers=ORIGIN).json()
        self.assertIsNone(resumed["cold_paused"])


if __name__ == "__main__":
    unittest.main()
