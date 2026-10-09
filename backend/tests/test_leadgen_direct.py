"""Поиск компаний по Директу: склейка объявлений, запуск, сигналы новых и пропавших рекламодателей."""
import asyncio
import os
import tempfile
import unittest
import httpx
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app import models  # noqa: F401
from app.domains.leadgen import direct_search
from app.domains.leadgen.core.serp import SerpAd, aggregate, ad_hash, run_fingerprint
from app.domains.leadgen.models import LgAd, LgCompany, LgSignal
from app.domains.leadgen.providers import (
    ProviderNotConfigured, StaticProvider, XmlStockLiveProvider, parse_xmlstock_live,
)
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
KEYS = ["кухни на заказ", "кухни под заказ", "заказать кухню", "шкаф купе на заказ"]


def ad(url, title="Кухни на заказ от 14 дней", text="Замер бесплатно", premium=False, display=None):
    return SerpAd(keyword="", url=url, title=title, text=text, premium=premium, display_url=display)


SEVER = ad("https://kuhni-sever.ru/?utm_source=yandex", premium=True)
DUB = ad("https://dub-mebel.ru/kuhni", title="Кухня мечты", text="Гарантия 5 лет")
AVITO = ad("https://www.avito.ru/ekaterinburg/mebel", title="Кухни на Авито")
FIRST = {KEYS[0]: [SEVER, DUB, AVITO], KEYS[1]: [SEVER], KEYS[2]: [SEVER, DUB]}


class SerpCoreTests(unittest.TestCase):
    def test_aggregate_by_domain_and_skip_platforms(self):
        items = [SerpAd(**{**a.__dict__, "keyword": k}) for k, ads in FIRST.items() for a in ads]
        res = aggregate(items)
        self.assertEqual(set(res), {"kuhni-sever.ru", "dub-mebel.ru"})
        self.assertEqual(res["kuhni-sever.ru"].keyword_count, 3)
        self.assertTrue(res["kuhni-sever.ru"].premium)
        self.assertEqual(len(res["kuhni-sever.ru"].ads), 1)  # одно объявление по трём ключам

    def test_display_url_wins_over_tracking_link(self):
        res = aggregate([SerpAd(keyword="k", url="https://yabs.yandex.ru/count/abc", display_url="kuhni-sever.ru")])
        self.assertEqual(set(res), {"kuhni-sever.ru"})

    def test_hash_and_fingerprint_stable(self):
        self.assertEqual(ad_hash(" Кухни ", "Текст"), ad_hash("кухни", "текст "))
        self.assertEqual(run_fingerprint("yandex_direct", 54, ["B", "a "]), run_fingerprint("yandex_direct", 54, ["a", "b"]))
        self.assertNotEqual(run_fingerprint("yandex_direct", 54, ["a"]), run_fingerprint("yandex_direct", 51, ["a"]))


class ProviderTests(unittest.TestCase):
    def test_xmlstock_requires_env(self):
        for name in ("XMLSTOCK_USER", "XMLSTOCK_KEY", "XMLSTOCK_LIVE_URL"):
            os.environ.pop(name, None)
        with self.assertRaises(ProviderNotConfigured):
            XmlStockLiveProvider()

    def test_parse_real_xmlstock_live_sample(self):
        """Реальный ответ XMLStock Live: «кухни на заказ самара», lr=51, 09.10.2026."""
        from pathlib import Path
        from app.domains.leadgen.core.serp import aggregate
        raw = (Path(__file__).parent / "fixtures" / "xmlstock_live_kuhni_samara.xml").read_text(encoding="utf-8")
        ads = parse_xmlstock_live(raw, "кухни на заказ самара")
        self.assertEqual((len(ads), sum(a.premium for a in ads)), (10, 5))
        self.assertEqual(ads[0].title, "Кухни на заказ в Самаре от производителя")
        self.assertTrue(ads[0].text.startswith("Кухни на заказ напрямую"))
        advertisers = aggregate(ads)
        self.assertIn("kuhni.modernova.ru", advertisers)
        self.assertTrue(advertisers["kuhni.modernova.ru"].premium)  # был и сверху, и снизу
        self.assertIn("ka2-design.ru", advertisers)
        self.assertEqual(len(advertisers), 9)

    def test_retries_on_glitches(self):
        from app.domains.leadgen.providers import ProviderError
        good = (Path(__file__).parent / "fixtures" / "xmlstock_live_kuhni_samara.xml").read_text(encoding="utf-8")
        no_ads = '<?xml version="1.0"?><yandexsearch><response><found>5</found><results/></response></yandexsearch>'
        no_money = '<yandexsearch><response><error code="32">Недостаточно средств</error></response></yandexsearch>'

        class P(XmlStockLiveProvider):
            retry_pause, min_interval = 0, 0

            def __init__(self, answers):
                super().__init__(user="u", key="k", url="https://x")
                self.answers, self.calls = list(answers), 0

            async def fetch_raw(self, keyword, region_code):
                self.calls += 1
                answer = self.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        p = P([httpx.ConnectError("boom"), "", good])
        self.assertEqual((len(asyncio.run(p.search("кухни", 51))), p.calls), (10, 3))
        p = P([no_ads, good])
        self.assertEqual(len(asyncio.run(p.search("кухни", 51))), 10)
        p = P([no_ads, no_ads])
        self.assertEqual((asyncio.run(p.search("кухни", 51)), p.calls), ([], 2))
        p = P([no_money, good])
        with self.assertRaises(ProviderError):
            asyncio.run(p.search("кухни", 51))
        self.assertEqual(p.calls, 1)  # нет денег — не повторяем

    def test_parse_errors(self):
        from app.domains.leadgen.providers import ProviderError
        with self.assertRaises(ProviderError) as ctx:
            parse_xmlstock_live('<?xml version="1.0"?><yandexsearch><response><error code="32">'
                                'Недостаточно средств</error></response></yandexsearch>', "кухни")
        self.assertIn("Недостаточно средств (код 32)", str(ctx.exception))
        with self.assertRaises(ProviderError):
            parse_xmlstock_live("Bad gateway", "кухни")
        empty = '<?xml version="1.0"?><yandexsearch><response><found>0</found></response></yandexsearch>'
        from app.domains.leadgen.providers import NoAdBlocks
        with self.assertRaises(NoAdBlocks):  # провайдер повторит запрос
            parse_xmlstock_live(empty, "кухни")
        only_top = '<yandexsearch><response><topads/></response></yandexsearch>'
        self.assertEqual(parse_xmlstock_live(only_top, "кухни"), [])


class DirectRunTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=1, name="StepToLead Agency"))
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

    async def launch(self, db, provider, now):
        run = await direct_search.create_run(db, 1, keywords=KEYS, region_code=54, niche="Кухни на заказ",
                                             city="Екатеринбург")
        return await direct_search.execute_run(db, run, provider, now=now)

    def test_estimate_and_validation(self):
        self.assertEqual(direct_search.estimate(KEYS + [" кухни  на заказ "], StaticProvider()),
                         {"requests": 4, "cost_rub": 0.05})

        async def fn(db):
            with self.assertRaises(ValueError):
                await direct_search.create_run(db, 1, keywords=["  "], region_code=54, niche=None, city=None)
        self.run_db(fn)

    def test_first_run_creates_companies_ads_and_signals(self):
        provider = StaticProvider(FIRST)

        async def fn(db):
            run = await self.launch(db, provider, NOW)
            companies = (await db.execute(select(LgCompany).order_by(LgCompany.score.desc()))).scalars().all()
            kinds = {(c.domain, k) for c in companies for (k,) in (await db.execute(
                select(LgSignal.kind).where(LgSignal.company_id == c.id))).all()}
            ads = await db.scalar(select(func.count(LgAd.id)))
            return run, companies, kinds, ads
        run, companies, kinds, ads = self.run_db(fn)
        self.assertEqual(run.status, "done")
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(provider.calls[0][1], 54)
        self.assertEqual([c.domain for c in companies], ["kuhni-sever.ru", "dub-mebel.ru"])
        self.assertEqual(companies[0].score, 7)  # ad_direct 3 + ad_premium 2 + new_advertiser 2
        self.assertEqual(companies[0].city, "Екатеринбург")
        self.assertIn(("kuhni-sever.ru", "ad_direct"), kinds)
        self.assertNotIn(("dub-mebel.ru", "ad_direct"), kinds)  # только 2 ключа
        self.assertIn(("dub-mebel.ru", "new_advertiser"), kinds)
        self.assertEqual(ads, 2)
        self.assertEqual((run.stats["companies"], run.stats["new_advertisers"], run.stats["ads"]), (2, 2, 6))

    def test_second_run_marks_stopped_and_not_new(self):
        async def fn(db):
            await self.launch(db, StaticProvider(FIRST), NOW)
            second = await self.launch(db, StaticProvider({KEYS[0]: [SEVER]}), NOW + timedelta(days=7))
            dub = (await db.execute(select(LgCompany).where(LgCompany.domain == "dub-mebel.ru"))).scalar_one()
            stopped = await db.scalar(select(func.count(LgSignal.id)).where(
                LgSignal.company_id == dub.id, LgSignal.kind == "ads_stopped"))
            companies = await db.scalar(select(func.count(LgCompany.id)))
            return second, stopped, companies
        second, stopped, companies = self.run_db(fn)
        self.assertEqual((second.stats["new_advertisers"], second.stats["already_in_base"]), (0, 1))
        self.assertEqual((second.stats["stopped"], stopped, companies), (1, 1, 2))

    def test_new_advertiser_expires_and_ad_signals_refresh(self):
        async def fn(db):
            await self.launch(db, StaticProvider(FIRST), NOW)
            later = NOW + timedelta(days=20)
            await self.launch(db, StaticProvider(FIRST), later)
            sever = (await db.execute(select(LgCompany).where(LgCompany.domain == "kuhni-sever.ru"))).scalar_one()
            return sever.score
        self.assertEqual(self.run_db(fn), 5)  # ad_direct + ad_premium, «новый» истёк

    def test_failed_keyword_does_not_stop_run(self):
        async def fn(db):
            run = await self.launch(db, StaticProvider(FIRST, failing={KEYS[1]}), NOW)
            all_failed = await self.launch(db, StaticProvider({}, failing=set(KEYS)), NOW)
            return run, all_failed
        run, all_failed = self.run_db(fn)
        self.assertEqual(run.status, "done")
        self.assertIn(KEYS[1], run.stats["failed"])
        self.assertEqual(all_failed.status, "failed")


if __name__ == "__main__":
    unittest.main()
