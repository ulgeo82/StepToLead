"""«Почта по ИНН»: DaData (тариф с почтами) → Checko, только общие ящики, сайт из реестра, экономия запросов.
Без сети, ключи — фиктивные."""
import asyncio
import json
import unittest
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.domains.leadgen import routes, service, source_runner, source_settings
from app.domains.leadgen.models import LgCompany, LgCompanyKey, LgContact, LgEnrichment
from app.domains.leadgen.sources import CompanyView, Context
from app.domains.leadgen.sources.email_by_inn import ADAPTER, contacts_from
from test_leadgen_sources import DbBase

INN = "6312000000"
CHECKO = {"data": {"ИНН": INN, "НаимСокр": "ООО \"КУХНИ\"",
                   "Контакты": {"Тел": ["+78460000000"], "Емэйл": ["info@kuhni.example", "ivan.petrov@kuhni.example"],
                               "ВебСайт": "https://www.kuhni.example/"}},
          "meta": {"status": "ok", "today_request_count": 3, "balance": 99.5}}
DADATA_EMPTY = {"suggestions": [{"value": "ООО КУХНИ", "data": {"inn": INN, "emails": None}}]}
DADATA_MAIL = {"suggestions": [{"value": "ООО КУХНИ", "data": {"inn": INN, "emails": [
    {"value": "zakaz@kuhni.example", "data": {"local": "zakaz", "domain": "kuhni.example"}}]}}]}


class FakeDadata:
    token = "dadata-test"


async def noop(*_):
    return None


class EmailByInnTests(unittest.TestCase):
    def setUp(self):
        self._old = routes.dadata_factory
        routes.dadata_factory = FakeDadata
        self.log = []

    def tearDown(self):
        routes.dadata_factory = self._old

    def ctx(self, dadata=DADATA_EMPTY, checko=CHECKO, checko_status=200):
        def handler(request):
            self.log.append(request.url.host)
            if "dadata" in request.url.host:
                return httpx.Response(200, json=dadata)
            return httpx.Response(checko_status, json=checko)
        return Context(transport=httpx.MockTransport(handler), sleep=noop)

    def run_enrich(self, view, settings, ctx):
        return asyncio.run(ADAPTER.enrich(view, settings, ctx))

    def test_parse_registry_answer(self):
        emails, sites = contacts_from(CHECKO["data"])
        self.assertEqual(emails, ["info@kuhni.example", "ivan.petrov@kuhni.example"])
        self.assertEqual(sites, ["kuhni.example"])

    def test_dadata_without_mail_then_checko(self):
        enr = self.run_enrich(CompanyView(id=1, inn=INN), {"checko_key": "k"}, self.ctx())
        self.assertEqual(enr.status, "done")
        self.assertEqual(enr.contacts, [{"kind": "email", "value": "info@kuhni.example"}])   # именной — не берём
        self.assertEqual(enr.counters["personal_dropped"], 1)
        self.assertEqual(enr.fields, {"domain": "kuhni.example"})
        self.assertEqual(enr.profile["source"], "checko")
        self.assertEqual(self.log, ["suggestions.dadata.ru", "api.checko.ru"])

    def test_dadata_with_mail_saves_checko_request(self):
        enr = self.run_enrich(CompanyView(id=1, inn=INN, domain="kuhni.example"), {"checko_key": "k"},
                              self.ctx(dadata=DADATA_MAIL))
        self.assertEqual(enr.contacts, [{"kind": "email", "value": "zakaz@kuhni.example"}])
        self.assertEqual(enr.fields, {})                         # сайт уже есть — не трогаем
        self.assertEqual(self.log, ["suggestions.dadata.ru"])    # Checko не спрашивали

    def test_skips_without_spending(self):
        has_mail = CompanyView(id=1, inn=INN, contacts=[{"kind": "email", "value": "info@x.example", "is_personal": False}])
        self.assertEqual(self.run_enrich(has_mail, {"checko_key": "k"}, self.ctx()).error, "почта уже есть")
        self.assertEqual(self.run_enrich(CompanyView(id=2), {"checko_key": "k"}, self.ctx()).error, "нет ИНН")
        self.assertEqual(self.log, [])

    def test_bad_key_and_nothing_found(self):
        enr = self.run_enrich(CompanyView(id=1, inn=INN), {"checko_key": "bad", "use_dadata": False},
                              self.ctx(checko_status=403))
        self.assertEqual((enr.status, enr.error), ("failed", "Checko не принял ключ"))
        empty = {"data": {"Контакты": {"Емэйл": [], "ВебСайт": None}}, "meta": {"status": "ok"}}
        enr = self.run_enrich(CompanyView(id=1, inn=INN, domain="kuhni.example"), {"checko_key": "k"},
                              self.ctx(checko=empty))
        self.assertEqual((enr.status, enr.error), ("skipped", "в реестрах почты нет"))

    def test_ready_and_check(self):
        routes.dadata_factory = self._old
        import os
        old = os.environ.pop("DADATA_TOKEN", None)
        try:
            self.assertEqual(ADAPTER.ready({}), "Нужен ключ Checko или DaData в портале")
            self.assertIsNone(ADAPTER.ready({"checko_key": "k"}))
            ok, msg = asyncio.run(ADAPTER.healthcheck({"checko_key": "k"}, self.ctx()))
            self.assertTrue(ok)
            self.assertIn("баланс 99.5", msg)
        finally:
            if old is not None:
                os.environ["DADATA_TOKEN"] = old


class RegistryPipelineTests(DbBase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self._dadata = routes.dadata_factory
        routes.dadata_factory = lambda: FakeDadata()
        def handler(request):
            self.requests.append(request.url.host)
            return httpx.Response(200, json={"suggestions": []})
        source_settings.transport = httpx.MockTransport(handler)

    def tearDown(self):
        routes.dadata_factory = self._dadata
        super().tearDown()

    def seed(self, status="skipped", age=1, tried=("dadata",)):
        async def fn(db):
            company = LgCompany(workspace_id=7, display_name="Компания")
            db.add(company)
            await db.flush()
            company.inn = INN + str(company.id)
            db.add(LgEnrichment(company_id=company.id, step="email_by_inn", status=status,
                                ran_at=service.utcnow() - timedelta(days=age), result={"tried": list(tried)}))
            return company.id
        return self.run_db(fn)

    def launch(self, cid, recheck=False):
        async def fn(db):
            return (await source_runner.create_run(db, 7, "email_by_inn", query=None,
                    target={"company_ids": [cid]}, list_name=None, admin_id=1, recheck=recheck)).id
        run_id = self.run_db(fn)
        asyncio.run(source_runner.execute(run_id, self.sessions))
        from app.domains.leadgen.models import LgSourceRun
        return self.run_db(lambda db: db.get(LgSourceRun, run_id))

    def test_recent_done_and_skipped_do_not_call_provider(self):
        for status in ("done", "skipped"):
            run = self.launch(self.seed(status))
            self.assertEqual(run.stats["result"]["reasons"], {"проверяли недавно": 1})
        self.assertEqual(self.requests, [])

    def test_recheck_and_expired_do_call_provider(self):
        self.launch(self.seed(), recheck=True)
        self.launch(self.seed(age=91))
        self.assertEqual(len(self.requests), 2)

    def test_missing_inn_skip_does_not_cache_registry_check(self):
        cid = self.seed(tried=())
        async def unset(db):
            (await db.get(LgCompany, cid)).inn = None
        self.run_db(unset)
        run = self.launch(cid)
        self.assertEqual(run.stats["result"]["reasons"], {"нет ИНН": 1})
        self.assertEqual(self.requests, [])
        async def set_inn(db):
            (await db.get(LgCompany, cid)).inn = INN
        self.run_db(set_inn)
        run = self.launch(cid)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("проверяли недавно", run.stats["result"]["reasons"])

    def test_invalid_and_bounced_emails_do_not_satisfy_only_missing(self):
        for status, bounced in (("invalid", False), ("valid", True)):
            cid = self.seed(tried=())
            async def add_email(db):
                db.add(LgContact(company_id=cid, kind="email", value="info@kuhni.example",
                                 value_norm="info@kuhni.example", verify_status=status, bounced=bounced,
                                 is_personal=False))
            self.run_db(add_email)
            run = self.launch(cid)
            self.assertNotIn("почта уже есть", run.stats["result"]["reasons"])
        self.assertEqual(len(self.requests), 2)

    def test_shared_valid_email_still_skips_provider_without_caching_check(self):
        cid = self.seed(tried=())
        async def add_email(db):
            db.add(LgContact(company_id=cid, kind="email", value="info@kuhni.example",
                             value_norm="info@kuhni.example", verify_status="valid", bounced=False))
        self.run_db(add_email)
        self.assertEqual(self.launch(cid).stats["result"]["reasons"], {"почта уже есть": 1})
        self.assertEqual(self.requests, [])

    def test_domain_conflict_preserves_email_and_status(self):
        for key_only in (False, True):
            async def fn(db):
                other = LgCompany(workspace_id=7, domain=None if key_only else "kuhni.example")
                company = LgCompany(workspace_id=7, inn=INN + str(key_only))
                db.add_all([other, company])
                await db.flush()
                if key_only:
                    db.add(LgCompanyKey(workspace_id=7, company_id=other.id, kind="domain",
                                        value_norm="kuhni.example"))
                    await db.flush()
                from app.domains.leadgen.sources.base import Enrichment
                enr = Enrichment(fields={"domain": "kuhni.example"},
                                 contacts=[{"kind": "email", "value": "info@kuhni.example"}])
                await source_runner.apply_enrichment(db, company, "email_by_inn", enr)
                emails = list((await db.scalars(select(LgContact.value).where(
                    LgContact.company_id == company.id))).all())
                return company.domain, company.needs_review, enr.status, emails
            self.assertEqual(self.run_db(fn), (None, None, "done", ["info@kuhni.example"]))

    def test_available_domain_uses_site_search_and_adds_identity_key(self):
        async def fn(db):
            company = LgCompany(workspace_id=7)
            db.add(company)
            await db.flush()
            from app.domains.leadgen.sources.base import Enrichment
            await source_runner.apply_enrichment(db, company, "email_by_inn",
                                                 Enrichment(fields={"domain": "kuhni.example"}))
            key = await db.scalar(select(LgCompanyKey).where(LgCompanyKey.company_id == company.id))
            return company.domain, company.provenance["domain"]["source"], key.kind, key.value_norm
        self.assertEqual(self.run_db(fn), ("kuhni.example", "site_search", "domain", "kuhni.example"))


if __name__ == "__main__":
    unittest.main()
