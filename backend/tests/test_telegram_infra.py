"""Этап A (docs/TELEGRAM_INFRA.md): форматы прокси, здоровье прокси и аккаунтов, массовое добавление с проверкой
в фоне, импорт tdata с отдельным прокси каждому аккаунту, замена и раздача прокси, @SpamBot, аватарки. Без сети."""
import asyncio
import io
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import test_stage2
from app.core.access import COOKIE
from app.core.config import settings
from app.models import Proxy, TelegramAccount
from app.services import account_health, proxy_health, telegram_jobs
from app.services.avatar_variants import variant
from app.services.proxy_parser import parse_proxy_line

ORIGIN = test_stage2.ORIGIN


class ParserTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "1.2.3.4:8000:u:p": ("socks5", "1.2.3.4", 8000, "u", "p", False),
            "u:p@1.2.3.4:8000": ("socks5", "1.2.3.4", 8000, "u", "p", False),
            "1.2.3.4:8000@u:p": ("socks5", "1.2.3.4", 8000, "u", "p", False),
            "u:p:proxy.example.com:8000": ("socks5", "proxy.example.com", 8000, "u", "p", False),
            "http://u:p@1.2.3.4:3128": ("http", "1.2.3.4", 3128, "u", "p", True),
            "1.2.3.4:8000": ("socks5", "1.2.3.4", 8000, None, None, False),
        }
        for line, want in cases.items():
            p = parse_proxy_line(line)
            self.assertEqual((p.scheme, p.host, p.port, p.username, p.password, p.explicit_scheme), want, line)
        self.assertEqual(parse_proxy_line("1.2.3.4:8000:u:p", "http").scheme, "http")
        for bad in ("abc", "1.2.3.4:70000", "a:b:c"):
            with self.assertRaises(ValueError):
                parse_proxy_line(bad)


def proxy(**kw):
    base = dict(id=1, scheme="socks5", host="1.1.1.1", port=1080, username=None, password_encrypted=None,
                status="untested", health=None, exit_ip=None, latency_ms=None, last_error=None, last_checked_at=None)
    return SimpleNamespace(**{**base, **kw})


class HealthTests(unittest.TestCase):
    def test_proxy_dead_after_three_fails_and_rotating(self):
        p = proxy()
        t = time.time()
        self.assertFalse(proxy_health.record(p, {"latency_ms": 120, "exit_ip": "9.9.9.1"}, now=t))
        self.assertEqual(proxy_health.view(p)["level"], "ok")
        self.assertFalse(proxy_health.record(p, None, "x", now=t + 1))
        self.assertFalse(proxy_health.record(p, None, "x", now=t + 2))
        self.assertTrue(proxy_health.record(p, None, "x", now=t + 3))      # стал мёртвым — сигнал для уведомления
        self.assertEqual((p.status, proxy_health.view(p)["level"], proxy_health.view(p)["score"]), ("error", "dead", 0))
        q = proxy()
        proxy_health.record(q, {"exit_ip": "9.9.9.1"}, now=t)
        proxy_health.record(q, {"exit_ip": "9.9.9.2"}, now=t + 1)
        view = proxy_health.view(q)
        self.assertTrue(view["rotating"])
        self.assertEqual(view["level"], "warn")

    def test_duplicates(self):
        a, b, c = proxy(id=1, exit_ip="5.5.5.5"), proxy(id=2, exit_ip="5.5.5.5"), proxy(id=3, exit_ip="6.6.6.6")
        self.assertEqual(proxy_health.duplicates([a, b, c]), {2: 1})

    def test_account_health(self):
        now = datetime.now(timezone.utc)
        acc = SimpleNamespace(health=None, avatar_version=0, username=None, bio=None, created_at=now,
                              session_encrypted="s", status="online", last_error=None, proxy_id=None,
                              flood_wait_until=None)
        h = account_health.compute(acc, None, {}, now)
        self.assertEqual(h["level"], "dead")
        self.assertIn("Нет прокси — аккаунт не подключается", h["reasons"])
        acc.proxy_id, acc.username, acc.health = 1, "sales", {"has_photo": True}
        account_health.apply_spamblock(acc, account_health.parse_spambot(
            "Unfortunately, your account is now limited until 25 Dec 2026."), now)
        h = account_health.compute(acc, "ok", {}, now)
        self.assertEqual((acc.status, h["level"], h["spamblock"]["state"]), ("spamblock", "warn", "limited"))
        account_health.apply_spamblock(acc, account_health.parse_spambot("Good news, no limits are currently applied"), now)
        self.assertEqual(acc.status, "online")

    def test_avatar_variants_unique_without_exif(self):
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (30, 120, 200)).save(buf, "JPEG", exif=Image.Exif())
        a, b = variant(buf.getvalue(), 1, "medium"), variant(buf.getvalue(), 2, "medium")
        self.assertNotEqual(a, b)
        self.assertFalse(dict(Image.open(io.BytesIO(a)).getexif()))


class ApiTests(unittest.TestCase):
    # Методы базы берём без привязки класса к имени модуля: иначе unittest запустит её тесты ещё раз.
    setUp_base = test_stage2.Stage2Tests.setUp
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        self.setUp_base()
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, "a" * 43)
        self.spawned = []
        self._old = (telegram_jobs.session_factory, telegram_jobs.spawn, settings.encryption_secret)
        telegram_jobs.session_factory = self.sessions
        telegram_jobs.spawn = lambda coro: self.spawned.append(coro)
        settings.encryption_secret = "test-secret-for-sessions"

    def tearDown(self):
        telegram_jobs.session_factory, telegram_jobs.spawn, settings.encryption_secret = self._old
        test_stage2.Stage2Tests.tearDown(self)

    def drain(self):
        while self.spawned:
            asyncio.run(self.spawned.pop(0))

    def add_proxies(self, text, **extra):
        r = self.client.post("/api/proxies/bulk", headers=ORIGIN, json={"entries": text, **extra})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_bulk_add_checks_in_background_and_picks_scheme(self):
        async def fake_probe(p, scheme=None):
            if p.host == "2.2.2.2" and (scheme or p.scheme) != "http":   # HTTP-прокси без схемы в строке
                raise proxy_health.ProbeError("Нет связи")
            if p.host == "3.3.3.3":
                raise proxy_health.ProbeError("Прокси не принял логин или пароль")
            return {"latency_ms": 100, "exit_ip": "8.8.8." + p.host[0]}
        result = self.add_proxies("# комментарий\n1.1.1.1:1080:u:p\n2.2.2.2:8080:u:p\n3.3.3.3:1080\n1.1.1.1:1080:u:p\nbad")
        self.assertEqual((result["imported"], result["duplicates"], len(result["errors"])), (3, 1, 1))
        self.assertIsNotNone(result["batch_id"])
        with patch.object(proxy_health, "probe", side_effect=fake_probe):
            self.drain()
        snap = self.client.get(f"/api/proxies/checks/{result['batch_id']}/progress").json()
        self.assertEqual(snap["state"], "done")
        listed = {p["host"]: p for p in self.client.get("/api/proxies?pool=agency").json()}
        self.assertEqual((listed["1.1.1.1"]["status"], listed["1.1.1.1"]["max_accounts"]), ("working", 1))
        self.assertEqual((listed["2.2.2.2"]["status"], listed["2.2.2.2"]["scheme"]), ("working", "http"))
        self.assertEqual(listed["3.3.3.3"]["status"], "error")
        self.assertEqual(listed["1.1.1.1"]["exit_ip"], "8.8.8.1")
        self.assertNotIn("password", str(listed))

    def seed_proxies(self, n, status="working"):
        async def go(db):
            for i in range(n):
                db.add(Proxy(id=100 + i, host=f"10.0.0.{i}", port=1080, pool="agency", status=status,
                             max_accounts=1, exit_ip=f"7.7.7.{i}", health={"checks": [[int(time.time()), 1]]}))
            await db.commit()
        self.run_db(go)

    def test_import_gives_each_account_own_proxy(self):
        self.seed_proxies(2)
        calls = []

        async def fake_convert(content, proxy_cfg):
            calls.append(proxy_cfg["addr"])
            n = len(calls)
            return SimpleNamespace(session=f"S{n}", telegram_user_id=1000 + n, phone=f"+7900000000{n}",
                                   first_name="A", last_name=None, username=None, bio=None)
        files = [("files", (f"a{i}.zip", b"PK-zip", "application/zip")) for i in range(3)]
        r = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN, files=files, data={"mode": "auto"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["without_proxy"], 1)
        with patch("app.services.tdata_import.convert_tdata_archive", side_effect=fake_convert), \
                patch("app.services.tdata_import.tdata_user_id", return_value=None):
            self.drain()
        result = self.client.get(f"/api/telegram-accounts/imports/{r.json()['batch_id']}").json()
        self.assertEqual(sorted(i["status"] for i in result["items"]), ["imported", "imported", "waiting_proxy"])
        self.assertEqual(sorted(calls), ["10.0.0.0", "10.0.0.1"])       # разные прокси, без подключения «без прокси»
        accounts = self.client.get("/api/telegram-accounts").json()
        self.assertEqual(len({a["proxy_id"] for a in accounts}), 2)
        self.assertEqual(accounts[0]["proxy"]["host"][:7], "10.0.0.")
        self.assertIn(accounts[0]["health"]["level"], ("ok", "warn"))

    def test_auto_reuses_proxy_after_duplicate_and_conversion_error(self):
        self.seed_proxies(2)

        async def seed(db):
            db.add(TelegramAccount(phone="+79000000001", telegram_user_id=1001, session_encrypted="",
                                   status="disconnected"))
            await db.commit()
        self.run_db(seed)
        active = set()

        async def convert(content, proxy_cfg):
            address = proxy_cfg["addr"]
            self.assertNotIn(address, active)
            active.add(address)
            try:
                await asyncio.sleep(0.03 if content in (b"duplicate", b"broken") else 0.01)
                if content == b"broken":
                    raise ValueError("Архив повреждён")
                n = {b"duplicate": 1, b"third": 3, b"fourth": 4}[content]
                return SimpleNamespace(session="", telegram_user_id=1000 + n, phone=f"+7900000000{n}",
                                       first_name="A", last_name=None, username=None, bio=None)
            finally:
                active.remove(address)
        files = [("files", (f"{i}.zip", content, "application/zip"))
                 for i, content in enumerate((b"duplicate", b"broken", b"third", b"fourth"))]
        response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN,
                                    files=files, data={"mode": "auto"})
        self.assertEqual(response.status_code, 202, response.text)
        with patch("app.services.tdata_import.tdata_user_id", return_value=None), \
                patch("app.services.tdata_import.convert_tdata_archive", side_effect=convert):
            self.drain()
        result = self.client.get(f"/api/telegram-accounts/imports/{response.json()['batch_id']}").json()
        self.assertEqual([item["status"] for item in result["items"]],
                         ["skipped", "error", "imported", "imported"])
        self.assertEqual(result["items"][1]["error"], "Архив повреждён")
        self.assertEqual(result["free_proxies"], 0)
        self.assertEqual(len({item["proxy"] for item in result["items"][2:]}), 2)

    def test_auto_duplicate_is_detected_before_connection_and_empty_pool_never_connects(self):
        async def seed(db):
            db.add(TelegramAccount(phone="+79000000001", telegram_user_id=1001, session_encrypted="",
                                   status="disconnected"))
            await db.commit()
        self.run_db(seed)
        files = [("files", (f"{i}.zip", content, "application/zip"))
                 for i, content in enumerate((b"duplicate", b"new"))]
        response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN,
                                    files=files, data={"mode": "auto"})
        with patch("app.services.tdata_import.tdata_user_id",
                   side_effect=lambda content: 1001 if content == b"duplicate" else 1002), \
                patch("app.services.tdata_import.convert_tdata_archive", new_callable=AsyncMock) as convert:
            self.drain()
        convert.assert_not_awaited()
        result = self.client.get(f"/api/telegram-accounts/imports/{response.json()['batch_id']}").json()
        self.assertEqual([item["status"] for item in result["items"]], ["skipped", "waiting_proxy"])
        self.assertEqual(result["free_proxies"], 0)

    def test_auto_checks_duplicate_and_bad_archive_before_reserving(self):
        self.seed_proxies(2)

        async def seed(db):
            db.add(TelegramAccount(phone="+79000000001", telegram_user_id=1001, session_encrypted="",
                                   status="disconnected"))
            await db.commit()
        self.run_db(seed)

        def read_id(content):
            if content == b"broken":
                raise ValueError("Архив повреждён")
            return int(content)

        async def convert(content, proxy_cfg):
            self.assertIsNotNone(proxy_cfg)
            n = int(content)
            return SimpleNamespace(session="", telegram_user_id=n, phone=f"+7900000{n}",
                                   first_name="A", last_name=None, username=None, bio=None)
        response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN,
                                    data={"mode": "auto"}, files=[
                                        ("files", (f"{i}.zip", content, "application/zip"))
                                        for i, content in enumerate((b"1001", b"broken", b"1003", b"1004"))])
        self.assertEqual(response.status_code, 202, response.text)
        with patch("app.services.tdata_import.tdata_user_id", side_effect=read_id), \
                patch("app.services.tdata_import.convert_tdata_archive", side_effect=convert) as conversion:
            self.drain()
        self.assertEqual(conversion.await_count, 2)
        result = self.client.get(f"/api/telegram-accounts/imports/{response.json()['batch_id']}").json()
        self.assertEqual([item["status"] for item in result["items"]],
                         ["skipped", "error", "imported", "imported"])
        self.assertEqual(result["free_proxies"], 0)

    def test_auto_respects_max_accounts_and_reports_remaining_capacity(self):
        self.seed_proxies(1)

        async def capacity(db):
            (await db.get(Proxy, 100)).max_accounts = 3
            await db.commit()
        self.run_db(capacity)

        async def convert(content, proxy_cfg):
            self.assertIsNotNone(proxy_cfg)
            await asyncio.sleep(0.01)
            n = int(content)
            return SimpleNamespace(session="", telegram_user_id=2000 + n, phone=f"+7900000000{n}",
                                   first_name="A", last_name=None, username=None, bio=None)
        response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN, data={"mode": "auto"},
                                    files=[("files", (f"{i}.zip", str(i).encode(), "application/zip"))
                                           for i in (1, 2)])
        with patch("app.services.tdata_import.tdata_user_id", return_value=None), \
                patch("app.services.tdata_import.convert_tdata_archive", side_effect=convert):
            self.drain()
        result = self.client.get(f"/api/telegram-accounts/imports/{response.json()['batch_id']}").json()
        self.assertEqual([item["status"] for item in result["items"]], ["imported", "imported"])
        self.assertEqual(result["free_proxies"], 1)

    def test_parallel_batches_share_reservations(self):
        self.seed_proxies(2)
        active = set()

        async def convert(content, proxy_cfg):
            address = proxy_cfg["addr"]
            self.assertNotIn(address, active)
            active.add(address)
            try:
                await asyncio.sleep(0.03)
                n = int(content)
                return SimpleNamespace(session="", telegram_user_id=3000 + n, phone=f"+7900000000{n}",
                                       first_name="A", last_name=None, username=None, bio=None)
            finally:
                active.remove(address)
        batches = []
        for numbers in ((1, 2), (3, 4)):
            response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN,
                                        data={"mode": "auto"}, files=[
                                            ("files", (f"{i}.zip", str(i).encode(), "application/zip"))
                                            for i in numbers])
            self.assertEqual(response.status_code, 202, response.text)
            batches.append(response.json()["batch_id"])

        async def together():
            jobs = self.spawned[:]
            self.spawned.clear()
            await asyncio.gather(*jobs)
        with patch("app.services.tdata_import.tdata_user_id", return_value=None), \
                patch("app.services.tdata_import.convert_tdata_archive", side_effect=convert):
            asyncio.run(together())
        rows = [item for batch in batches for item in
                self.client.get(f"/api/telegram-accounts/imports/{batch}").json()["items"]]
        self.assertEqual(sorted(row["status"] for row in rows),
                         ["imported", "imported", "waiting_proxy", "waiting_proxy"])
        self.assertEqual(len({row["proxy"] for row in rows if row["status"] == "imported"}), 2)

    def test_order_keeps_the_selected_proxy_for_each_file(self):
        self.seed_proxies(2)
        calls = []

        async def convert(content, proxy_cfg):
            calls.append((content, proxy_cfg["addr"]))
            if content == b"broken":
                raise ValueError("Архив повреждён")
            return SimpleNamespace(session="", telegram_user_id=4001, phone="+79000000001",
                                   first_name="A", last_name=None, username=None, bio=None)
        response = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN,
                                    data={"mode": "order", "proxy_ids": "101,100"}, files=[
                                        ("files", ("broken.zip", b"broken", "application/zip")),
                                        ("files", ("good.zip", b"good", "application/zip")),
                                    ])
        self.assertEqual(response.status_code, 202, response.text)
        with patch("app.services.tdata_import.convert_tdata_archive", side_effect=convert):
            self.drain()
        self.assertEqual(dict(calls), {b"broken": "10.0.0.1", b"good": "10.0.0.0"})
        result = self.client.get(f"/api/telegram-accounts/imports/{response.json()['batch_id']}").json()
        self.assertEqual([row["status"] for row in result["items"]], ["error", "imported"])
        self.assertEqual(result["free_proxies"], 1)

    def test_order_mode_and_replace(self):
        self.seed_proxies(2)
        files = [("files", ("a.zip", b"PK", "application/zip"))]
        bad = self.client.post("/api/telegram-accounts/import-batch", headers=ORIGIN, files=files,
                               data={"mode": "order", "proxy_ids": "100,101"})
        self.assertEqual(bad.status_code, 422)

        async def seed(db):
            db.add(TelegramAccount(id=5, phone="+79001112233", session_encrypted="x", proxy_id=100, status="online"))
            db.add(TelegramAccount(id=6, phone="+79001112234", session_encrypted="x", proxy_id=None, status="disconnected"))
            await db.commit()
        self.run_db(seed)
        self.assertEqual(self.client.post("/api/telegram-accounts/5/replace-proxy", headers=ORIGIN, json={}).status_code, 422)
        self.assertEqual(self.client.post("/api/telegram-accounts/5/replace-proxy", headers=ORIGIN,
                                          json={"confirm_ip_change": True}).json()["proxy_id"], 101)
        assigned = self.client.post("/api/telegram-accounts/assign-proxies", headers=ORIGIN, json={}).json()
        self.assertEqual(assigned, {"assigned": 1, "still_waiting": 0})   # 100 освободился после замены
        summary = self.client.get("/api/telegram-accounts/health-summary").json()
        self.assertEqual((summary["accounts"]["total"], summary["proxies"]["free"]), (2, 0))

    def test_spamcheck_never_without_proxy(self):
        async def seed(db):
            db.add(TelegramAccount(id=7, phone="+79001112235", session_encrypted="x", proxy_id=None, status="online"))
            await db.commit()
        self.run_db(seed)
        self.assertEqual(self.client.post("/api/telegram-accounts/spamcheck", headers=ORIGIN, json={}).status_code, 422)

    def test_avatar_preview(self):
        buf = io.BytesIO()
        Image.new("RGB", (400, 400), (200, 50, 50)).save(buf, "PNG")
        r = self.client.post("/api/telegram-accounts/avatars/preview", headers=ORIGIN,
                             files={"file": ("a.png", buf.getvalue(), "image/png")}, data={"strength": "low"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(set(r.json()["variants"])), 4)


    def test_campaign_runner_skips_account_without_proxy(self):
        from app.models import Campaign
        from app.models.automation import CampaignAccount, CampaignEvent, CampaignRuntime
        from app.services import campaign_runner

        async def seed(db):
            db.add(Campaign(id=1, name="Тест"))
            db.add(TelegramAccount(id=9, phone="+79001119999", session_encrypted="x", proxy_id=None, status="online"))
            await db.flush()
            db.add(CampaignRuntime(campaign_id=1, status="running", settings={"sleep_periods": ""}))
            db.add(CampaignAccount(campaign_id=1, account_id=9))
            await db.commit()
        self.run_db(seed)
        connect = AsyncMock()
        with patch.object(campaign_runner, "SessionLocal", self.sessions), \
                patch.object(campaign_runner, "connect_account", connect):
            asyncio.run(campaign_runner.tick(1))
        connect.assert_not_awaited()
        events = self.run_db(lambda db: db.scalars(__import__("sqlalchemy").select(CampaignEvent.event)))
        self.assertIn("account_no_proxy", list(events))


if __name__ == "__main__":
    unittest.main()
