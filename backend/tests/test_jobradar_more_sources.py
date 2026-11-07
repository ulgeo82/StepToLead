"""No network: all requests use Context.transport, pauses and AI are stubbed."""
import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.domains.jobradar.sources.base import Context, SourceFormatError
from app.domains.jobradar.sources.flru import ADAPTER as FLRU, parse_feed, project_details
from app.domains.jobradar.sources.getmatch import ADAPTER as GETMATCH, parse_offer
from app.domains.jobradar.sources.telegram_channels import (
    ADAPTER as TELEGRAM, check_channel, normalize_channel, parse_channel,
)

FIXTURES = Path(__file__).parent / "fixtures" / "jobradar"
SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 7, 14, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def sample_offers():
    return json.loads((FIXTURES / "getmatch_offers.json").read_text())


def sample_html():
    return (FIXTURES / "tg_channel.html").read_text()


def context(handler):
    return Context(transport=httpx.MockTransport(handler), sleep=AsyncMock())


class GetmatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_invalid_records_are_skipped_and_counted(self):
        valid = sample_offers()["offers"][1:]
        for rows, expected, fails in (([{}, *valid], 2, False), ([{}, valid[0]], 1, False),
                                      ([{}, {}, valid[0]], 0, True)):
            with self.subTest(rows=len(rows), fails=fails):
                ctx = context(lambda _: httpx.Response(200, json={
                    "meta": {"total": len(rows), "offset": 0, "limit": 100}, "offers": rows,
                }))
                if fails:
                    with self.assertRaises(SourceFormatError):
                        await GETMATCH.fetch({}, SINCE, {}, {}, ctx)
                    self.assertNotIn("getmatch", ctx.cache)
                else:
                    found = await GETMATCH.fetch({}, SINCE, {}, {}, ctx)
                    self.assertEqual(len(found), expected)
                self.assertEqual(ctx.cache["getmatch_skipped"], 2 if fails else 1)

    async def test_relocation_empty_city_and_unknown_format_keep_offers(self):
        for city in ("", None):
            with self.subTest(city=city):
                relocation = copy.deepcopy(sample_offers()["offers"][1])
                relocation["id"] = 99999
                relocation["location_requirements"] = [{"city": city, "format": "relocation_company"}]
                rows = [relocation, *sample_offers()["offers"][1:]]
                ctx = context(lambda _: httpx.Response(200, json={
                    "meta": {"total": 3, "offset": 0, "limit": 100}, "offers": rows,
                }))
                found = await GETMATCH.fetch({}, SINCE, {}, {}, ctx)
                self.assertEqual(len(found), 3)
                self.assertEqual(ctx.cache["getmatch_skipped"], 0)
                self.assertEqual(found[0].extra["work_format"], "relocation")
                self.assertEqual(found[0].city, "")
                self.assertFalse(found[0].remote)
                relocation["location_requirements"][0]["format"] = "unknown_future_format"
                parsed = parse_offer(relocation)
                self.assertEqual(parsed.extra["work_format"], "office")
                self.assertFalse(parsed.remote)

    async def test_live_offer_parse_pagination_and_cache_between_filters(self):
        sample = sample_offers()
        requests = []
        def handler(request):
            self.assertEqual(request.url.host, "getmatch.ru")
            self.assertEqual(request.extensions["timeout"]["read"], 20)
            offset = int(request.url.params["offset"])
            requests.append(offset)
            return httpx.Response(200, json={"meta": {"total": 3, "offset": offset, "limit": 2},
                                              "offers": sample["offers"][offset:offset + 2]})
        ctx = context(handler)
        found = await GETMATCH.fetch({"keywords": ["Python"], "remote_only": True}, SINCE, {}, {}, ctx)
        self.assertEqual(len(found), 1)
        vacancy = found[0]
        self.assertEqual(vacancy.ext_id, "36673")
        self.assertEqual(vacancy.company, "МегаФон")
        self.assertIn("Python", vacancy.extra["stack"])
        self.assertEqual(vacancy.extra["work_format"], "remote")
        self.assertEqual(vacancy.city, "Москва")
        self.assertTrue(vacancy.url.startswith("https://getmatch.ru/vacancies/"))
        second = await GETMATCH.fetch({"keywords": ["QA"]}, SINCE, {}, {}, ctx)
        self.assertTrue(second)
        self.assertEqual(requests, [0, 2])
        self.assertIn("getmatch", ctx.cache)

    async def test_changed_format_and_json_failures(self):
        for data in ({"items": []}, [], {"meta": {}, "offers": []},
                     {"meta": {"total": 1, "offset": 0, "limit": 20}, "offers": [{}]}):
            with self.subTest(data=data):
                ctx = context(lambda _: httpx.Response(200, json=data))
                with self.assertRaises(SourceFormatError):
                    await GETMATCH.fetch({}, SINCE, {}, {}, ctx)
                self.assertNotIn("getmatch", ctx.cache)
        with self.assertRaises(SourceFormatError):
            await GETMATCH.fetch({}, SINCE, {}, {}, context(lambda _: httpx.Response(200, text="not JSON")))

    async def test_no_partial_cache_and_no_infinite_pagination(self):
        offer = sample_offers()["offers"][1]
        ctx = context(lambda r: httpx.Response(200, json={
            "meta": {"total": 2, "offset": int(r.url.params["offset"]), "limit": 1}, "offers": [offer]}))
        with self.assertRaises(SourceFormatError):
            await GETMATCH.fetch({}, SINCE, {}, {}, ctx)
        self.assertNotIn("getmatch", ctx.cache)

    async def test_words_exclusions_salary_and_city(self):
        row = copy.deepcopy(sample_offers()["offers"][1])
        row.update(salary_hidden=False, salary_display_from=100000, salary_display_to=200000, salary_currency="RUB")
        ctx = context(lambda _: httpx.Response(200, json={"meta": {"total": 1, "offset": 0, "limit": 20},
                                                         "offers": [row]}))
        self.assertEqual(len(await GETMATCH.fetch({"salary_from": 200000}, SINCE, {}, {}, ctx)), 1)
        for conditions in ({"salary_from": 200001}, {"exclude": ["PYTHON"]},
                           {"keywords": ["директор"]}, {"regions": ["Самара"], "strict_city": True}):
            self.assertEqual(await GETMATCH.fetch(conditions, SINCE, {}, {}, ctx), [])
        self.assertEqual(len(await GETMATCH.fetch({"regions": ["Самара"]}, SINCE, {}, {}, ctx)), 1)
        self.assertEqual(parse_offer(row).salary_to, 200000)

    async def test_hidden_salary_policy_office_and_dates(self):
        row = copy.deepcopy(sample_offers()["offers"][1])
        row["location_requirements"][0]["format"] = "office"
        ctx = context(lambda _: httpx.Response(200, json={"meta": {"total": 1, "offset": 0, "limit": 20},
                                                         "offers": [row]}))
        for conditions in ({"allow_no_salary": False}, {"remote_only": True}, {"regions": ["Самара"]}):
            self.assertEqual(await GETMATCH.fetch(conditions, SINCE, {}, {}, ctx), [])
        self.assertEqual(await GETMATCH.fetch({}, NOW, {}, {}, ctx), [])

    async def test_overlapping_pages_deduplicate_without_losing_new_items(self):
        offers = sample_offers()["offers"]
        def handler(request):
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json={"meta": {"offset": offset, "limit": 2, "total": 4},
                                              "offers": offers[:2] if offset == 0 else offers[1:]})
        found = await GETMATCH.fetch({}, datetime(2026, 10, 1, tzinfo=timezone.utc), {}, {}, context(handler))
        self.assertEqual(len(found), 3)

    async def test_invalid_nested_salary_or_stack_is_format_error(self):
        for update in ({"skills_objects": [{}]}, {"salary_currency": []},
                       {"salary_display_from": "invalid"}, {"offer_description": {"html": "changed"}}):
            with self.subTest(update=update):
                with self.assertRaises(SourceFormatError):
                    parse_offer({**sample_offers()["offers"][1], **update})


class FlruTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_without_remote_word_passes_remote_only_filter(self):
        from app.domains.jobradar.core import matches

        content = """<rss><channel><item><title>Маркетолог</title>
        <link>https://www.fl.ru/projects/1/test</link>
        <description>Настроить рекламную кампанию. Бюджет: 50000 ₽.</description>
        </item></channel></rss>"""
        conditions = {"keywords": ["маркетолог"], "remote_only": True}
        ctx = context(lambda _: httpx.Response(200, text=content))
        found = await FLRU.fetch(conditions, SINCE, {}, {"include_projects": True}, ctx)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].remote)
        self.assertEqual(matches(found[0], conditions), (True, ""))

    async def test_live_feed_projects_and_explicit_opt_in(self):
        content = (FIXTURES / "flru.xml").read_text()
        requests = []
        ctx = context(lambda request: (requests.append(request), httpx.Response(200, text=content))[1])
        self.assertEqual(await FLRU.fetch({}, SINCE, {}, {}, ctx), [])
        self.assertEqual(requests, [])
        found = await FLRU.fetch({"keywords": ["SEO"]}, SINCE, {}, {"include_projects": True}, ctx)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "project")
        self.assertFalse(found[0].salary_known)
        self.assertEqual(found[0].ext_id, "5528023")
        long_text_match = await FLRU.fetch({"keywords": ["аутрич"]}, SINCE, {}, {"include_projects": True}, ctx)
        self.assertEqual(len(long_text_match), 1)

    async def test_budget_deadline_and_unknown_budget_not_filtered(self):
        self.assertEqual(project_details("Бюджет: 50 000 руб. Срок: 5 дней."),
                         {"budget": 50000, "deadline": "5 дней"})
        self.assertEqual(project_details("Оплата: 20 тыс. ₽"), {"budget": 20000, "deadline": None})
        self.assertIsNone(project_details("По договорённости")["budget"])
        content = """<rss><channel><item><title>Маркетолог</title><link>https://www.fl.ru/projects/1/test</link>
        <description>Бюджет: 50000 ₽. Срок: 5 дней.</description></item>
        <item><title>Маркетолог</title><link>https://www.fl.ru/projects/2/test</link>
        <description>По договорённости</description></item></channel></rss>"""
        ctx = context(lambda _: httpx.Response(200, text=content))
        found = await FLRU.fetch({}, SINCE, {}, {"include_projects": True, "budget_from": 50000}, ctx)
        self.assertEqual(len(found), 2)
        found = await FLRU.fetch({}, SINCE, {}, {"include_projects": True, "budget_from": 50001}, ctx)
        self.assertEqual([v.ext_id for v in found], ["2"])
        self.assertEqual(await FLRU.fetch({"exclude": ["маркетолог"]}, SINCE, {}, {"include_projects": True}, ctx), [])

    async def test_changed_feed_format(self):
        for content in ("<html>no rss</html>", "broken", "<rss/>"):
            with self.assertRaises(SourceFormatError):
                parse_feed(content)


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch("app.domains.jobradar.sources.telegram_channels.datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def test_normalization_and_closed_channel_no_http(self):
        for raw in ("@Sample_Jobs", "t.me/sample_jobs", "https://t.me/s/sample_jobs/"):
            self.assertEqual(normalize_channel(raw), ("sample_jobs", None))
        for raw in ("https://t.me/+abc", "t.me/joinchat/abc"):
            self.assertEqual(normalize_channel(raw)[1], "закрытый канал, читать нельзя")
            ctx = context(lambda _: self.fail("Closed channels must not cause HTTP"))
            self.assertEqual((await check_channel(raw, ctx))["status"], "closed")
        for raw in ("https://evil.test/sample_jobs", "file:///etc/passwd", "@bad/name", ""):
            self.assertIsNone(normalize_channel(raw)[0])

    async def test_live_post_dates_ignore_video_durations(self):
        html = (FIXTURES / "tg_channel_dates.html").read_text()
        _, posts = parse_channel(html, "telegram")
        self.assertEqual({post.ext_id: post.published.isoformat() for post in posts}, {
            "telegram/441": "2026-05-14T16:08:31+00:00",
            "telegram/442": "2026-05-14T16:08:52+00:00",
            "telegram/443": "2026-05-14T16:09:19+00:00",
            "telegram/444": "2026-05-14T16:09:39+00:00",
        })

    async def test_date_permalink_priority_and_datetime_fallback(self):
        date = "2026-05-14T16:08:31+00:00"
        other = "2026-01-01T00:00:00+00:00"
        duration = f'<time class="message_video_duration" datetime="{other}">0:06</time>'
        cases = [
            (f'{duration}<time datetime="{other}"></time>'
             f'<a class="tgme_widget_message_date"><time datetime="{date}"></time></a>', date),
            (f'{duration}<time>0:09</time><time datetime="{date}"></time>', date),
            (f'{duration}<time datetime="invalid"></time><time datetime="{date}"></time>', date),
            (duration, None),
        ]
        for times, expected in cases:
            with self.subTest(times=times):
                html = ('<div class="tgme_widget_message" data-post="telegram/441">'
                        '<div class="tgme_widget_message_text">Текст поста</div>' + times + '</div>')
                _, posts = parse_channel(html, "telegram")
                actual = posts[0].published
                self.assertEqual(actual.isoformat() if actual else None, expected)

    async def test_parse_preview_and_first_run_window(self):
        title, posts = parse_channel(sample_html(), "sample_jobs")
        self.assertEqual(title, "Тестовые вакансии")
        self.assertEqual(len(posts), 3)
        self.assertIn("Нужен маркетолог", posts[-1].extra["text"])
        self.assertNotIn("<b>", posts[-1].snippet)
        ctx = context(lambda _: httpx.Response(200, text=sample_html()))
        found = await TELEGRAM.fetch({}, SINCE, {}, {"channels": ["sample_jobs"]}, ctx)
        self.assertEqual([v.ext_id for v in found], ["sample_jobs/101", "sample_jobs/102"])
        state = ctx.channel_result["sample_jobs"]
        self.assertEqual((state["status"], state["last_post_id"], state["found"]), ("ok", 102, 2))
        self.assertEqual(len((await check_channel("@sample_jobs", ctx))["preview"]), 3)

    async def test_only_new_ids_words_exclusions_remote_and_city(self):
        ctx = context(lambda _: httpx.Response(200, text=sample_html()))
        ctx.channel_state["sample_jobs"] = 101
        found = await TELEGRAM.fetch({"keywords": ["маркетолог"], "exclude": ["senior"], "remote_only": True,
                                       "regions": ["Самара"], "strict_city": True}, SINCE, {},
                                      {"channels": ["sample_jobs"]}, ctx)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].url, "https://t.me/sample_jobs/102")
        self.assertEqual(found[0].city, "Самара")
        self.assertFalse(found[0].salary_known)
        ctx.channel_state["sample_jobs"] = 102
        self.assertEqual(await TELEGRAM.fetch({}, SINCE, {}, {"channels": ["sample_jobs"]}, ctx), [])

    async def test_body_exclusion_and_ai_extraction_is_stubbed(self):
        ctx = context(lambda _: httpx.Response(200, text=sample_html()))
        ctx.channel_state["sample_jobs"] = 100
        ctx.ai_extract = AsyncMock(return_value={"title": "Маркетолог", "company": "Пример",
                                                 "salary_from": 120000, "salary_to": 150000,
                                                 "remote": True, "city": "Самара"})
        found = await TELEGRAM.fetch({"keywords": ["маркетолог"], "exclude": ["senior"]}, SINCE, {},
                                      {"channels": ["sample_jobs"]}, ctx)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].title, found[0].company, found[0].salary_from), ("Маркетолог", "Пример", 120000))
        self.assertTrue(found[0].salary_known)
        ctx.ai_extract.assert_awaited_once()

    async def test_unavailable_429_and_empty_html_preserve_cursor(self):
        for response in (httpx.Response(429), httpx.Response(200, text=""), httpx.Response(200, text="<html/>")):
            ctx = context(lambda _: response)
            ctx.channel_state["sample_jobs"] = 100
            self.assertEqual(await TELEGRAM.fetch({}, SINCE, {}, {"channels": ["sample_jobs"]}, ctx), [])
            self.assertEqual(ctx.channel_result["sample_jobs"]["status"], "unavailable")
            self.assertEqual(ctx.channel_result["sample_jobs"]["last_post_id"], 100)
            self.assertFalse((await check_channel("sample_jobs", ctx))["ok"])

    async def test_channels_limit_pauses_and_proxy_context(self):
        called = []
        def handler(request):
            called.append(request.url.path)
            channel = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, text=sample_html().replace("sample_jobs", channel))
        ctx = context(handler)
        with patch.object(ctx, "http", wraps=ctx.http) as http:
            await TELEGRAM.fetch({}, SINCE, {}, {"channels": [f"jobs{i}" for i in range(31)]}, ctx)
        self.assertEqual(len(called), 30)
        self.assertEqual(ctx.sleep.await_count, 29)
        self.assertTrue(all(c.kwargs["use_proxy"] for c in http.call_args_list))

    async def test_healthcheck_does_not_import_default_probe_posts(self):
        ctx = context(lambda _: httpx.Response(200, text=sample_html().replace("sample_jobs", "telegram")))
        self.assertTrue((await TELEGRAM.healthcheck({}, ctx))[0])
        self.assertEqual(ctx.channel_result, {})
        self.assertEqual(await TELEGRAM.fetch({}, SINCE, {}, {"channels": []}, ctx), [])
