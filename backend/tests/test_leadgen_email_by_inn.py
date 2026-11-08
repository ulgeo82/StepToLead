"""«Почта по ИНН»: DaData (тариф с почтами) → Checko, только общие ящики, сайт из реестра, экономия запросов.
Без сети, ключи — фиктивные."""
import asyncio
import json
import unittest

import httpx

from app.domains.leadgen import routes
from app.domains.leadgen.sources import CompanyView, Context
from app.domains.leadgen.sources.email_by_inn import ADAPTER, contacts_from

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


if __name__ == "__main__":
    unittest.main()
