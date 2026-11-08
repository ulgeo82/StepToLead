"""Рабочее место базы: таблица и фильтры колонок, совпадения, сводка, очистка (preview = apply), правка ячейки,
«на проверку», CSV, запуск функций по базе, новые источники, переменные писем. Без сети, ИИ подменяется."""
import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.domains.leadgen import service
from app.domains.leadgen.models import LgCompany, LgContact, LgEnrichment, LgList, LgListItem, LgSignal

from test_leadgen_sources_step2 import _Api

FIX = Path(__file__).parent / "fixtures" / "leadgen_sources"
VALID_INN = "6312000014"
OTHER_INN = "7734454184"


class WorkspaceBase(_Api):
    def seed(self):
        """Список из 5 компаний: почты разных статусов, дубль по телефону, «не подходит», сеть, скрытая."""
        async def fn(db):
            lst = LgList(workspace_id=7, name="Кухни", kind="manual")
            db.add(lst)
            await db.flush()
            made = []
            specs = [
                dict(name="Кухни Волги", domain="kuhni-volgi.ru", city="Самара", niche="Кухни"),
                dict(name="Мебель Стиль", domain="mebel-stil.ru", city="Самара", niche="Кухни"),
                dict(name="Кухни-Волги", domain="kuhni-volgi63.ru", city="Самара", niche="Кухни"),
                dict(name="Большая Сеть", domain="set.ru", city="Казань", niche="Мебель"),
                dict(name="Без почты", domain=None, city=None, niche=None),
            ]
            for s in specs:
                c = (await service.upsert_company(db, 7, service.FindingIn(source="import", **s))).company
                db.add(LgListItem(list_id=lst.id, company_id=c.id))
                made.append(c)
            a, b, dup, chain, bare = made
            db.add_all([
                LgContact(company_id=a.id, kind="email", value="info@kuhni-volgi.ru", value_norm="info@kuhni-volgi.ru",
                          verify_status="valid"),
                LgContact(company_id=a.id, kind="email", value="zakaz@kuhni-volgi.ru", value_norm="zakaz@kuhni-volgi.ru",
                          verify_status="invalid"),
                LgContact(company_id=a.id, kind="phone", value="+78462001020", value_norm="+78462001020"),
                LgContact(company_id=dup.id, kind="phone", value="+78462001020", value_norm="+78462001020"),
                LgContact(company_id=b.id, kind="email", value="info@mebel-stil.ru", value_norm="info@mebel-stil.ru",
                          verify_status="invalid"),
                LgContact(company_id=b.id, kind="telegram", value="mebelstil", value_norm="mebelstil"),
                LgSignal(company_id=chain.id, kind="chain", payload={}),
                LgSignal(company_id=b.id, kind="hiring", payload={}),
            ])
            b.fit_label = "no"
            chain.revenue_rub = 900_000_000
            await db.flush()
            return lst.id, [c.id for c in made]
        self.list_id, self.ids = self.run_db(fn)

    def get(self, url, **params):
        r = self.client.get(f"/api/admin/leadgen/lists/{self.list_id}{url}", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TableTests(WorkspaceBase):
    def test_rows_best_email_duplicates_and_columns(self):
        self.seed()
        t = self.get("/table", sort="display_name", dir="asc")
        self.assertEqual(t["total"], 5)
        by = {r["display_name"]: r for r in t["items"]}
        a = by["Кухни Волги"]
        self.assertEqual(a["best_email"], {"value": "info@kuhni-volgi.ru", "verify_status": "valid"})
        self.assertEqual((a["emails"], a["phone"]), (2, "+78462001020"))
        self.assertEqual(by["Мебель Стиль"]["messengers"], ["telegram"])
        self.assertEqual(by["Мебель Стиль"]["signals"], ["hiring"])
        self.assertEqual(a["duplicate_group"], by["Кухни-Волги"]["duplicate_group"])
        self.assertIsNotNone(a["duplicate_group"])
        self.assertIsNone(by["Мебель Стиль"]["duplicate_group"])
        self.assertIn({"key": "email_status", "label": "Статус почты", "filter": "values"}, t["columns"])
        self.assertEqual(t["items"][0]["display_name"], "Без почты")    # сортировка по имени
        self.assertIsNone(t["items"][0]["best_email"])

    def test_column_filters_search_sort_pages(self):
        self.seed()
        f = lambda **p: [r["display_name"] for r in self.get("/table", sort="display_name", dir="asc", **p)["items"]]
        self.assertEqual(f(**{"f.city": "contains:самар"}), ["Кухни Волги", "Кухни-Волги", "Мебель Стиль"])
        self.assertEqual(f(**{"f.domain": "empty"}), ["Без почты"])
        self.assertEqual(f(**{"f.email_status": "in:valid,risky"}), ["Кухни Волги"])
        self.assertEqual(f(**{"f.email_status": "in:none"}), ["Без почты", "Большая Сеть", "Кухни-Волги"])
        self.assertEqual(f(**{"f.signal": "in:hiring"}), ["Мебель Стиль"])
        self.assertEqual(f(**{"f.fit": "notempty"}), ["Мебель Стиль"])
        self.assertEqual(f(q="mebelstil"), [])                       # поиск по колонкам, не по скрытым полям
        self.assertEqual(f(q="КАЗАНЬ"), ["Большая Сеть"])
        page = self.get("/table", sort="display_name", dir="asc", per_page=2, page=3)
        self.assertEqual((page["total"], len(page["items"])), (5, 1))
        bad = self.client.get(f"/api/admin/leadgen/lists/{self.list_id}/table", params={"f.nope": "empty"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.get("/api/admin/leadgen/lists/999/table").status_code, 404)

    def test_summary(self):
        self.seed()
        s = self.get("/summary", column="city")
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["items"][0], {"value": "Самара", "count": 3})
        self.assertIn({"value": None, "count": 1}, s["items"])
        s = self.get("/summary", column="email_status", **{"f.city": "contains:самар"})
        self.assertEqual({i["value"]: i["count"] for i in s["items"]}, {"valid": 1, "invalid": 1, None: 1})


class CleanupTests(WorkspaceBase):
    def preview_apply(self, rule, **extra):
        url = f"/api/admin/leadgen/lists/{self.list_id}/cleanup"
        p = self.client.post(url + "/preview", json={"rule": rule, **extra}).json()
        a = self.client.post(url, json={"rule": rule, **extra}).json()
        return p, a

    def test_preview_equals_apply_and_hidden_leave_table(self):
        self.seed()
        p, a = self.preview_apply("no_email")
        self.assertEqual((p["count"], a["updated"]), (3, 3))
        self.assertEqual({r["display_name"] for r in p["sample"]}, {"Без почты", "Большая Сеть", "Кухни-Волги"})
        self.assertEqual(self.get("/table")["total"], 2)
        self.assertEqual(self.get("/table", **{"f.stage": "in:hidden"})["total"], 3)
        p, a = self.preview_apply("bad_email")
        self.assertEqual((p["count"], a["updated"]), (1, 1))     # Мебель Стиль: единственная почта невалидна

    def test_not_fit_chains_duplicates(self):
        self.seed()
        self.assertEqual(self.preview_apply("not_fit")[0]["count"], 1)
        p, _ = self.preview_apply("chains")
        self.assertEqual([r["display_name"] for r in p["sample"]], ["Большая Сеть"])
        p, a = self.preview_apply("duplicates")
        self.assertEqual((p["count"], a["updated"]), (2, 2))
        self.assertEqual(self.preview_apply("duplicates")[0]["count"], 0)   # уже на проверке
        rows = self.get("/table", **{"f.needs_review": "notempty"})["items"]
        self.assertEqual({r["needs_review"] for r in rows}, {"duplicate"})


class EditTests(WorkspaceBase):
    def patch(self, cid, **data):
        return self.client.patch(f"/api/admin/leadgen/companies/{cid}", json=data)

    def test_edit_inn_domain_conflicts_and_manual_kept(self):
        self.seed()
        a, b = self.ids[0], self.ids[1]
        self.assertEqual(self.patch(a, inn="6312000015").status_code, 422)
        r = self.patch(a, inn=VALID_INN, display_name="  Кухни Волги Премиум ", city="Тольятти")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["inn"], r.json()["display_name"], r.json()["city"]),
                         (VALID_INN, "Кухни Волги Премиум", "Тольятти"))
        r = self.patch(b, inn=VALID_INN)
        self.assertEqual((r.status_code, r.json()["company_id"]), (409, a))
        r = self.patch(b, domain="https://www.KUHNI-VOLGI.ru/contacts")
        self.assertEqual((r.status_code, r.json()["company_id"]), (409, a))
        self.assertEqual(self.patch(b, phone="1").status_code, 422)

        async def auto(db):   # автоматический источник (сайт) не перетирает ручное название
            c = await db.get(LgCompany, a)
            service._apply_fields(c, {"display_name": "ООО КУХНИ ВОЛГИ", "city": "Самара"}, "site", None,
                                  service.utcnow())
            return c.display_name, c.city, c.provenance["display_name"]["source"]
        self.assertEqual(self.run_db(auto), ("Кухни Волги Премиум", "Тольятти", "manual"))
        # новый ИНН стал ключом склейки: находка с тем же ИНН прилипает к этой компании
        found = self.run_db(lambda db: service.upsert_company(db, 7, service.FindingIn(source="hh", inn=VALID_INN,
                                                                                       name="x")))
        self.assertEqual(found.company.id, a)

    def test_review_and_clear(self):
        self.seed()
        url = "/api/admin/leadgen/companies/review"
        self.assertEqual(self.client.post(url, json={"company_ids": self.ids[:2], "reason": "сеть?"}).json(),
                         {"updated": 2})
        names = {r["needs_review"] for r in self.get("/table", **{"f.needs_review": "notempty"})["items"]}
        self.assertEqual(names, {"manual:сеть?"})
        self.assertEqual(self.client.post(url + "/clear", json={"company_ids": self.ids}).json(), {"updated": 2})


FNS_SEARCH = (FIX / "fns_search.json").read_bytes()
FNS_BFO = (FIX / "fns_bfo.json").read_bytes()


def fns_net(log=None, search=FNS_SEARCH):
    def handler(request):
        if log is not None:
            log.append(str(request.url))
        if request.url.path == "/advanced-search/organizations/search":
            return httpx.Response(200, content=search, headers={"content-type": "application/json"})
        if request.url.path == "/nbo/organizations/900001/bfo/":
            return httpx.Response(200, content=FNS_BFO, headers={"content-type": "application/json"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class FakeAI:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(self, system, messages, **kw):
        self.calls.append((system, messages, kw))
        return self.answer


class CsvTests(WorkspaceBase):
    def test_csv_bom_semicolon_and_formula_escape(self):
        self.seed()
        self.run_db(lambda db: _rename(db, self.ids[1], "=HYPERLINK(1)"))
        r = self.client.get("/api/admin/leadgen/companies/export", params={"list_id": self.list_id, "format": "csv"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0][0], "Компания")
        self.assertIn("'=HYPERLINK(1)", [row[0] for row in rows[1:]])
        self.assertEqual(len(rows), 6)
        self.assertEqual(self.client.get("/api/admin/leadgen/companies/export",
                                         params={"list_id": self.list_id, "format": "pdf"}).status_code, 422)


async def _rename(db, cid, name):
    (await db.get(LgCompany, cid)).display_name = name


class ListRunTests(WorkspaceBase):
    def setUp(self):
        super().setUp()
        from app.domains.leadgen import source_runner, source_settings
        from app.domains.leadgen.sources import personalize
        self.runner, self.settings, self.pers = source_runner, source_settings, personalize
        self.old = (source_settings.transport, personalize.complete)
        source_runner._running.clear()

    def tearDown(self):
        self.settings.transport, self.pers.complete = self.old
        super().tearDown()

    def test_order_is_fixed_estimate_and_conflict(self):
        self.seed()
        url = f"/api/admin/leadgen/lists/{self.list_id}"
        est = self.client.post(url + "/estimate", json={"steps": ["personalize", "fns_revenue", "name_clean"]}).json()
        self.assertEqual([s["code"] for s in est["steps"]], ["name_clean", "fns_revenue", "personalize"])
        self.assertEqual(est["companies"], 5)
        self.assertEqual(self.client.post(url + "/estimate", json={"steps": ["serp"]}).status_code, 422)
        self.assertEqual(self.client.post(url + "/estimate", json={"steps": ["name_clean"],
                                                                     "company_ids": [999]}).status_code, 422)
        self.runner._running.add(f"list:{self.list_id}")
        r = self.client.post(url + "/run", json={"steps": ["name_clean"]})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "По этой базе уже идёт запуск"))

    def test_run_selected_fns_and_personalize_chain_result(self):
        self.seed()
        self.run_db(lambda db: _set(db, self.ids[0], inn=VALID_INN))
        self.settings.transport = fns_net()
        self.pers.complete = FakeAI('{"line": "Видим, что на сайте нет коллтрекинга — звонки с рекламы не размечены."}')
        self.run_db(lambda db: _sig(db, self.ids[0], "no_calltracking"))
        url = f"/api/admin/leadgen/lists/{self.list_id}"
        r = self.client.post(url + "/run", json={"steps": ["personalize", "fns_revenue"],
                                                 "company_ids": [self.ids[0], self.ids[4]]})
        self.assertEqual(r.status_code, 202, r.text)
        run_id = r.json()["run_id"]
        run = self.client.get(f"/api/admin/leadgen/runs/{run_id}").json()
        self.assertEqual(run["status"], "done", run)
        chain = run["result"]["chain"]
        self.assertEqual(list(chain), ["fns_revenue", "personalize"])
        self.assertEqual((chain["fns_revenue"]["done"], chain["fns_revenue"]["skipped"]), (1, 1))
        self.assertEqual(chain["personalize"]["done"], 1)
        self.assertEqual(chain["personalize"]["reasons"], {"мало данных": 1})
        self.assertEqual(self.client.get(f"/api/admin/leadgen/runs/{run_id}/progress").json()["state"], "done")
        row = next(r for r in self.get("/table")["items"] if r["id"] == self.ids[0])
        self.assertEqual((row["revenue_rub"], row["signals"]), (96_500_000, ["no_calltracking", "revenue_fit"]))
        self.assertIn("коллтрекинга", row["personal_line"])
        prompt = self.pers.complete.calls[0][1][0]["content"]
        self.assertIn("<facts>", prompt)
        self.assertEqual(self.pers.complete.calls[0][2]["bucket"], "agency")


async def _set(db, cid, **kw):
    c = await db.get(LgCompany, cid)
    for k, v in kw.items():
        setattr(c, k, v)


async def _sig(db, cid, kind, **payload):
    db.add(LgSignal(company_id=cid, kind=kind, payload=payload))


class SourceUnitTests(WorkspaceBase):
    def enrich(self, code, cid, **ctx_kw):
        from app.domains.leadgen import source_runner, source_settings
        from app.domains.leadgen.sources import REGISTRY

        async def fn(db):
            run = await source_runner.create_list_run(db, 7, self.list_id, steps=[code], company_ids=[cid])
            return run.id
        run_id = self.run_db(fn)
        asyncio.run(source_runner.execute_list(run_id, self.sessions))
        return self.run_db(lambda db: db.get(__import__("app.domains.leadgen.models", fromlist=["x"]).LgSourceRun,
                                             run_id))

    def test_fns_big_company_and_ip_and_not_found(self):
        from app.domains.leadgen import source_settings, source_runner
        from app.domains.leadgen.sources import fns_revenue
        source_runner._running.clear()
        old = source_settings.transport
        try:
            self.seed()
            self.assertEqual(fns_revenue.pick_revenue(json.loads(FNS_BFO)), (96_500_000, 2024))
            self.run_db(lambda db: _set(db, self.ids[0], inn=VALID_INN))
            async def low_max(db):
                from app.domains.leadgen.sources import REGISTRY
                rows = await source_settings.ensure(db)
                source_settings.apply(REGISTRY["fns_revenue"], rows["fns_revenue"], {"revenue_max": 50_000_000})
            self.run_db(low_max)
            source_settings.transport = fns_net()
            run = self.enrich("fns_revenue", self.ids[0])
            kinds = self.run_db(lambda db: _kinds(db, self.ids[0]))
            self.assertIn("big_company", kinds)
            source_runner._running.clear()
            self.run_db(lambda db: _set(db, self.ids[1], inn="500100732259"))   # ИП — 12 цифр
            run = self.enrich("fns_revenue", self.ids[1])
            self.assertEqual(run.stats["result"]["chain"]["fns_revenue"]["reasons"], {"ИП не сдают бухотчётность": 1})
            source_runner._running.clear()
            source_settings.transport = fns_net(search=b'{"content": [], "totalElements": 0}')
            run = self.enrich("fns_revenue", self.ids[0])
            self.assertEqual(run.stats["result"]["chain"]["fns_revenue"]["reasons"], {"нет отчётности в ГИР БО": 1})
        finally:
            source_settings.transport = old

    def test_dadata_not_configured_and_wrapper_keeps_signals(self):
        from app.domains.leadgen import routes, source_runner
        source_runner._running.clear()
        self.seed()
        self.run_db(lambda db: _set(db, self.ids[0], inn=VALID_INN))
        old = routes.dadata_factory

        def broken():
            from app.domains.leadgen.dadata import DadataNotConfigured
            raise DadataNotConfigured("нет")
        routes.dadata_factory = broken
        try:
            run = self.enrich("dadata", self.ids[0])
            self.assertEqual(run.stats["result"]["errors"]["dadata"], "DaData: DaData не настроена в портале")

            class Provider:
                async def find_party(self, inn):
                    return {"inn": inn, "ogrn": "1236300000001", "state": {"status": "ACTIVE"},
                            "name": {"short_with_opf": "ООО «Кухни Волги»", "full_with_opf": "ООО «Кухни Волги»",
                                     "short": "Кухни Волги"}, "okved": "31.02",
                            "management": {"name": "Иванов Иван", "post": "Директор"}}
            routes.dadata_factory = lambda: Provider()
            source_runner._running.clear()
            run = self.enrich("dadata", self.ids[0])
            self.assertEqual(run.stats["result"]["chain"]["dadata"]["done"], 1, run.stats)
            self.assertIn("legal_active", self.run_db(lambda db: _kinds(db, self.ids[0])))
            c = self.run_db(lambda db: db.get(LgCompany, self.ids[0]))
            self.assertEqual((c.okved, c.director_name), ("31.02", "Иванов Иван"))
        finally:
            routes.dadata_factory = old

    def test_fit_ai_wrapper(self):
        from app.domains.leadgen import routes, source_runner
        source_runner._running.clear()
        self.seed()
        old = (routes.fit_complete, routes.fit_available)
        routes.fit_available = lambda: True

        async def answer(system, messages, **kw):
            return '{"label": "no", "reason": "федеральная сеть", "chain": true}'
        routes.fit_complete = answer
        try:
            run = self.enrich("fit_ai", self.ids[3])
            self.assertEqual(run.stats["result"]["chain"]["fit_ai"]["done"], 1, run.stats)
            c = self.run_db(lambda db: db.get(LgCompany, self.ids[3]))
            self.assertEqual(c.fit_label, "no")
            self.assertIn("chain", self.run_db(lambda db: _kinds(db, self.ids[3])))
        finally:
            routes.fit_complete, routes.fit_available = old


async def _kinds(db, cid):
    return {k for (k,) in (await db.execute(select(LgSignal.kind).where(LgSignal.company_id == cid))).all()}


class VariablesTests(WorkspaceBase):
    def test_source_variables_in_context_and_editor_and_manual_line(self):
        from app.domains.leadgen import sequences
        from app.domains.leadgen.core import sequence as seq_core
        self.seed()
        cid = self.ids[0]

        async def add(db):
            db.add(LgEnrichment(company_id=cid, step="site_tech", status="done",
                                result={"site_platform": "Тильда", "has_metrika_text": "нет",
                                        "has_calltracking_text": "да"}))
        self.run_db(add)
        r = self.client.patch(f"/api/admin/leadgen/companies/{cid}", json={"personal_line": "Увидели вашу вакансию."})
        self.assertEqual(r.status_code, 200, r.text)
        ctx = self.run_db(lambda db: _ctx(db, cid))
        self.assertEqual((ctx["site_platform"], ctx["has_metrika"], ctx["has_calltracking"], ctx["personal_line"]),
                         ("Тильда", "нет", "да", "Увидели вашу вакансию."))
        self.assertEqual(ctx["company"], "Кухни Волги")
        seq_core.parse_steps([{"channel": "email", "delay_days": 0, "subject": "Тема",
                               "body": "{{personal_line|Добрый день}} {{site_platform}}"}])
        variables = self.client.get("/api/admin/leadgen/sequences").json()["variables"]
        self.assertTrue({"personal_line", "site_platform", "has_calltracking"} <= set(variables))
        self.assertEqual(self.client.patch(f"/api/admin/leadgen/companies/{cid}",
                                           json={"personal_line": "x" * 201}).status_code, 422)


async def _ctx(db, cid):
    from app.domains.leadgen import sequences
    return await sequences._context(db, await db.get(LgCompany, cid), None)
