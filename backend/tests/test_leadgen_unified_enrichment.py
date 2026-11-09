"""Единый конвейер поиска/контактов и безопасная ручная очистка. Только подменённая сеть."""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select

from app.domains.leadgen import routes, scheduler, service, source_runner, source_settings
from app.domains.leadgen.core.serp import SerpAd
from app.domains.leadgen.core.table_import import Parsed, Row
from app.domains.leadgen.importer import import_parsed
from app.domains.leadgen.models import (
    LgCompany, LgContact, LgDnc, LgEnrichment, LgEnrollment, LgList, LgListItem, LgSequence, LgSourceRun, LgTouch,
)
from app.domains.leadgen.providers import StaticProvider
from app.domains.leadgen.sources import REGISTRY, Enrichment
from app.models.marketing import ClientWorkspace
from scripts.cleanup_personal_contacts import cleanup
from test_leadgen_sources_step2 import _Api


class UnifiedTests(_Api):
    def setUp(self):
        super().setUp()
        self.provider = routes.provider_factory
        routes.provider_factory = lambda: StaticProvider({"кухни": [SerpAd(
            keyword="кухни", url="https://new.example", title="Кухни", text="Кухни", premium=True)]})
        source_settings.transport = httpx.MockTransport(lambda request: httpx.Response(404))

    def tearDown(self):
        routes.provider_factory = self.provider
        super().tearDown()

    def test_direct_chain_validation_order_result_and_new_only(self):
        endpoint = "/api/admin/leadgen/runs"
        for invalid in (["missing"], ["serp"]):
            self.assertEqual(self.client.post(endpoint, json={"keywords": ["кухни"], "then": invalid}).status_code, 422)
        called = []
        async def enrich(company, settings, ctx):
            called.append(company.id)
            return Enrichment(profile={"name": "Кухни", "raw": company.name})
        with patch.object(REGISTRY["name_clean"], "enrich", enrich):
            first = self.client.post(endpoint, json={"keywords": ["кухни"], "then": ["name_clean"]})
            self.assertEqual(first.status_code, 202, first.text)
            result = self.client.get(f"{endpoint}/{first.json()['id']}").json()
            self.assertEqual(result["result"]["chain"]["name_clean"]["done"], 1)
            self.assertEqual(result["status"], "done")
            self.client.post(endpoint, json={"keywords": ["кухни"], "then": ["name_clean"]})
        self.assertEqual(len(called), 1)
        order = asyncio.run(routes.direct_chain(None, ["name_clean", "email_check", "find_site"]))
        self.assertEqual(order, ["find_site", "email_check", "name_clean"])

    def test_legacy_default_and_weekly_params(self):
        self.assertEqual(asyncio.run(routes.direct_chain(None, None, True)),
                         ["find_site", "site_contacts", "email_check", "name_clean"])
        self.assertEqual(asyncio.run(routes.direct_chain(None, None, False)), [])
        self.assertEqual(asyncio.run(routes.direct_chain(None, [], True)), [])
        async def fn(db):
            db.add(LgSourceRun(workspace_id=7, source="yandex_direct", status="done",
                params={"keywords": ["тест"], "repeat": True, "then": ["name_clean"], "fingerprint": "f"},
                finished_at=service.utcnow() - timedelta(days=8)))
            await db.flush()
            ids = await scheduler.schedule_due(db)
            return (await db.get(LgSourceRun, ids[0])).params
        self.assertEqual(self.run_db(fn)["then"], ["name_clean"])

    def test_contacts_staleness_and_progress_no_paid_step(self):
        async def seed(db):
            a = LgCompany(workspace_id=7, display_name="Бренд")
            b = LgCompany(workspace_id=7, display_name="Бренд 2")
            db.add_all([a, b])
            await db.flush()
            db.add(LgEnrichment(company_id=a.id, step="site_check", status="done", ran_at=service.utcnow()))
            return a.id, b.id
        a, b = self.run_db(seed)
        response = self.client.post("/api/admin/leadgen/companies/enrich", json={"company_ids": [a, b, 999]})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual((response.json()["queued"], response.json()["skipped"]), (1, 2))
        rid = response.json()["run_id"]
        result = self.client.get(f"/api/admin/leadgen/runs/{rid}").json()
        self.assertEqual(list(result["result"]["chain"]),
                         ["site_contacts", "email_check", "name_clean", "dadata", "fit_ai"])
        self.assertEqual(self.client.get(f"/api/admin/leadgen/runs/{rid}/progress").json()["state"], "done")
        retry = self.client.post("/api/admin/leadgen/companies/enrich",
                                 json={"company_ids": [a], "only_stale": False})
        self.assertEqual(retry.json()["queued"], 1)

    def test_collect_contacts_skips_assessed_but_workspace_reassesses(self):
        async def seed(db):
            company = LgCompany(workspace_id=7, fit_label="fit")
            lst = LgList(workspace_id=7, name="Оценка", kind="manual")
            db.add(lst)
            db.add(company)
            await db.flush()
            db.add(LgListItem(list_id=lst.id, company_id=company.id))
            return company.id, lst.id
        cid, lid = self.run_db(seed)
        with patch.object(REGISTRY["fit_ai"], "ready", return_value=None), patch(
            "app.domains.leadgen.fit_ai.assess", new_callable=AsyncMock, return_value={"status": "done"}
        ) as assess:
            response = self.client.post("/api/admin/leadgen/companies/enrich", json={"company_ids": [cid]})
            self.assertEqual(response.status_code, 202, response.text)
            assess.assert_not_called()
            run = self.client.get(f"/api/admin/leadgen/runs/{response.json()['run_id']}").json()
            self.assertEqual(run["result"]["chain"]["fit_ai"]["reasons"], {"компания уже оценена": 1})
            response = self.client.post(f"/api/admin/leadgen/lists/{lid}/run", json={"steps": ["fit_ai"]})
            self.assertEqual(response.status_code, 202, response.text)
            assess.assert_awaited_once()

    def test_direct_search_stays_done_if_chain_raises(self):
        from app.domains.leadgen import direct_search
        with patch.object(source_runner, "_run_steps", new_callable=AsyncMock,
                          side_effect=RuntimeError("Ошибка цепочки")):
            response = self.client.post("/api/admin/leadgen/runs",
                                        json={"keywords": ["кухни"], "then": ["name_clean"]})
        self.assertEqual(response.status_code, 202, response.text)
        rid = response.json()["id"]
        result = self.client.get(f"/api/admin/leadgen/runs/{rid}").json()
        self.assertEqual(result["status"], "done")
        expected_error = source_runner.short_error(RuntimeError("Ошибка цепочки"))
        self.assertEqual(result["error"], expected_error)
        self.assertEqual(result["result"]["errors"]["chain"], expected_error)
        self.assertEqual(len(result["companies"]), 1)
        async def previous(db):
            next_run = await direct_search.create_run(db, 7, keywords=["кухни"], region_code=None,
                                                       niche=None, city=None)
            return await direct_search._previous_domains(db, next_run)
        self.assertEqual(self.run_db(previous), {result["companies"][0]["id"]})

    def test_non_direct_chain_exception_still_marks_run_failed(self):
        async def seed(db):
            company = LgCompany(workspace_id=7)
            db.add(company)
            await db.flush()
            return (await source_runner.create_steps_run(db, 7, [company.id], ["name_clean"])).id
        rid = self.run_db(seed)
        with patch.object(source_runner, "_run_steps", new_callable=AsyncMock,
                          side_effect=RuntimeError("Ошибка цепочки")):
            asyncio.run(source_runner.execute_list(rid, self.sessions))
        run = self.run_db(lambda db: db.get(LgSourceRun, rid))
        self.assertEqual(run.status, "failed")

    def test_recheck_available_for_source_and_list_api(self):
        async def seed(db):
            company = LgCompany(workspace_id=7, inn="6312000014")
            lst = LgList(workspace_id=7, name="Тест", kind="manual")
            db.add_all([company, lst])
            await db.flush()
            db.add_all([LgListItem(list_id=lst.id, company_id=company.id),
                        LgEnrichment(company_id=company.id, step="email_by_inn", status="skipped",
                                     ran_at=service.utcnow(), result={"tried": ["dadata"]})])
            return company.id, lst.id
        cid, lid = self.run_db(seed)
        adapter = REGISTRY["email_by_inn"]
        with patch.object(adapter, "ready", return_value=None), patch.object(
            adapter, "enrich", new_callable=AsyncMock,
            return_value=Enrichment(status="skipped", error="в реестрах почты нет", profile={"tried": ["dadata"]})
        ) as enrich:
            for endpoint, body in [
                ("/sources/email_by_inn/run", {"target": {"company_ids": [cid]}}),
                (f"/lists/{lid}/run", {"steps": ["email_by_inn"]}),
            ]:
                start_count = enrich.await_count
                response = self.client.post("/api/admin/leadgen" + endpoint, json=body)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(enrich.await_count, start_count)
                response = self.client.post("/api/admin/leadgen" + endpoint, json={**body, "recheck": True})
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(enrich.await_count, start_count + 1)

    def test_import_drops_personal_even_without_site(self):
        async def fn(db):
            parsed = Parsed(rows=[Row(line=1, name="Компания", inn="6312000014",
                                      emails=["info@brand.example", "ivan.petrov@mail.ru"])], invalid=[], columns={})
            run = await import_parsed(db, 7, parsed)
            emails = list((await db.scalars(select(LgContact.value))).all())
            return run.stats, emails
        stats, emails = self.run_db(fn)
        self.assertEqual(stats["personal_dropped"], 1)
        self.assertEqual(emails, ["info@brand.example"])


class PersonalCleanupTests(_Api):
    def seed(self):
        async def fn(db):
            db.add(ClientWorkspace(id=8, name="Чужой workspace"))
            company = LgCompany(workspace_id=7)
            foreign = LgCompany(workspace_id=8)
            crm = LgCompany(workspace_id=7, crm_deal_id=123)
            db.add_all([company, foreign, crm])
            await db.flush()
            contacts = [LgContact(company_id=company.id, kind="email", value=f"person{i}@example.test",
                                  value_norm=f"person{i}@example.test", is_personal=True) for i in range(5)]
            contacts += [LgContact(company_id=foreign.id, kind="email", value="foreign@example.test",
                                   value_norm="foreign@example.test", is_personal=True),
                         LgContact(company_id=crm.id, kind="email", value="crm@example.test",
                                   value_norm="crm@example.test", is_personal=True),
                         LgContact(company_id=company.id, kind="email", value="info@example.test",
                                   value_norm="info@example.test", is_personal=False)]
            db.add_all(contacts)
            await db.flush()
            seq = LgSequence(workspace_id=7, name="Цепочка")
            db.add(seq)
            await db.flush()
            db.add_all([
                LgTouch(company_id=company.id, contact_id=contacts[1].id, channel="email", direction="out", status="sent"),
                LgEnrollment(workspace_id=7, sequence_id=seq.id, company_id=company.id, contact_id=contacts[2].id),
                LgDnc(workspace_id=7, kind="email", value_norm=contacts[3].value_norm, reason="legal"),
                LgTouch(company_id=company.id, address=contacts[4].value_norm, channel="email", direction="out", status="sent"),
            ])
            return contacts[0].id
        return self.run_db(fn)

    def cleanup(self, apply=False):
        async def fn():
            async with self.engine.begin() as connection:
                return await connection.run_sync(lambda conn: cleanup(conn, 7, apply=apply))
        return asyncio.run(fn())

    def test_dry_run_apply_and_idempotence(self):
        disposable = self.seed()
        report = self.cleanup()
        self.assertEqual((report["would_delete"], report["retained"], report["deleted"]), (1, 5, 0))
        self.assertIsNotNone(self.run_db(lambda db: db.get(LgContact, disposable)))
        self.assertEqual(report["references"]["lg_touches"], 2)
        self.assertEqual(report["references"]["lg_enrollments"], 1)
        self.assertEqual(report["references"]["lg_dnc"], 1)
        self.assertEqual(self.cleanup(True)["deleted"], 1)
        self.assertIsNone(self.run_db(lambda db: db.get(LgContact, disposable)))
        self.assertEqual(self.cleanup(True)["deleted"], 0)
        self.assertEqual(self.cleanup()["retained"], 5)
