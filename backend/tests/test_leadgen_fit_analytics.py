"""ИИ-оценка «подходит ли под ЦА» (на подставной модели) и аналитика аутрича."""
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
from app.domains.leadgen import analytics, fit_ai, routes, sequences, service, site_enrich
from app.domains.leadgen.models import LgAd, LgCompany, LgEnrichment, LgMailbox, LgSequence, LgSignal, LgTouch
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace
from app.services import ai

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
ORIGIN = {"Origin": "http://localhost:3000"}


def fake_model(answer, seen=None):
    async def complete(system, messages, **kw):
        if seen is not None:
            seen.append((system, messages[0]["content"], kw))
        if isinstance(answer, Exception):
            raise answer
        if kw.get("validator"):
            kw["validator"](answer)
        return answer
    return complete


class ParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(fit_ai.parse_answer('Вот: {"label": "FIT", "reason": "B2C, свой цех", "chain": false}'),
                         {"label": "fit", "reason": "B2C, свой цех", "chain": False})
        for bad in ("", "{oops}", '{"label": "great"}'):
            with self.assertRaises(fit_ai.FitParseError):
                fit_ai.parse_answer(bad)


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

    async def company(self, db, domain="kuhni.ru", niche="Кухни на заказ", email=True):
        c = (await service.upsert_company(db, 7, service.FindingIn(
            source="site", name=domain.split(".")[0].title(), domain=domain, niche=niche, city="Екатеринбург",
            contacts=[{"kind": "email", "value": f"info@{domain}"}] if email else []), now=NOW)).company
        db.add(LgAd(company_id=c.id, content_hash=f"h{c.id}", title="Кухни на заказ от 14 дней", text="Свой цех"))
        await db.flush()
        return c


class FitTests(Base_):
    def test_assess_sets_label_signal_and_caps_score(self):
        seen = []

        async def fn(db):
            c = await self.company(db)
            for kind in ("ad_direct", "ad_premium"):
                await service.add_signal(db, c, kind, now=NOW)
            db.add(LgEnrichment(company_id=c.id, step="site_check", status="done",
                                result={"text_excerpt": "Мебельная фабрика, 120 салонов по России"}))
            await db.flush()
            res = await fit_ai.assess(db, c, now=NOW, complete=fake_model(
                '{"label": "no", "reason": "Федеральная сеть салонов", "chain": true}', seen))
            chain = await db.scalar(select(LgSignal.id).where(LgSignal.company_id == c.id, LgSignal.kind == "chain"))
            return c, res, chain
        c, res, chain = self.run_db(fn)
        self.assertEqual((c.fit_label, c.fit_reason), ("no", "Федеральная сеть салонов"))
        self.assertTrue(chain)
        self.assertEqual(c.score, 2)  # реклама 3 + 2, сеть −3; «не подходит» — не выше 2
        system, prompt, kw = seen[0]
        self.assertIn("Кухни на заказ от 14 дней", prompt)
        self.assertIn("120 салонов", prompt)
        self.assertIn("Не подходят", prompt)
        self.assertEqual((kw["feature"], kw["workspace_id"]), ("summary", 7))

    def test_custom_icp_and_failure(self):
        seen = []

        async def fn(db):
            await service.ensure_settings(db)
            row = await db.get(models.AppSetting, service.SETTINGS_KEY)
            row.value = {**row.value, "icp": "Только стоматологии Самары, частные клиники."}
            await db.flush()
            a = await self.company(db)
            ok = await fit_ai.assess(db, a, now=NOW, complete=fake_model('{"label":"maybe","reason":"мало данных"}', seen))
            b = await self.company(db, domain="other.ru")
            failed = await fit_ai.assess(db, b, now=NOW, complete=fake_model(ai.AIError("ИИ не подключён")))
            step = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == b.id,
                                                              LgEnrichment.step == "fit_ai"))
            return ok, failed, b, step
        ok, failed, b, step = self.run_db(fn)
        self.assertIn("Только стоматологии Самары", seen[0][1])
        self.assertEqual((ok["label"], failed["status"], b.fit_label, step.status), ("maybe", "failed", None, "failed"))

    def test_site_excerpt_saved(self):
        from test_leadgen_site import SITE, site

        async def fn(db):
            c = await self.company(db, domain="kuhni-sever.ru")
            summary = await site_enrich.enrich_company(db, c, transport=site(SITE), delay=0, now=NOW)
            return summary
        summary = self.run_db(fn)
        self.assertIn("Пишите", summary["text_excerpt"])


class AnalyticsTests(Base_):
    def test_funnel_sequences_mailboxes(self):
        class T:
            sent = []

            async def send(self, mb, pw, mail):
                self.sent.append(mail)

        async def fn(db):
            mb = LgMailbox(workspace_id=7, email="egor@team.ru", smtp_host="s", imap_host="i", login="egor@team.ru",
                           password_encrypted=encrypt_secret("p"), daily_limit=30)
            seq = LgSequence(workspace_id=7, name="Кухни", window={"tz": "Europe/Moscow"},
                             steps=[{"channel": "email", "subject": "Привет", "body": "Текст"},
                                    {"channel": "email", "delay_days": 3, "body": "Напомню"}])
            db.add_all([mb, seq])
            await db.flush()
            a, b = await self.company(db, "a.ru"), await self.company(db, "b.ru")
            await self.company(db, "c.ru", niche="Окна", email=False)
            t = T()
            for c in (a, b):
                await sequences.enroll(db, seq, c, now=NOW)
            await sequences.process_due(db, t, now=NOW)
            await sequences.handle_inbound(db, mb, sequences.IncomingMail(
                uid=1, sender="info@a.ru", subject="Re: Привет", body="Интересно", in_reply_to=t.sent[0].message_id), now=NOW)
            # +3 дня от среды — суббота: письмо переносится на понедельник 10:00 МСК, поэтому проверяем после выходных.
            await sequences.process_due(db, t, now=NOW + timedelta(days=6))
            return await analytics.overview(db, 7, days=30, now=NOW + timedelta(days=6))
        data = self.run_db(fn)
        funnel = {f["key"]: f["count"] for f in data["funnel"]}
        self.assertEqual(funnel, {"found": 3, "with_contacts": 2, "in_outreach": 2, "written": 2, "replied": 1, "converted": 0})
        self.assertEqual(data["funnel"][1]["from_previous"], 66.7)
        seq = data["sequences"][0]
        self.assertEqual((seq["enrolled"], seq["replied"], seq["reply_rate"]), (2, 1, 50.0))
        self.assertEqual(seq["emails"], [{"email": 1, "sent": 2, "replies": 1, "bounced": 0, "reply_rate": 50.0},
                                         {"email": 2, "sent": 1, "replies": 0, "bounced": 0, "reply_rate": 0.0}])
        niches = {n["niche"]: n for n in data["niches"]}
        self.assertEqual((niches["Кухни на заказ"]["in_outreach"], niches["Кухни на заказ"]["replied"]), (2, 1))
        self.assertEqual(data["mailboxes"][0]["sent_7d"], 3)


class ApiTests(Base_):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = (settings.agency_workspace_id, routes.session_factory, routes.fit_available, routes.fit_complete)
        settings.agency_workspace_id = 7
        routes.session_factory = self.sessions
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        settings.agency_workspace_id, routes.session_factory, routes.fit_available, routes.fit_complete = self._old
        super().tearDown()

    def test_assess_settings_analytics(self):
        base = "/api/admin/leadgen"
        cid = self.run_db(lambda db: self.company(db)).id
        routes.fit_available = lambda: False
        self.assertEqual(self.client.post(f"{base}/companies/assess", headers=ORIGIN, json={"company_ids": [cid]}).status_code, 400)
        routes.fit_available = lambda: True
        routes.fit_complete = fake_model('{"label": "fit", "reason": "B2C, свой цех"}')
        r = self.client.post(f"{base}/companies/assess", headers=ORIGIN, json={"company_ids": [cid, 999]})
        self.assertEqual(r.json(), {"queued": 1, "skipped": 1})
        card = self.client.get(f"{base}/companies/{cid}").json()
        self.assertEqual(card["fit_label"], "fit")
        self.assertEqual(self.client.get(f"{base}/companies", params={"fit": "fit"}).json()["total"], 1)
        self.assertEqual(self.client.get(f"{base}/companies", params={"fit": "no"}).json()["total"], 0)
        self.assertEqual(self.client.get(f"{base}/companies", params={"fit": "bad"}).status_code, 422)
        self.assertEqual(self.client.get(f"{base}/companies", params={"run_id": 999}).status_code, 404)
        s = self.client.get(f"{base}/settings").json()
        self.assertTrue(s["icp"].startswith("Подходят"))
        self.client.put(f"{base}/settings", headers=ORIGIN, json={"icp": "Только кухни в Самаре и области, B2C."})
        self.assertEqual(self.client.get(f"{base}/settings").json()["icp"], "Только кухни в Самаре и области, B2C.")
        a = self.client.get(f"{base}/analytics", params={"days": 30}).json()
        self.assertEqual(a["funnel"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
