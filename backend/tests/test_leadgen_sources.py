"""Источники компаний: контракт, тех-профиль сайта (на сохранённых страницах, без сети), раннер, API."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.db import Base, get_db
from app.domains.leadgen import routes, service, source_runner, source_settings
from app.domains.leadgen.core.tech_profile import footer_year, profile
from app.domains.leadgen.models import LgEnrichment, LgList, LgListItem, LgSignal, LgSource, LgSourceRun
from app.domains.leadgen.sources import REGISTRY, Candidate, CompanySource, SignalIn
from app.domains.leadgen.sources.site_tech import ADAPTER as SITE_TECH
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace

FIX = Path(__file__).parent / "fixtures" / "leadgen_sources"
MODERN = (FIX / "tilda_metrika_calltouch.html").read_text(encoding="utf-8")
OLD = (FIX / "wordpress_old_bare.html").read_text(encoding="utf-8")
PAGESPEED = {"lighthouseResult": {"categories": {"performance": {"score": 0.31}}}}


def net(pages: dict, robots: str | None = None, log: list | None = None, pagespeed=PAGESPEED):
    """pages: домен -> html главной (или код ошибки)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        if request.url.host == "www.googleapis.com":
            assert request.url.params.get("key") == "test-key"
            return httpx.Response(200, json=pagespeed)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots else httpx.Response(404)
        body = pages.get(request.url.host)
        if body is None:
            return httpx.Response(404, text="nope")
        if isinstance(body, int):
            return httpx.Response(body, text="err")
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})
    return httpx.MockTransport(handler)


class ProfileTests(unittest.TestCase):
    def test_modern_tilda_site(self):
        p = profile(MODERN, 2026)
        self.assertEqual((p["platform"], p["platform_title"]), ("tilda", "Тильда"))
        self.assertTrue(p["has_metrika"])
        self.assertEqual(p["metrika_ids"], ["91234567"])
        self.assertEqual(p["ga_ids"], ["G-AB12CD34EF"])
        self.assertEqual(p["calltracking"], ["calltouch"])
        self.assertEqual(p["chats"], ["jivo"])
        self.assertTrue(p["has_quiz"] and p["has_privacy_policy"])
        self.assertEqual(p["footer_year"], 2025)   # «© 2018–2025» — берём последний

    def test_old_bare_wordpress(self):
        p = profile(OLD, 2026)
        self.assertEqual(p["platform"], "wordpress")
        self.assertFalse(p["has_metrika"] or p["has_ga"] or p["has_privacy_policy"])
        self.assertEqual((p["calltracking"], p["crm_forms"]), ([], ["amocrm"]))
        self.assertEqual(p["footer_year"], 2019)

    def test_footer_year_without_copyright_and_future_years(self):
        self.assertEqual(footer_year("<p>текст</p>" * 50 + "<footer>Самара, 2023</footer>", 2026), 2023)
        self.assertIsNone(footer_year("<footer>© 2099</footer>", 2026))


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
        self.old_ws = settings.agency_workspace_id
        settings.agency_workspace_id = 7
        self.old_factory = routes.session_factory
        routes.session_factory = self.sessions
        self.old_transport = source_settings.transport
        source_runner._running.clear()

    def tearDown(self):
        settings.agency_workspace_id = self.old_ws
        routes.session_factory = self.old_factory
        source_settings.transport = self.old_transport
        REGISTRY.pop("fake_finder", None)
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                result = await fn(db)
                await db.commit()
                return result
        return asyncio.run(go())

    def companies(self, *domains):
        async def fn(db):
            out = []
            for d in domains:
                c = (await service.upsert_company(db, 7, service.FindingIn(source="import", domain=d, name=d))).company
                out.append(c.id)
            return out
        return self.run_db(fn)

    def set_key(self, key="test-key"):
        async def fn(db):
            rows = await source_settings.ensure(db)
            source_settings.apply(SITE_TECH, rows["site_tech"], {"pagespeed_key": key})
        self.run_db(fn)

    def run_enrich(self, ids):
        async def create(db):
            return (await source_runner.create_run(db, 7, "site_tech", query=None, target={"company_ids": ids},
                                                   list_name=None, admin_id=1)).id
        run_id = self.run_db(create)
        asyncio.run(source_runner.execute(run_id, self.sessions))
        return run_id


class EnrichTests(DbBase):
    def test_signals_profile_and_score(self):
        log = []
        source_settings.transport = net({"modern.ru": MODERN, "old.ru": OLD}, log=log)
        self.set_key()
        modern, old, nosite = self.companies("modern.ru", "old.ru", None)
        run_id = self.run_enrich([modern, old, nosite])

        async def fn(db):
            sig = {}
            for s in (await db.scalars(select(LgSignal))).all():
                sig.setdefault(s.company_id, set()).add(s.kind)
            run = await db.get(LgSourceRun, run_id)
            return sig, run, await source_runner.tech_for(db, old), await source_runner.tech_for(db, modern)
        sig, run, tech_old, tech_modern = self.run_db(fn)
        self.assertEqual(sig[modern], {"has_calltracking", "slow_site"})
        self.assertEqual(sig[old], {"no_analytics", "no_calltracking", "outdated_site", "slow_site"})
        self.assertEqual(run.status, "done")
        r = run.stats["result"]
        self.assertEqual((r["found"], r["updated"], r["skipped"]), (3, 2, 1))
        self.assertEqual(r["signals"]["slow_site"], 2)
        self.assertEqual(tech_old["platform"], "wordpress")
        self.assertEqual((tech_old["analytics"], tech_old["pagespeed_mobile"], tech_old["footer_year"]), ([], 31, 2019))
        self.assertEqual(tech_modern["analytics"], ["yandex_metrika", "google_analytics"])
        self.assertTrue(all("test-key" not in u or "googleapis" in u for u in log))

    def test_without_key_no_pagespeed_and_recheck_replaces_signals(self):
        log = []
        source_settings.transport = net({"old.ru": OLD}, log=log)
        (old,) = self.companies("old.ru")
        self.run_enrich([old])
        self.assertFalse(any("googleapis" in u for u in log))
        source_settings.transport = net({"old.ru": MODERN})   # сайт обновили
        self.run_enrich([old])

        async def fn(db):
            kinds = sorted(k for (k,) in (await db.execute(select(LgSignal.kind))).all())
            return kinds, await source_runner.tech_for(db, old)
        kinds, tech = self.run_db(fn)
        self.assertEqual(kinds, ["has_calltracking"])
        self.assertIsNone(tech["pagespeed_mobile"])

    def test_robots_and_dead_site(self):
        source_settings.transport = net({"dead.ru": 503, "closed.ru": MODERN}, robots=None)
        dead, = self.companies("dead.ru")
        run_id = self.run_enrich([dead])

        async def fn(db):
            row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == dead))
            return row, await db.get(LgSourceRun, run_id)
        row, run = self.run_db(fn)
        self.assertEqual((row.status, row.error), ("failed", "сайт ответил ошибкой 503"))
        self.assertIn("не проверено 1", run.stats["result"]["errors"]["site_tech"])
        self.assertNotIn("dead.ru", run.stats["result"]["errors"]["site_tech"])

        log = []
        source_settings.transport = net({"closed.ru": MODERN}, robots="User-agent: *\nDisallow: /", log=log)
        closed, = self.companies("closed.ru")
        self.run_enrich([closed])
        self.assertEqual([u for u in log if not u.endswith("/robots.txt")], [])


class FakeFinder(CompanySource):
    code = "fake_finder"; kind = "finder"; title = "Тестовый поиск"
    query_schema = [{"key": "city", "label": "Город", "type": "region", "required": True}]
    signals_info = [{"kind": "new_company", "label": "x"}]

    async def fetch(self, query, settings, ctx):
        f = lambda d, inn: service.FindingIn(source="fns", domain=d, inn=inn, name=d, city=query["city"])
        return [Candidate(f("a.ru", None), [SignalIn("new_company", {"months": 3})]), Candidate(f("b.ru", None))]


class FinderTests(DbBase):
    def test_finder_creates_list_and_signals(self):
        REGISTRY["fake_finder"] = FakeFinder()
        self.companies("b.ru")

        async def create(db):
            return (await source_runner.create_run(db, 7, "fake_finder", query={"city": "Казань"}, target=None,
                                                   list_name="Тест", admin_id=1)).id
        run_id = self.run_db(create)
        asyncio.run(source_runner.execute(run_id, self.sessions))

        async def fn(db):
            run = await db.get(LgSourceRun, run_id)
            items = (await db.scalars(select(LgListItem).where(LgListItem.list_id == run.stats["list_id"]))).all()
            return run, len(items), await db.get(LgList, run.stats["list_id"])
        run, items, lst = self.run_db(fn)
        self.assertEqual((run.stats["result"]["found"], run.stats["result"]["new"]), (2, 1))
        self.assertEqual((items, lst.name), (2, "Тест"))

    def test_required_query_field(self):
        REGISTRY["fake_finder"] = FakeFinder()

        async def create(db):
            await source_runner.create_run(db, 7, "fake_finder", query={}, target=None, list_name=None, admin_id=1)
        with self.assertRaises(source_runner.RunError) as err:
            self.run_db(create)
        self.assertEqual(err.exception.status, 422)


class ApiTests(DbBase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(api_router, prefix="/api")

        async def override():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.client = TestClient(app, headers={"Origin": "http://localhost:3000"})
        self.client.cookies.set(COOKIE, "a" * 43)

    def test_sources_secret_never_returned(self):
        r = self.client.get("/api/admin/leadgen/sources")
        self.assertEqual(r.status_code, 200)
        item = next(i for i in r.json()["items"] if i["code"] == "site_tech")
        self.assertEqual((item["kind"], item["state"], item["ready"], item["query_schema"]), ("enricher", "never", True, []))
        self.assertIn({"kind": "no_analytics", "label": "Нет Метрики на сайте"}, item["signals"])
        r = self.client.patch("/api/admin/leadgen/sources/site_tech", json={"settings": {"pagespeed_key": "secret-123"}})
        self.assertEqual(r.json()["settings"]["pagespeed_key"], {"set": True})
        self.assertNotIn("secret-123", r.text)
        stored = self.run_db(lambda db: db.get(LgSource, "site_tech"))
        self.assertNotIn("secret-123", str(stored.settings))
        r = self.client.patch("/api/admin/leadgen/sources/site_tech", json={"settings": {"pagespeed_key": ""}})
        self.assertEqual(r.json()["settings"]["pagespeed_key"], {"set": False})
        self.assertEqual(self.client.patch("/api/admin/leadgen/sources/nope", json={}).status_code, 404)

    def test_estimate_run_progress_and_company_tech(self):
        source_settings.transport = net({"old.ru": OLD})
        old, nosite = self.companies("old.ru", None)
        r = self.client.post("/api/admin/leadgen/sources/site_tech/estimate", json={"target": {"company_ids": [old, nosite]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["companies"], r.json()["units"]), (2, 1))
        self.assertGreater(r.json()["seconds"], 0)
        self.assertEqual(self.client.post("/api/admin/leadgen/sources/site_tech/estimate",
                                          json={"target": {}}).status_code, 422)
        r = self.client.post("/api/admin/leadgen/sources/site_tech/run", json={"target": {"company_ids": [old]}})
        self.assertEqual(r.status_code, 202)
        run_id = r.json()["run_id"]
        p = self.client.get(f"/api/admin/leadgen/runs/{run_id}/progress").json()
        self.assertEqual(p["state"], "done")
        run = self.client.get(f"/api/admin/leadgen/runs/{run_id}").json()
        self.assertEqual((run["source"], run["list_id"], run["result"]["updated"]), ("site_tech", None, 1))
        company = self.client.get(f"/api/admin/leadgen/companies/{old}").json()
        self.assertEqual(company["tech"]["platform"], "wordpress")
        self.assertIn("no_analytics", {s["kind"] for s in company["signals"]})
        self.assertIsNone(self.client.get(f"/api/admin/leadgen/companies/{nosite}").json()["tech"])
        # дообогащение не создаёт мусорный список «Поиск · дата» при открытии списков
        names = [l["name"] for l in self.client.get("/api/admin/leadgen/lists").json()["items"]]
        self.assertFalse([n for n in names if n.startswith("Поиск") or "Тех-профиль" in n], names)

    def test_conflict_when_running(self):
        old, = self.companies("old.ru")
        source_runner._running.add("site_tech")
        r = self.client.post("/api/admin/leadgen/sources/site_tech/run", json={"target": {"company_ids": [old]}})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "Этот источник уже запущен"))

    def test_check_without_key(self):
        r = self.client.post("/api/admin/leadgen/sources/site_tech/check")
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(self.client.get("/api/admin/leadgen/sources").json()["items"][0]["state"], "ok")


if __name__ == "__main__":
    unittest.main()


GTM_PAGE = """<html><head><script>(function(w,d,s,l,i){w[l]=w[l]||[];var j=d.createElement(s);
j.src='https://www.googletagmanager.com/gtm.js?id='+i;})(window,document,'script','dataLayer','GTM-ABC123');</script>
</head><body><h1>Кухни на заказ</h1><footer>© 2026</footer></body></html>"""


class SiteTechEdgeTests(unittest.TestCase):
    """Ложные сигналы и мёртвые сайты — на них нельзя писать письма с неправдой и нельзя зависать."""

    def enrich(self, transport, domain="gtm.example"):
        from app.domains.leadgen.sources import CompanyView, Context

        async def noop(*_):
            return None
        ctx = Context(transport=transport, sleep=noop)
        return asyncio.run(SITE_TECH.enrich(CompanyView(id=1, domain=domain), {}, ctx))

    def test_tag_manager_no_negative_signals(self):
        enr = self.enrich(net({"gtm.example": GTM_PAGE}))
        kinds = {s.kind for s in enr.signals}
        self.assertEqual(enr.status, "done")
        self.assertTrue(enr.profile["has_gtm"])
        self.assertNotIn("no_analytics", kinds)        # Метрика могла прийти через GTM — не утверждаем «нет»
        self.assertNotIn("no_calltracking", kinds)
        self.assertEqual(enr.profile["has_metrika_text"], "")

    def test_dead_site_gives_up_fast_without_http_retry(self):
        log = []

        def handler(request):
            log.append(str(request.url))
            raise httpx.ConnectTimeout("timeout", request=request)
        enr = self.enrich(httpx.MockTransport(handler), domain="dead.example")
        self.assertEqual((enr.status, enr.error), ("failed", "сайт не ответил вовремя"))
        self.assertEqual(log, ["https://dead.example/robots.txt"])   # не ждём ещё html и http://
