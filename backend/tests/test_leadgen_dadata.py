"""DaData по ИНН и еженедельный повтор поиска."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.db import Base
from app.domains.leadgen import dadata, direct_search, scheduler, service
from app.domains.leadgen.models import LgCompany, LgEnrichment, LgSourceRun
from app.domains.leadgen.providers import StaticProvider
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
INN = "6315000005"
PARTY = {"inn": INN, "ogrn": "1027700132195", "okved": "31.02",
         "name": {"short_with_opf": "ООО «СЕВЕРНАЯ МЕБЕЛЬ»", "short": "СЕВЕРНАЯ МЕБЕЛЬ"},
         "management": {"name": "Иванов Иван Иванович", "post": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"},
         "state": {"status": "ACTIVE"}, "finance": {"year": 2025, "revenue": 48_500_000},
         "address": {"value": "г Екатеринбург"}}


def dadata_mock(party=PARTY, status=200, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append((request.headers.get("authorization"), json.loads(request.content)))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"suggestions": [{"data": party}] if party else []})
    return dadata.DadataProvider("tok", transport=httpx.MockTransport(handler))


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

    async def company(self, db, **kw):
        kw.setdefault("domain", "kuhni-sever.ru")
        return (await service.upsert_company(db, 7, service.FindingIn(source="yandex_direct", **kw), now=NOW)).company


class DadataTests(Base_):
    def test_requires_token(self):
        old = os.environ.pop("DADATA_TOKEN", None)
        try:
            with self.assertRaises(dadata.DadataNotConfigured):
                dadata.DadataProvider()
        finally:
            if old is not None:
                os.environ["DADATA_TOKEN"] = old

    def test_enrich_fills_legal_data_and_signal(self):
        seen = []

        async def fn(db):
            c = await self.company(db, inn=INN)
            res = await dadata.enrich_legal(db, c, dadata_mock(seen=seen), now=NOW)
            return c, res
        c, res = self.run_db(fn)
        self.assertEqual(seen, [("Token tok", {"query": INN, "branch_type": "MAIN", "count": 1})])
        self.assertEqual(res, {"status": "done", "legal_status": "active"})
        self.assertEqual((c.legal_name, c.director_name, c.director_post), (
            "ООО «СЕВЕРНАЯ МЕБЕЛЬ»", "Иванов Иван Иванович", "Генеральный директор"))
        self.assertEqual((c.okved, c.revenue_rub, c.revenue_year, c.ogrn), ("31.02", 48_500_000, 2025, "1027700132195"))
        self.assertEqual(c.display_name, "СЕВЕРНАЯ МЕБЕЛЬ")
        self.assertEqual(c.provenance["director_name"]["source"], "dadata")
        self.assertEqual(c.score, 1)  # legal_active

    def test_liquidated_zeroes_score(self):
        async def fn(db):
            c = await self.company(db, inn=INN)
            await service.add_signal(db, c, "ad_direct", now=NOW)
            await dadata.enrich_legal(db, c, dadata_mock({**PARTY, "state": {"status": "LIQUIDATED"}}), now=NOW)
            return c
        c = self.run_db(fn)
        self.assertEqual((c.legal_status, c.score), ("liquidated", 0))

    def test_no_inn_not_found_and_http_error(self):
        async def fn(db):
            a = await self.company(db)
            skipped = await dadata.enrich_legal(db, a, dadata_mock(), now=NOW)
            b = await self.company(db, domain="b.ru", inn=INN)
            missing = await dadata.enrich_legal(db, b, dadata_mock(party=None), now=NOW)
            err = await dadata.enrich_legal(db, b, dadata_mock(status=403), now=NOW)
            steps = await db.scalar(select(func.count(LgEnrichment.id)).where(LgEnrichment.step == "dadata"))
            return skipped, missing, err, steps
        skipped, missing, err, steps = self.run_db(fn)
        self.assertEqual((skipped["status"], missing["status"], err["status"], steps), ("skipped", "not_found", "failed", 2))


    def test_enrich_chain_site_then_dadata(self):
        from app.domains.leadgen import routes
        from test_leadgen_site import SITE, site
        old = (routes.session_factory, routes.site_transport, routes.site_delay, routes.dadata_factory)
        routes.session_factory, routes.site_transport, routes.site_delay = self.sessions, site(SITE), 0
        routes.dadata_factory = lambda: dadata_mock()
        try:
            cid = self.run_db(lambda db: self.company(db)).id
            asyncio.run(routes._enrich([cid], only_stale=True))
            c = self.run_db(lambda db: db.get(LgCompany, cid))
        finally:
            routes.session_factory, routes.site_transport, routes.site_delay, routes.dadata_factory = old
        self.assertEqual((c.inn, c.director_name, c.legal_status), (INN, "Иванов Иван Иванович", "active"))

class RepeatTests(Base_):
    async def launch(self, db, keywords, *, repeat, at, provider=None):
        run = await direct_search.create_run(db, 7, keywords=keywords, region_code=54, niche="Кухни", city="Екб")
        run.params = {**run.params, "repeat": repeat, "enrich": False}
        return await direct_search.execute_run(db, run, provider or StaticProvider({}), now=at)

    def test_due_after_week_only_for_repeat_series(self):
        async def fn(db):
            await self.launch(db, ["кухни"], repeat=True, at=NOW - timedelta(days=8))
            await self.launch(db, ["окна"], repeat=False, at=NOW - timedelta(days=30))
            await self.launch(db, ["потолки"], repeat=True, at=NOW - timedelta(days=2))
            created = await scheduler.schedule_due(db, now=NOW)
            again = await scheduler.schedule_due(db, now=NOW)   # новый запуск в очереди — повторно не создаём
            new = await db.get(LgSourceRun, created[0])
            return created, again, new
        created, again, new = self.run_db(fn)
        self.assertEqual((len(created), again), (1, []))
        self.assertEqual((new.status, new.params["keywords"], new.params["repeat"], new.params["region_code"]),
                         ("queued", ["кухни"], True, 54))

    def test_series_uses_latest_run(self):
        async def fn(db):
            await self.launch(db, ["кухни"], repeat=True, at=NOW - timedelta(days=20))
            await self.launch(db, ["кухни"], repeat=True, at=NOW - timedelta(days=1))  # свежий повтор той же серии
            return await scheduler.schedule_due(db, now=NOW)
        self.assertEqual(self.run_db(fn), [])

    def test_turning_repeat_off_on_latest_stops_series(self):
        async def fn(db):
            last = await self.launch(db, ["кухни"], repeat=True, at=NOW - timedelta(days=9))
            last.params = {**last.params, "repeat": False}
            await db.flush()
            return await scheduler.schedule_due(db, now=NOW)
        self.assertEqual(self.run_db(fn), [])


if __name__ == "__main__":
    unittest.main()
