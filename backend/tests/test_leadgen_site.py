"""Сбор контактов с сайтов: разбор html, вежливый обход, запись в базу, API обогащения."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.db import Base, get_db
from app.domains.leadgen import routes, service, site_enrich
from app.domains.leadgen.core.site_extract import extract
from app.domains.leadgen.models import LgCompany, LgContact, LgEnrichment, LgSignal
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
INN = "7707083893"

HOME = """<html><head><script src="https://mc.yandex.ru/metrika/tag.js"></script>
<script src="https://quiz.marquiz.ru/v2.js"></script><script src="//code.jivo.ru/widget/abc"></script></head>
<body><a href="tel:+7 (343) 200-10-20">Позвонить</a>
<a href="https://wa.me/79120001122?text=Здравствуйте">WhatsApp</a>
<a href="https://t.me/kuhni_sever">Telegram</a><a href="https://t.me/share/url?url=x">Поделиться</a>
<a href="/contacts/">Контакты</a><a href="/policy.html">Политика конфиденциальности</a>
<a href="https://vk.com/kuhni">VK</a><a href="/catalog/">Каталог</a>
<img src="/img/logo@2x.png"><p>Пишите: zakaz@kuhni-sever.ru</p></body></html>"""
CONTACTS = """<html><body><h1>Контакты</h1><p>Телефон: 8 912 000-11-22, офис +7 343 200 10 20</p>
<p>Директор: ivan.petrov@kuhni-sever.ru, общий: info@kuhni-sever.ru</p>
<p>ООО «Северная мебель», ИНН/КПП 7707083893/770701001, ОГРН 1027700132195</p></body></html>"""
AMO_PAGE = '<html><body><script src="https://gso.amocrm.ru/js/button.js"></script>ИНН 7707083894</body></html>'


def site(pages: dict[str, tuple[int, str]], robots: str | None = None, log: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots is not None else httpx.Response(404)
        status, body = pages.get(request.url.path, (404, "not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})
    return httpx.MockTransport(handler)


SITE = {"/": (200, HOME), "/contacts/": (200, CONTACTS), "/policy.html": (200, "<p>Политика</p>")}


class ExtractTests(unittest.TestCase):
    def test_home_page(self):
        f = extract(HOME, "https://kuhni-sever.ru/")
        self.assertEqual(f.phones, {"+73432001020"})
        self.assertEqual(f.whatsapp, {"+79120001122"})
        self.assertEqual(f.telegram, {"kuhni_sever"})
        self.assertEqual(f.emails, {"zakaz@kuhni-sever.ru"})  # logo@2x.png — не почта
        self.assertTrue(f.has_quiz and f.has_metrika)
        self.assertEqual((f.chats, f.crm), ({"jivo"}, set()))
        self.assertEqual(f.pages, ["https://kuhni-sever.ru/contacts/", "https://kuhni-sever.ru/policy.html"])

    def test_contacts_page_inn_and_phones(self):
        f = extract(CONTACTS, "https://kuhni-sever.ru/contacts/")
        self.assertEqual(f.phones, {"+79120001122", "+73432001020"})
        self.assertEqual(f.inns, {INN})
        self.assertEqual(f.ogrns, {"1027700132195"})
        self.assertIn("info@kuhni-sever.ru", f.emails)

    def test_crm_detected_and_bad_inn_ignored(self):
        f = extract(AMO_PAGE, "https://x.ru/")
        self.assertEqual((f.crm, f.inns), ({"amocrm"}, set()))

    def test_personal_email_detection(self):
        self.assertTrue(site_enrich._looks_personal("ivan.petrov@kuhni-sever.ru", "kuhni-sever.ru"))
        self.assertFalse(site_enrich._looks_personal("info@kuhni-sever.ru", "kuhni-sever.ru"))
        self.assertFalse(site_enrich._looks_personal("sever.kuhni@mail.ru", "kuhni-sever.ru"))


class CrawlTests(unittest.TestCase):
    def test_crawl_visits_hint_pages_only(self):
        log = []
        r = asyncio.run(site_enrich.crawl("kuhni-sever.ru", transport=site(SITE, log=log), delay=0))
        self.assertEqual(len(r.fetched), 3)
        self.assertNotIn("https://kuhni-sever.ru/catalog/", log)
        self.assertEqual(r.facts.inns, {INN})

    def test_robots_disallow_means_no_page_requests(self):
        log = []
        r = asyncio.run(site_enrich.crawl(
            "kuhni-sever.ru", transport=site(SITE, robots="User-agent: *\nDisallow: /", log=log), delay=0))
        self.assertTrue(r.blocked_by_robots)
        self.assertEqual([u for u in log if not u.endswith("/robots.txt")], [])

    def test_robots_disallow_single_page(self):
        r = asyncio.run(site_enrich.crawl("kuhni-sever.ru", transport=site(SITE, robots="User-agent: *\nDisallow: /contacts/"), delay=0))
        self.assertEqual(r.fetched, ["https://kuhni-sever.ru/", "https://kuhni-sever.ru/policy.html"])

    def test_unreachable(self):
        r = asyncio.run(site_enrich.crawl("dead.ru", transport=site({}), delay=0))
        self.assertFalse(r.ok)
        self.assertIn("https:/", r.errors)


class DbBase(unittest.TestCase):
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

    async def company(self, db, domain="kuhni-sever.ru", **kw):
        return (await service.upsert_company(db, 7, service.FindingIn(source="yandex_direct", domain=domain, **kw),
                                             now=NOW)).company


class ApplyTests(DbBase):
    def test_enrich_writes_contacts_inn_signals_and_score(self):
        async def fn(db):
            c = await self.company(db)
            summary = await site_enrich.enrich_company(db, c, transport=site(SITE), delay=0, now=NOW)
            contacts = {(x.kind, x.value_norm, x.is_personal) for x in
                        (await db.execute(select(LgContact).where(LgContact.company_id == c.id))).scalars()}
            kinds = {k for (k,) in (await db.execute(select(LgSignal.kind).where(LgSignal.company_id == c.id))).all()}
            step = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == c.id))
            return c, summary, contacts, kinds, step
        c, summary, contacts, kinds, step = self.run_db(fn)
        self.assertEqual(summary["inn_status"], "set")
        self.assertEqual((c.inn, c.ogrn, c.stage), (INN, "1027700132195", "enriched"))
        self.assertIn(("whatsapp", "+79120001122", False), contacts)
        self.assertIn(("telegram", "kuhni_sever", False), contacts)
        self.assertIn(("email", "info@kuhni-sever.ru", False), contacts)
        self.assertIn(("email", "ivan.petrov@kuhni-sever.ru", True), contacts)
        self.assertEqual(kinds, {"has_messenger", "site_quiz", "no_crm"})
        self.assertEqual(c.score, 4)  # мессенджер 2 + квиз 1 + нет CRM 1
        self.assertEqual(step.status, "done")

    def test_repeat_does_not_duplicate_contacts(self):
        async def fn(db):
            c = await self.company(db)
            count = lambda: db.scalar(select(func.count(LgContact.id)).where(LgContact.company_id == c.id))
            await site_enrich.enrich_company(db, c, transport=site(SITE), delay=0, now=NOW)
            first = await count()
            await site_enrich.enrich_company(db, c, transport=site(SITE), delay=0, now=NOW)
            return first, await count()
        first, second = self.run_db(fn)
        self.assertEqual((first, second), (7, 7))  # 2 телефона, WhatsApp, Telegram, 3 почты

    def test_inn_owned_by_other_company_goes_to_review(self):
        async def fn(db):
            other = await self.company(db, domain="other.ru", inn=INN)
            c = await self.company(db)
            summary = await site_enrich.enrich_company(db, c, transport=site(SITE), delay=0, now=NOW)
            return other, c, summary
        other, c, summary = self.run_db(fn)
        self.assertEqual(summary["inn_status"], "duplicate")
        self.assertIsNone(c.inn)
        self.assertEqual(c.needs_review, f"inn_duplicate:{other.id}")

    def test_failed_site_marked_and_no_signals(self):
        async def fn(db):
            c = await self.company(db, domain="dead.ru")
            await site_enrich.enrich_company(db, c, transport=site({}), delay=0, now=NOW)
            step = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == c.id))
            signals = await db.scalar(select(func.count(LgSignal.id)).where(LgSignal.company_id == c.id))
            return step, signals, c.stage
        step, signals, stage = self.run_db(fn)
        self.assertEqual((step.status, signals, stage), ("failed", 0, "new"))

    def test_crm_on_site_means_no_signal(self):
        async def fn(db):
            c = await self.company(db, domain="x.ru")
            await site_enrich.enrich_company(db, c, transport=site({"/": (200, AMO_PAGE)}), delay=0, now=NOW)
            return {k for (k,) in (await db.execute(select(LgSignal.kind).where(LgSignal.company_id == c.id))).all()}
        self.assertNotIn("no_crm", self.run_db(fn))


class EnrichApiTests(DbBase):
    def setUp(self):
        super().setUp()

        async def database():
            async with self.sessions() as db:
                yield db
        self._old = (settings.agency_workspace_id, routes.session_factory, routes.site_transport, routes.site_delay)
        settings.agency_workspace_id = 7
        routes.session_factory, routes.site_transport, routes.site_delay = self.sessions, site(SITE), 0
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        settings.agency_workspace_id, routes.session_factory, routes.site_transport, routes.site_delay = self._old
        super().tearDown()

    def test_enrich_endpoint(self):
        async def fn(db):
            a = await self.company(db)
            b = await self.company(db, domain=None, name="Без сайта", phones=["89120009999"])
            return a.id, b.id
        a, b = self.run_db(fn)
        r = self.client.post("/api/admin/leadgen/companies/enrich", headers={"Origin": "http://localhost:3000"},
                             json={"company_ids": [a, b, 999]})
        self.assertEqual(r.json(), {"queued": 1, "skipped": 2})
        card = self.client.get(f"/api/admin/leadgen/companies/{a}").json()
        self.assertEqual(card["inn"], INN)
        self.assertIn("whatsapp", {c["kind"] for c in card["contacts"]})
        self.assertEqual(self.client.post("/api/admin/leadgen/companies/enrich", headers={"Origin": "http://localhost:3000"},
                                          json={}).status_code, 422)


    def test_search_run_auto_enriches_new_companies(self):
        from app.domains.leadgen.core.serp import SerpAd
        from app.domains.leadgen.providers import StaticProvider
        old = routes.provider_factory
        ad = SerpAd(keyword="", url="https://kuhni-sever.ru/", title="Кухни", text="Замер", premium=True)
        routes.provider_factory = lambda: StaticProvider({"кухни": [ad]})
        try:
            r = self.client.post("/api/admin/leadgen/runs", headers={"Origin": "http://localhost:3000"},
                                 json={"keywords": ["кухни"], "region_code": 54})
            self.assertEqual(r.status_code, 202, r.text)
            run = self.client.get(f"/api/admin/leadgen/runs/{r.json()['id']}").json()
            company = self.client.get(f"/api/admin/leadgen/companies/{run['companies'][0]['id']}").json()
        finally:
            routes.provider_factory = old
        self.assertEqual((company["inn"], company["stage"]), (INN, "enriched"))
        self.assertIn("telegram", {c["kind"] for c in company["contacts"]})
        self.assertEqual(company["score"], 8)  # реклама: премиум 2 + новый 2; сайт: мессенджер 2 + квиз 1 + нет CRM 1

if __name__ == "__main__":
    unittest.main()
