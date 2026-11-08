"""Скорость функций по базе: один обход сайта на запуск, вторая попытка для не ответивших, органика без повтора,
расчёт времени конвейера. Без сети."""
import asyncio
import unittest

import httpx

from app.domains.leadgen import providers, site_enrich, source_runner
from app.domains.leadgen.sources import CompanyView, Context
from app.domains.leadgen.sources.site_contacts import ADAPTER as CONTACTS
from app.domains.leadgen.sources.site_tech import ADAPTER as TECH

HOME = ('<html><head><title>Кухни</title><script src="https://mc.yandex.ru/metrika/tag.js"></script></head>'
        '<body><a href="/contacts">Контакты</a> info@kuhni.example © 2026</body></html>')
CONTACTS_PAGE = "<html><body>sales@kuhni.example +7 495 000-00-00</body></html>"


async def noop(*_):
    return None


class SpeedTests(unittest.TestCase):
    def test_site_fetched_once_for_all_functions(self):
        log = []

        def handler(request):
            log.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            body = CONTACTS_PAGE if request.url.path == "/contacts" else HOME
            return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})
        ctx = Context(transport=httpx.MockTransport(handler), sleep=noop)
        ctx.cache["crawl_delay"] = 0
        view = CompanyView(id=1, domain="kuhni.example")

        async def go():   # контакты и тех-профиль одновременно — как в конвейере
            return await asyncio.gather(CONTACTS.enrich(view, {}, ctx), TECH.enrich(view, {}, ctx))
        contacts, tech = asyncio.run(go())
        self.assertEqual(log.count("/"), 1)                        # главная — один раз на обе функции
        self.assertEqual(tech.status, "done")
        self.assertTrue(tech.profile["has_metrika"])
        self.assertEqual(contacts.crawl.fetched[-1], "https://kuhni.example/contacts")

    def test_dead_site_gets_second_slow_try_and_is_shared(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            raise httpx.ConnectTimeout("timeout", request=request)
        ctx = Context(transport=httpx.MockTransport(handler), sleep=noop)
        view = CompanyView(id=1, domain="dead.example")

        async def go():
            first = await site_enrich.crawl("dead.example", transport=ctx.transport, cache=ctx.cache, delay=0)
            tech = await TECH.enrich(view, {}, ctx)
            return first, tech
        first, tech = asyncio.run(go())
        self.assertTrue(first.dead)
        self.assertEqual(calls, ["/robots.txt", "/robots.txt"])    # быстрая попытка + медленная, без http:// и главной
        self.assertEqual((tech.status, tech.error), ("failed", "сайт не ответил вовремя"))   # тех-профиль не ждёт снова

    def test_organic_search_does_not_retry_without_ads(self):
        class P(providers.XmlStockLiveProvider):
            def __init__(self):
                super().__init__(user="u", key="k", url="https://x.example")
                self.retry_pause, self.min_interval, self.raw_calls = 0, 0, 0

            async def fetch_raw(self, *a, **kw):
                self.raw_calls += 1
                return "<yandexsearch><response><results><grouping></grouping></results></response></yandexsearch>"
        p = P()
        asyncio.run(p.search_page("кухни", 213, want_ads=False))
        self.assertEqual(p.raw_calls, 1)                          # органика: без повтора и без +10 с
        p.raw_calls = 0
        asyncio.run(p.search_page("кухни", 213))
        self.assertEqual(p.raw_calls, 2)                          # Директ: повтор, как раньше

    def test_pipeline_seconds(self):
        self.assertEqual(source_runner.pipeline_seconds(0, [(4, 3.0)]), 0)
        # 100 компаний, шаги 12×5 с и 20×1 с: самый медленный шаг 9×5=45 с, поток 5×6=30 с -> 45 с
        self.assertEqual(source_runner.pipeline_seconds(100, [(12, 5.0), (20, 1.0)]), 45)
