"""Four enrichers + a single-run chain. All HTTP and DNS are local fixtures/mocks."""
import asyncio
import copy
import json
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dns.exception
import dns.resolver
import httpx
from sqlalchemy import select

from app.domains.leadgen import service, source_runner, source_settings
from app.domains.leadgen.core.names import clean_company_name
from app.domains.leadgen.core.serp import SerpOrganic
from app.domains.leadgen.models import LgCompany, LgContact, LgEnrichment, LgSourceRun
from app.domains.leadgen.providers import SerpPage
from app.domains.leadgen.sources import Candidate, CompanySource, CompanyView, Context, REGISTRY
from app.domains.leadgen.sources import email_check, find_site, name_clean
from app.services import progress

import test_leadgen_sources as base

FIX = Path(__file__).parent / "fixtures" / "leadgen_sources"
HTML = (FIX / "step3_site.html").read_text()
PARTY = json.loads((FIX / "step3_party.json").read_text())
INN = "6315000005"
CHAIN = ["find_site", "site_contacts", "email_check", "name_clean"]


class Resolver:
    def resolve(self, domain, kind):
        if domain == "timeout.test":
            raise dns.exception.Timeout()
        if domain == "absent.test":
            raise dns.resolver.NXDOMAIN()
        if domain == "nomail.test":
            raise dns.resolver.NoAnswer()
        if domain == "a-only.test" and kind == "MX":
            raise dns.resolver.NoAnswer()
        return [SimpleNamespace(exchange="." if domain == "null-mx.test" else "mx.test.")]


def context(*, html=HTML, robots=False, party=PARTY, log=None):
    def handler(request):
        if log is not None:
            log.append(str(request.url))
        if request.url.host == "suggestions.dadata.ru":
            return httpx.Response(200, json=party)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /" if robots else "User-agent: *\nAllow: /")
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})
    ctx = Context(transport=httpx.MockTransport(handler), sleep=AsyncMock())
    ctx.cache["crawl_delay"] = 0
    return ctx


class Provider:
    def __init__(self):
        self.calls = []

    async def search_page(self, query, region, want_ads=True):
        self.calls.append((query, region))
        return SerpPage(organic=[SerpOrganic(query, "https://rusprofile.ru/1"),
                                  SerpOrganic(query, "https://hh.ru/employer/1"),
                                  SerpOrganic(query, "https://brand.test/")])


class NamesTests(unittest.TestCase):
    def test_25_saved_name_examples(self):
        for raw, expected, note in json.loads((FIX / "step3_names.json").read_text()):
            with self.subTest(raw=raw):
                self.assertEqual(clean_company_name(raw), (expected, note))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_site_needs_exact_inn_and_skips_directories(self):
        provider, log = Provider(), []
        view = CompanyView(1, name="ООО КУХНИ ВОЛГИ", inn=INN)
        with patch.object(find_site, "provider_factory", return_value=provider):
            found = await find_site.ADAPTER.enrich(view, {}, context(log=log))
            self.assertEqual(found.fields, {"domain": "brand.test"})
            self.assertNotIn("rusprofile", " ".join(log))
            self.assertNotIn("hh.ru", " ".join(log))
            self.assertEqual(provider.calls[0][1], 225)
            bad = await find_site.ADAPTER.enrich(view, {}, context(html=HTML.replace(INN, "6315000012")))
            self.assertEqual(bad.status, "skipped")
            self.assertNotIn("domain", bad.fields)
            blocked = await find_site.ADAPTER.enrich(view, {}, context(robots=True))
            self.assertNotIn("domain", blocked.fields)

    async def test_inn_from_existing_site_checksum_and_multiple(self):
        view = CompanyView(1, name="Кухни", domain="brand.test")
        good = await find_site.ADAPTER.enrich(view, {}, context())
        self.assertEqual(good.fields, {"inn": INN})
        for html in (HTML.replace(INN, "1234567890"), HTML + "<p>ИНН 6315000012</p>"):
            self.assertEqual((await find_site.ADAPTER.enrich(view, {}, context(html=html))).status, "skipped")

    async def test_dadata_only_unique_active_same_name_and_city(self):
        provider = Provider()
        view = CompanyView(1, name="ООО КУХНИ ВОЛГИ", city="Тестоград")
        with patch.object(find_site.dadata, "DadataProvider", return_value=SimpleNamespace(token="fixture")), \
                patch.object(find_site, "provider_factory", return_value=provider):
            good = await find_site.ADAPTER.enrich(view, {}, context())
            self.assertEqual(good.fields, {"inn": INN, "domain": "brand.test"})
            for field, value in (("state", {"status": "LIQUIDATED"}),
                                 ("address", {"data": {"city": "Другой город"}}),
                                 ("name", {"short": "Другое название"})):
                sample = copy.deepcopy(PARTY)
                sample["suggestions"][0]["data"][field] = value
                self.assertEqual((await find_site.ADAPTER.enrich(view, {}, context(party=sample))).status, "skipped")
            sample = copy.deepcopy(PARTY)
            second = copy.deepcopy(sample["suggestions"][0])
            second["data"]["inn"] = "6315000012"
            sample["suggestions"].append(second)
            self.assertEqual((await find_site.ADAPTER.enrich(view, {}, context(party=sample))).status, "skipped")
        with patch.object(find_site.dadata, "DadataProvider", side_effect=find_site.dadata.DadataNotConfigured()):
            self.assertEqual((await find_site.ADAPTER.enrich(view, {}, context())).error, "DaData не настроена")

    async def test_email_no_smtp_dns_fallback_typo_disposable_and_transient(self):
        cases = {"info@brand.test": "valid", "info@a-only.test": "valid", "info@absent.test": "invalid",
                 "info@nomail.test": "invalid", "info@null-mx.test": "invalid", "a..b@brand.test": "invalid",
                 "info@mailinator.com": "invalid", "info@gmail.com": "risky", "info@gmial.com": "risky",
                 "info@timeout.test": "risky", "info@пример.рф": "valid"}
        with patch.object(email_check, "resolver_factory", Resolver):
            ctx = context()
            for email, status in cases.items():
                with self.subTest(email=email):
                    self.assertEqual((await email_check.check_email(email, ctx))[0], status)
            _, payload = await email_check.check_email("info@gmial.com", ctx)
            self.assertEqual(payload["suggestion"], "info@gmail.com")

    async def test_email_30_days_force_and_personal_skip(self):
        now = service.utcnow()
        contact = {"id": 1, "kind": "email", "value": "info@brand.test", "verified_at": now}
        view = CompanyView(1, contacts=[contact])
        with patch.object(email_check, "resolver_factory", Resolver):
            self.assertEqual((await email_check.ADAPTER.enrich(view, {}, context())).status, "skipped")
            self.assertEqual((await email_check.ADAPTER.enrich(view, {"force": True}, context())).counters["valid"], 1)
            contact["verified_at"] = now - timedelta(days=31)
            self.assertEqual((await email_check.ADAPTER.enrich(view, {}, context())).counters["valid"], 1)
            contact["is_personal"] = True
            self.assertEqual((await email_check.ADAPTER.enrich(view, {"force": True}, context())).status, "skipped")

    async def test_names_manual_and_sole_proprietor_brand(self):
        manual = CompanyView(1, name="ООО КУХНИ ВОЛГИ", provenance={"display_name": {"source": "manual"}})
        self.assertEqual((await name_clean.ADAPTER.enrich(manual, {}, context())).status, "skipped")
        ip = CompanyView(1, name="ИП Иванов Иван Иванович")
        self.assertEqual((await name_clean.ADAPTER.enrich(ip, {}, context())).review, "name_clean:ip")
        ip.domain = "brand.test"
        branded = await name_clean.ADAPTER.enrich(ip, {}, context())
        self.assertEqual(branded.profile["name"], "Кухни Волги")
        blocked = await name_clean.ADAPTER.enrich(ip, {}, context(robots=True))
        self.assertEqual(blocked.review, "name_clean:ip")
        personal = await name_clean.ADAPTER.enrich(ip, {}, context(html="<title>Иванов Иван Иванович</title>"))
        self.assertEqual(personal.review, "name_clean:ip")


class DbTests(base.DbBase):
    def setUp(self):
        super().setUp()
        self.dns = patch.object(email_check, "resolver_factory", Resolver)
        self.dns.start()
        self.sleep = source_settings.sleep
        source_settings.sleep = AsyncMock()
        source_settings.transport = context().transport

    def tearDown(self):
        source_settings.sleep = self.sleep
        self.dns.stop()
        super().tearDown()

    def company(self, name="ООО КУХНИ ВОЛГИ", domain=None, inn=None, manual=False):
        async def add(db):
            c = LgCompany(workspace_id=7, display_name=name, domain=domain, inn=inn,
                          provenance={"display_name": {"source": "manual"}} if manual else {})
            db.add(c)
            await db.flush()
            return c.id
        return self.run_db(add)

    def run_source(self, code, ids):
        async def create(db):
            return (await source_runner.create_run(db, 7, code, query=None, target={"company_ids": ids},
                                                   list_name=None, admin_id=1)).id
        rid = self.run_db(create)
        asyncio.run(source_runner.execute(rid, self.sessions))
        return self.run_db(lambda db: db.get(LgSourceRun, rid))

    def test_duplicate_identity_is_review_not_overwrite(self):
        owner = self.company(domain="other.test", inn=INN)
        candidate = self.company(domain="brand.test")
        self.run_source("find_site", [candidate])
        c = self.run_db(lambda db: db.get(LgCompany, candidate))
        self.assertIsNone(c.inn)
        self.assertEqual(c.needs_review, "find_site:duplicate")
        self.assertEqual(self.run_db(lambda db: db.get(LgCompany, owner)).inn, INN)

    def test_duplicate_domain_and_manual_identity_provenance(self):
        owner = self.company(domain="brand.test")
        candidate = self.company(inn=INN)
        with patch.object(find_site, "provider_factory", return_value=Provider()):
            self.run_source("find_site", [candidate])
        c = self.run_db(lambda db: db.get(LgCompany, candidate))
        self.assertIsNone(c.domain)
        self.assertEqual(c.needs_review, "find_site:duplicate")
        self.assertEqual(self.run_db(lambda db: db.get(LgCompany, owner)).domain, "brand.test")

    def test_empty_chain_and_disabled_step_do_not_lose_finder_results(self):
        class Fake(CompanySource):
            code, kind, title = "fake_finder", "finder", "Тестовый поиск"
            async def fetch(self, query, settings, ctx):
                return [Candidate(service.FindingIn(source="import", domain="brand.test", name="КУХНИ ВОЛГИ"))]
        REGISTRY["fake_finder"] = Fake()
        async def create(db):
            rows = await source_settings.ensure(db)
            rows["email_check"].enabled = False
            return (await source_runner.create_run(db, 7, "fake_finder", query={}, target=None,
                list_name=None, admin_id=1, then=["email_check", "name_clean"])).id
        rid = self.run_db(create)
        asyncio.run(source_runner.execute(rid, self.sessions))
        run = self.run_db(lambda db: db.get(LgSourceRun, rid))
        self.assertEqual(run.status, "done")
        self.assertEqual(run.stats["result"]["found"], 1)
        self.assertEqual(run.stats["result"]["chain"]["email_check"]["skipped"], 1)
        self.assertEqual(run.stats["result"]["chain"]["name_clean"]["done"], 1)

    def test_contact_wrapper_then_check_and_clean_keep_raw_and_bounce(self):
        cid = self.company(domain="brand.test", inn=INN)
        self.run_source("site_contacts", [cid])
        async def contacts(db):
            return list((await db.scalars(select(LgContact).where(LgContact.company_id == cid))).all())
        found = self.run_db(contacts)
        self.assertEqual([c.value for c in found], ["info@brand.test"])
        async def profiles(db):
            return list((await db.scalars(select(LgEnrichment).where(LgEnrichment.company_id == cid))).all())
        for profile in self.run_db(profiles):
            self.assertNotIn("text_excerpt", profile.result)
            self.assertNotIn("ivan.petrov", str(profile.result))
        async def bounce(db):
            c = await db.get(LgContact, found[0].id)
            c.bounced = True
        self.run_db(bounce)
        run = self.run_source("email_check", [cid])
        self.assertEqual(run.stats["result"]["valid"], 1)
        checked = self.run_db(contacts)[0]
        self.assertTrue(checked.bounced)
        self.assertEqual(checked.verify_status, "valid")
        self.assertIsNotNone(checked.verified_at)
        self.run_source("name_clean", [cid])
        c = self.run_db(lambda db: db.get(LgCompany, cid))
        self.assertEqual((c.display_name, c.legal_name), ("Кухни Волги", "ООО КУХНИ ВОЛГИ"))
        manual = self.company(manual=True)
        self.run_source("name_clean", [manual])
        self.assertEqual(self.run_db(lambda db: db.get(LgCompany, manual)).display_name, "ООО КУХНИ ВОЛГИ")

    def test_finder_four_steps_single_run_progress_and_ids(self):
        class Fake(CompanySource):
            code, kind, title = "fake_finder", "finder", "Тестовый поиск"
            async def fetch(self, query, settings, ctx):
                return [Candidate(service.FindingIn(source="import", name="КУХНИ ВОЛГИ", inn=INN))]
        REGISTRY["fake_finder"] = Fake()
        async def create(db):
            return (await source_runner.create_run(db, 7, "fake_finder", query={}, target=None,
                list_name=None, admin_id=1, then=CHAIN)).id
        provider = Provider()
        rid = self.run_db(create)
        with patch.object(find_site, "provider_factory", return_value=provider):
            asyncio.run(source_runner.execute(rid, self.sessions))
        run = self.run_db(lambda db: db.get(LgSourceRun, rid))
        self.assertEqual(run.status, "done", run.error)
        self.assertEqual(list(run.stats["result"]["chain"]), CHAIN)
        self.assertEqual(run.stats["result"]["chain"]["email_check"]["valid"], 1)
        self.assertEqual(run.stats["result"]["chain"]["find_site"]["site_found"], 1)
        self.assertEqual(len(run.stats["company_ids"]), 1)
        c = self.run_db(lambda db: db.get(LgCompany, run.stats["company_ids"][0]))
        self.assertEqual((c.domain, c.display_name), ("brand.test", "Кухни Волги"))
        snapshot = progress.get(source_runner.run_key(rid))
        self.assertEqual(snapshot["state"], "done")
        self.assertTrue(all(s["state"] == "done" for s in snapshot["steps"]))
        self.assertTrue(set(CHAIN).issubset({s["code"] for s in snapshot["steps"]}))


class ApiTests(DbTests):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.router import api_router
        from app.core.access import COOKIE
        from app.db import get_db
        app = FastAPI()
        app.include_router(api_router, prefix="/api")
        async def override():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.client = TestClient(app, headers={"Origin": "http://localhost:3000"})
        self.client.cookies.set(COOKIE, "a" * 43)

    def test_api_run_chain_and_company_statuses(self):
        class Fake(CompanySource):
            code, kind, title = "fake_finder", "finder", "Тестовый поиск"
            async def fetch(self, query, settings, ctx):
                return [Candidate(service.FindingIn(source="import", name="КУХНИ ВОЛГИ", inn=INN))]
        REGISTRY["fake_finder"] = Fake()
        with patch.object(find_site, "provider_factory", return_value=Provider()):
            response = self.client.post("/api/admin/leadgen/sources/fake_finder/run", json={"query": {}, "then": CHAIN})
        self.assertEqual(response.status_code, 202, response.text)
        run = self.client.get(f"/api/admin/leadgen/runs/{response.json()['run_id']}").json()
        self.assertEqual(run["status"], "done", run.get("error"))
        self.assertEqual(list(run["result"]["chain"]), CHAIN)
        self.assertIsNotNone(run["list_id"])
        card = self.client.get(f"/api/admin/leadgen/companies/{run['companies'][0]['id']}").json()
        self.assertEqual((card["display_name"], card["legal_name"]), ("Кухни Волги", "КУХНИ ВОЛГИ"))
        self.assertEqual(card["contacts"][0]["verify_status"], "valid")
        self.assertIsNotNone(card["contacts"][0]["verified_at"])

    def test_api_chain_validation_estimate_and_verified_email_filter(self):
        sources = self.client.get("/api/admin/leadgen/sources").json()["items"]
        self.assertEqual([s["code"] for s in sources if s["chain_default"]], CHAIN)
        cid = self.company(domain="brand.test")
        for then in (["missing"], ["serp"], ["email_check", "email_check"]):
            r = self.client.post("/api/admin/leadgen/sources/serp/estimate", json={"query": {"queries": ["тест"]}, "then": then})
            self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/admin/leadgen/sources/name_clean/estimate",
                             json={"target": {"company_ids": [cid]}, "then": []})
        self.assertEqual(r.status_code, 422)
        plain = self.client.post("/api/admin/leadgen/sources/serp/estimate", json={"query": {"queries": ["тест"]}}).json()
        chained = self.client.post("/api/admin/leadgen/sources/serp/estimate",
            json={"query": {"queries": ["тест"]}, "then": CHAIN}).json()
        self.assertGreater(chained["seconds"], plain["seconds"])
        self.assertGreater(chained["cost"]["requests"], plain["cost"]["requests"])
        self.assertEqual(self.client.get("/api/admin/leadgen/companies?verify_status=valid").json()["total"], 0)
        async def email(db):
            db.add(LgContact(company_id=cid, kind="email", value="info@brand.test", value_norm="info@brand.test",
                             verify_status="valid", is_personal=False, bounced=False))
        self.run_db(email)
        rows = self.client.get("/api/admin/leadgen/companies?verify_status=valid").json()
        self.assertEqual([c["id"] for c in rows["items"]], [cid])
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/admin/leadgen/sources").status_code, 401)
