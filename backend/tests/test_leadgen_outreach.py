"""«В аутрич»: компания -> сделка в отдельной воронке CRM, проверки стоп-листа, обратная синхронизация этапов."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.db import Base, get_db
from app.domains.leadgen import outreach, routes, service
from app.domains.leadgen.models import LgAd, LgCompany, LgContact, LgDnc
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmActivity, CrmContact, CrmDeal, CrmStage
from app.models.marketing import ClientLead, ClientWorkspace, Project

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
ORIGIN = {"Origin": "http://localhost:3000"}


class OutreachBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=7, name="StepToLead Agency"))
                db.add(Project(id=70, workspace_id=7, name="Агентство", is_default=True))
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

    async def company(self, db, domain="kuhni-sever.ru", contacts=None, **kw):
        contacts = contacts if contacts is not None else [
            {"kind": "whatsapp", "value": "+79120001122"}, {"kind": "email", "value": "info@kuhni-sever.ru"},
            {"kind": "telegram", "value": "kuhni_sever"}]
        c = (await service.upsert_company(db, 7, service.FindingIn(
            source="yandex_direct", name="Кухни Северная", domain=domain, city=kw.pop("city", "Екатеринбург"),
            contacts=contacts, **kw), now=NOW)).company
        await service.add_signal(db, c, "ad_direct", now=NOW)
        db.add(LgAd(company_id=c.id, content_hash="h" + str(c.id), title="Кухни на заказ от 14 дней",
                    text="Бесплатный замер", keywords=["кухни на заказ"], placement="premium"))
        await db.flush()
        return c


class HandOffTests(OutreachBase):
    def test_creates_separate_project_pipeline_contact_and_deal(self):
        async def fn(db):
            c = await self.company(db)
            r = await outreach.hand_off(db, c, now=NOW)
            deal = await db.get(CrmDeal, r.deal_id)
            contact = await db.get(CrmContact, deal.contact_id)
            project = await db.get(Project, deal.project_id)
            stages = [s.name for s in (await db.execute(select(CrmStage).where(
                CrmStage.pipeline_id == deal.pipeline_id).order_by(CrmStage.position))).scalars()]
            note = await db.scalar(select(CrmActivity.payload).where(CrmActivity.deal_id == deal.id,
                                                                     CrmActivity.event_type == "COMMENT_ADDED"))
            main_leads = await db.scalar(select(func.count(ClientLead.id)).where(ClientLead.project_id == 70))
            return c, r, deal, contact, project, stages, note, main_leads
        c, r, deal, contact, project, stages, note, main_leads = self.run_db(fn)
        self.assertTrue(r.ok)
        self.assertEqual((project.name, project.is_default), ("Аутрич", False))
        self.assertEqual(stages, ["Новый", "Написали", "Ответил", "Созвон назначен", "Тест портала", "Клиент", "Отказ"])
        self.assertEqual((contact.phones, contact.emails, contact.telegram),
                         (["+79120001122"], ["info@kuhni-sever.ru"], "kuhni_sever"))
        self.assertEqual((deal.name, deal.tags), ("Кухни Северная — аутрич", ["лидогенерация"]))
        self.assertIn("Что крутят: «Кухни на заказ от 14 дней»", note["text"])
        self.assertIn("В Директе по 3+ ключам", note["text"])
        self.assertEqual(main_leads, 0)  # основной проект агентства не засоряется
        self.assertEqual((c.stage, c.crm_deal_id), ("in_outreach", deal.id))

    def test_repeat_and_refusals(self):
        async def fn(db):
            a = await self.company(db)
            first = await outreach.hand_off(db, a, now=NOW)
            again = await outreach.hand_off(db, a, now=NOW)
            no_contacts = await outreach.hand_off(db, await self.company(
                db, domain="empty.ru", contacts=[{"kind": "email", "value": "ivan@empty.ru", "is_personal": True}]), now=NOW)
            db.add(LgDnc(workspace_id=7, kind="domain", value_norm="blocked.ru", reason="refused"))
            await db.flush()
            blocked = await outreach.hand_off(db, await self.company(db, domain="blocked.ru", contacts=[
                {"kind": "phone", "value": "89120005555"}]), now=NOW)
            return first, again, no_contacts, blocked
        first, again, no_contacts, blocked = self.run_db(fn)
        self.assertEqual((again.ok, again.reason, again.deal_id), (False, "already_in_crm", first.deal_id))
        self.assertEqual(no_contacts.reason, "no_contacts")
        self.assertEqual(blocked.reason, "dnc:domain")

    def test_existing_crm_contact_is_reused(self):
        async def fn(db):
            a = await self.company(db)
            await outreach.hand_off(db, a, now=NOW)
            # Тот же телефон в другом городе: в базе это отдельная компания (на ручной проверке),
            # а в CRM — тот же человек, второй контакт не создаём.
            b = await self.company(db, domain="second-site.ru", city="Казань",
                                   contacts=[{"kind": "phone", "value": "8 912 000-11-22"}])
            r = await outreach.hand_off(db, b, now=NOW)
            return a, b, r, await db.scalar(select(func.count(CrmContact.id)))
        a, b, r, contacts = self.run_db(fn)
        self.assertNotEqual(a.id, b.id)
        self.assertTrue(r.ok)
        self.assertEqual(contacts, 1)

    def test_sync_stages_back_from_crm(self):
        async def fn(db):
            out = {}
            for name, stage in (("replied", "Ответил"), ("won", "Клиент"), ("lost", "Отказ")):
                c = await self.company(db, domain=f"{name}.ru", contacts=[{"kind": "email", "value": f"info@{name}.ru"}])
                r = await outreach.hand_off(db, c, now=NOW)
                deal = await db.get(CrmDeal, r.deal_id)
                deal.stage_id = await db.scalar(select(CrmStage.id).where(CrmStage.pipeline_id == deal.pipeline_id,
                                                                          CrmStage.name == stage))
                out[name] = c
            await db.flush()
            changed = await outreach.sync_from_crm(db, 7, now=NOW)
            dnc = {(d.value_norm, d.reason, d.until is not None) for d in (await db.execute(select(LgDnc))).scalars()}
            return out, changed, dnc
        out, changed, dnc = self.run_db(fn)
        self.assertEqual(changed, 3)
        self.assertEqual([out[k].stage for k in ("replied", "won", "lost")], ["replied", "converted", "rejected"])
        self.assertEqual(dnc, {(str(out["won"].id), "client", False), (str(out["lost"].id), "refused", True)})
        self.assertEqual(out["lost"].score, 0)


class OutreachApiTests(OutreachBase):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = settings.agency_workspace_id
        settings.agency_workspace_id = 7
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        settings.agency_workspace_id = self._old
        super().tearDown()

    def test_outreach_endpoint_and_card(self):
        async def fn(db):
            a = await self.company(db)
            b = await self.company(db, domain="nocontacts.ru", contacts=[])
            return a.id, b.id
        a, b = self.run_db(fn)
        r = self.client.post("/api/admin/leadgen/companies/outreach", headers=ORIGIN, json={"company_ids": [a, b, 999]})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["moved"], body["skipped"]), (1, 2))
        self.assertEqual([x["reason_text"] for x in body["results"][1:]], ["нет контактов", "не найдена"])
        card = self.client.get(f"/api/admin/leadgen/companies/{a}").json()
        self.assertEqual((card["stage"], card["crm_deal_id"]), ("in_outreach", body["results"][0]["deal_id"]))
        pipe = self.client.get("/api/admin/leadgen/outreach/pipeline").json()
        self.assertEqual(pipe["pipeline_id"], body["pipeline_id"])
        self.assertEqual(self.client.post("/api/admin/leadgen/companies/outreach", headers=ORIGIN, json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
