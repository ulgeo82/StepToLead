"""Пространство клиента в лидогене и аутриче (docs/PLAN.md, этап 1): без выбора — агентство, данные клиентов
не смешиваются, настройки у каждого свои, отключённый клиент не рассылает, расход на данные пишется по клиенту."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db import Base, get_db
from app.domains.leadgen import fit_ai, hot, routes, scheduler, sequences, service, spend, warmup
from app.domains.leadgen.core.serp import SerpAd
from app.domains.leadgen.models import LgCompany, LgEnrollment, LgMailbox, LgSequence, LgSourceRun, LgSpend, LgTouch
from app.domains.leadgen.providers import StaticProvider
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace
from app.models.system import AppSetting

ORIGIN = {"Origin": "http://localhost:3000"}
HOME, CLIENT, GONE = 7, 8, 9
AS_CLIENT = {**ORIGIN, "X-Workspace-Id": str(CLIENT)}
BASE = "/api/admin/leadgen"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)   # среда, 15:00 по Москве
INN = "7707083893"
KEYS = ["оплата рекламы", "пополнить рекламный кабинет"]
AD = SerpAd(keyword="", url="https://agency-one.ru/", title="Ведём рекламу", text="Под ключ", premium=True)
STEPS = [{"channel": "email", "delay_days": 0, "subject": "Вопрос по {{company}}", "body": "Здравствуйте!"}]
BOX = {"email": "anna@onespot-mail.ru", "smtp_host": "smtp.yandex.ru", "imap_host": "imap.yandex.ru",
       "password": "secret", "sender_name": "Анна"}


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, mailbox, password, mail):
        self.sent.append(mail)

    async def fetch(self, mailbox, password, after_uid):
        return []

    async def check(self, mailbox, password):
        return None


class SpaceBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=HOME, name="StepToLead Agency"))
                db.add(ClientWorkspace(id=CLIENT, name="OneSpot", website="https://www.onespot.one/"))
                db.add(ClientWorkspace(id=GONE, name="Ушедший клиент", status="deleted"))
                db.add(AdminUser(id=1, username="admin", password_hash=hash_password("x" * 16), role="admin"))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        from app.domains.leadgen import source_settings
        self._source_settings = source_settings
        self._old = (settings.agency_workspace_id, routes.session_factory, routes.provider_factory,
                     source_settings.transport)
        source_settings.transport = httpx.MockTransport(lambda request: httpx.Response(404))   # в сеть не ходим
        settings.agency_workspace_id = HOME
        routes.session_factory = self.sessions
        routes.provider_factory = lambda: StaticProvider({KEYS[0]: [AD], KEYS[1]: [AD]})
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(COOKIE, "a" * 43)

    def tearDown(self):
        (settings.agency_workspace_id, routes.session_factory, routes.provider_factory,
         self._source_settings.transport) = self._old
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                result = await fn(db)
                await db.commit()
                return result
        return asyncio.run(go())

    def get(self, url, headers=ORIGIN):
        return self.client.get(BASE + url, headers=headers)

    def post(self, url, json=None, headers=ORIGIN):
        return self.client.post(BASE + url, json=json, headers=headers)

    def company(self, ws, domain="agency-one.ru", inn=None):
        async def fn(db):
            return (await service.upsert_company(db, ws, service.FindingIn(
                source="site", name="Агентство Один", domain=domain, inn=inn,
                contacts=[{"kind": "email", "value": f"info@{domain}"}]), now=NOW)).company.id
        return self.run_db(fn)


class SpaceChoiceTests(SpaceBase):
    def test_without_choice_it_is_the_agency(self):
        body = self.get("/spaces").json()
        self.assertEqual(body["current"], HOME)
        self.assertEqual([(i["id"], i["is_home"]) for i in body["items"]], [(HOME, True), (CLIENT, False)])

    def test_header_and_link_param_choose_the_client(self):
        self.assertEqual(self.get("/spaces", AS_CLIENT).json()["current"], CLIENT)
        self.assertEqual(self.get(f"/spaces?ws={CLIENT}").json()["current"], CLIENT)
        self.assertEqual(self.get("/spaces").json()["current"], HOME)   # выбор не «прилипает» к следующему запросу

    def test_unknown_deleted_and_garbage(self):
        self.assertEqual(self.get("/runs", {**ORIGIN, "X-Workspace-Id": "99"}).status_code, 404)
        self.assertEqual(self.get("/runs", {**ORIGIN, "X-Workspace-Id": str(GONE)}).status_code, 404)
        self.assertEqual(self.get("/runs", {**ORIGIN, "X-Workspace-Id": "abc"}).status_code, 422)
        self.assertEqual(self.get("/sources", {**ORIGIN, "X-Workspace-Id": "99"}).status_code, 404)   # источники
        self.assertEqual(self.get("/lists/1/table", {**ORIGIN, "X-Workspace-Id": "99"}).status_code, 404)  # рабочее место

    def test_stranger_learns_nothing_about_clients(self):
        anon = TestClient(self.client.app)
        self.assertEqual(anon.get(f"{BASE}/runs", headers={"X-Workspace-Id": "99"}).status_code, 401)
        self.assertEqual(anon.get(f"{BASE}/spaces").status_code, 401)


class IsolationTests(SpaceBase):
    def test_companies_do_not_mix(self):
        mine, theirs = self.company(HOME, inn=INN), self.company(CLIENT, inn=INN)
        self.assertNotEqual(mine, theirs)   # один ИНН у двух клиентов — две разные компании
        self.assertEqual([c["id"] for c in self.get("/companies").json()["items"]], [mine])
        self.assertEqual([c["id"] for c in self.get("/companies", AS_CLIENT).json()["items"]], [theirs])
        self.assertEqual(self.get(f"/companies/{theirs}").status_code, 404)
        self.assertEqual(self.get(f"/companies/{mine}", AS_CLIENT).status_code, 404)
        self.assertEqual(self.get(f"/companies/{theirs}", AS_CLIENT).status_code, 200)

    def test_lists_segments_dnc_sequences_mailboxes(self):
        cid = self.company(CLIENT)
        made = {
            "lists": self.post("/lists", {"name": "Агентства · октябрь", "company_ids": [cid]}, AS_CLIENT),
            "segments": self.post("/segments", {"name": "С почтой", "filters": {}}, AS_CLIENT),
            "dnc": self.post("/dnc", {"kind": "domain", "value": "konkurent.ru", "reason": "competitor"}, AS_CLIENT),
            "sequences": self.post("/sequences", {"name": "Первое письмо", "steps": STEPS}, AS_CLIENT),
            "mailboxes": self.post("/mailboxes", BOX, AS_CLIENT),
        }
        for name, response in made.items():
            self.assertEqual(response.status_code, 201, f"{name}: {response.text}")
            self.assertEqual(len(self.get(f"/{name}", AS_CLIENT).json()["items"]), 1, name)
            self.assertEqual(self.get(f"/{name}").json()["items"], [], name)   # у агентства пусто
        seq, box, lst = made["sequences"].json()["id"], made["mailboxes"].json()["id"], made["lists"].json()["id"]
        # чужое по id не открыть и не изменить
        self.assertEqual(self.client.delete(f"{BASE}/dnc/{made['dnc'].json()['id']}", headers=ORIGIN).status_code, 404)
        self.assertEqual(self.client.put(f"{BASE}/segments/{made['segments'].json()['id']}", headers=ORIGIN,
                                         json={"name": "x", "filters": {}}).status_code, 404)
        self.assertEqual(self.post(f"/sequences/{seq}/enroll", {"company_ids": [cid]}).status_code, 404)
        self.assertEqual(self.post(f"/mailboxes/{box}/check").status_code, 404)
        self.assertEqual(self.get(f"/lists/{lst}/table").status_code, 404)
        self.assertEqual(self.get(f"/lists/{lst}/table", AS_CLIENT).status_code, 200)

    def test_search_runs_in_the_chosen_space_and_spend_is_logged(self):
        r = self.post("/runs", {"keywords": KEYS, "enrich": False}, AS_CLIENT)
        self.assertEqual(r.status_code, 202, r.text)
        run_id = r.json()["id"]

        async def state(db):
            run = await db.get(LgSourceRun, run_id)
            companies = (await db.scalars(select(LgCompany.workspace_id))).all()
            rows = (await db.scalars(select(LgSpend))).all()
            return run, companies, rows
        run, companies, rows = self.run_db(state)
        self.assertEqual((run.workspace_id, run.status), (CLIENT, "done"))
        self.assertEqual(set(companies), {CLIENT})    # фоновая часть пишет туда же, где запущена
        self.assertEqual(self.get("/runs").json()["items"], [])
        self.assertEqual(self.get(f"/runs/{run_id}").status_code, 404)
        self.assertEqual([(s.workspace_id, s.kind, s.run_id) for s in rows], [(CLIENT, "yandex_direct", run_id)])
        self.assertGreaterEqual(rows[0].units, len(KEYS))
        theirs, ours = self.get("/spend", AS_CLIENT).json(), self.get("/spend").json()
        self.assertEqual((theirs["units"], theirs["items"][0]["kind"]), (rows[0].units, "yandex_direct"))
        self.assertEqual((ours["units"], ours["items"]), (0, []))

    def test_old_client_campaigns_still_work_from_the_agency(self):
        """«Аутрич как услуга» (client_project_id) заморожен, но работает как раньше: в пространстве агентства."""
        from app.models.marketing import Project

        async def seed(db):
            p = Project(workspace_id=CLIENT, name="OneSpot", is_default=True)
            db.add(p)
            await db.flush()
            return p.id
        project = self.run_db(seed)
        self.assertEqual([c["project_id"] for c in self.get("/clients").json()["items"]], [project])
        r = self.post("/campaigns", {"name": "Для OneSpot", "client_project_id": project})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["client"]["project_id"], project)
        self.assertEqual(self.get("/campaigns", AS_CLIENT).json()["items"], [])   # она живёт у агентства


class SettingsTests(SpaceBase):
    def test_each_space_has_its_own_settings(self):
        fresh = self.get("/settings", AS_CLIENT).json()
        self.assertEqual((fresh["icp"], fresh["main_domains"], fresh["recontact_days"]), ("", ["onespot.one"], 90))
        home = self.get("/settings").json()
        self.assertEqual((home["icp"], home["main_domains"]), (fit_ai.DEFAULT_ICP, ["steptolead.ru"]))

        icp = "Рекламные агентства и фрилансеры, которые ведут кабинеты клиентов."
        r = self.client.put(f"{BASE}/settings", headers=AS_CLIENT, json={"icp": icp, "recontact_days": 30})
        self.assertEqual(r.status_code, 200, r.text)
        theirs, ours = self.get("/settings", AS_CLIENT).json(), self.get("/settings").json()
        self.assertEqual((theirs["icp"], theirs["recontact_days"]), (icp, 30))
        self.assertEqual((ours["icp"], ours["recontact_days"]), (fit_ai.DEFAULT_ICP, 90))

    def test_old_shared_settings_become_the_agency_ones(self):
        async def fn(db):
            db.add(AppSetting(key=service.SETTINGS_KEY, value={"recontact_days": 45, "icp": "Свой портрет агентства " * 2}))
            await db.flush()
            before = await service.get_settings(db, HOME)
            await service.ensure_settings(db, HOME)
            await service.save_settings(db, HOME, {**await service.get_settings(db, HOME), "recontact_days": 60})
            old = await db.get(AppSetting, service.SETTINGS_KEY)
            return before, await service.get_settings(db, HOME), await service.get_settings(db, CLIENT), old.value
        before, after, client, old = self.run_db(fn)
        self.assertEqual((before["recontact_days"], before["icp"]), (45, "Свой портрет агентства " * 2))
        self.assertEqual((after["recontact_days"], after["icp"], after["main_domains"]),
                         (60, "Свой портрет агентства " * 2, ["steptolead.ru"]))
        self.assertEqual((client["recontact_days"], client["icp"], client["main_domains"]), (90, None, None))
        self.assertEqual(old["recontact_days"], 45)   # прежняя запись не тронута — можно откатиться

    def test_no_portrait_no_ai_assessment(self):
        cid = self.company(CLIENT)
        called = []

        async def model(*args, **kwargs):
            called.append(kwargs)
            return '{"label": "fit", "reason": "ведут рекламу", "chain": false}'

        async def fn(db):
            company = await db.get(LgCompany, cid)
            empty = await fit_ai.assess(db, company, now=NOW, complete=model)
            await service.save_settings(db, CLIENT, {"icp": "Рекламные агентства с кабинетами клиентов."})
            return empty, await fit_ai.assess(db, company, now=NOW, complete=model)
        empty, done = self.run_db(fn)
        self.assertEqual((empty["status"], empty["error"]), ("failed", fit_ai.NO_ICP))
        self.assertEqual((done["status"], done["label"]), ("done", "fit"))
        self.assertEqual([k["workspace_id"] for k in called], [CLIENT])   # без портрета ИИ не звали; расход — клиенту

    def test_mailbox_rules(self):
        main = self.post("/mailboxes", {**BOX, "email": "anna@onespot.one"}, AS_CLIENT)
        self.assertEqual(main.status_code, 422)
        self.assertIn("onespot.one", main.json()["detail"])
        self.assertEqual(self.post("/mailboxes", {**BOX, "email": "egor@steptolead.ru"}).status_code, 422)
        self.assertEqual(self.post("/mailboxes", BOX, AS_CLIENT).status_code, 201)
        taken = self.post("/mailboxes", BOX)   # тот же ящик у другого клиента
        self.assertEqual((taken.status_code, taken.json()["detail"]), (409, "Этот ящик уже подключён у другого клиента"))
        # основные домены — настройка: задали свои — правило следует за ними
        r = self.client.put(f"{BASE}/settings", headers=AS_CLIENT, json={"main_domains": ["OneSpot.io", "https://onespot.one/"]})
        self.assertEqual(r.json()["main_domains"], ["onespot.io", "onespot.one"])
        self.assertEqual(self.post("/mailboxes", {**BOX, "email": "anna@onespot.io"}, AS_CLIENT).status_code, 422)
        self.client.put(f"{BASE}/settings", headers=AS_CLIENT, json={"main_domains": []})
        self.assertEqual(self.post("/mailboxes", {**BOX, "email": "anna@onespot.one"}, AS_CLIENT).status_code, 201)


class WorkerTests(SpaceBase):
    async def world(self, db, ws=CLIENT):
        mb = LgMailbox(workspace_id=ws, email=f"anna{ws}@onespot-mail.ru", sender_name="Анна", smtp_host="smtp.x",
                       imap_host="imap.x", login="anna", password_encrypted=encrypt_secret("secret"), daily_limit=30,
                       warmup_started_on=(NOW - timedelta(days=40)).date())
        seq = LgSequence(workspace_id=ws, name="Письмо", steps=STEPS, window={"tz": "Europe/Moscow", "gap_minutes": 0})
        db.add_all([mb, seq])
        company = (await service.upsert_company(db, ws, service.FindingIn(
            source="site", name="Агентство Один", domain="agency-one.ru",
            contacts=[{"kind": "email", "value": "info@agency-one.ru"}]), now=NOW)).company
        await db.flush()
        result = await sequences.enroll(db, seq, company, now=NOW)
        assert result.ok, result.reason
        return result.enrollment_id

    def status(self, ws, value):
        async def fn(db):
            (await db.get(ClientWorkspace, ws)).status = value
        self.run_db(fn)

    def test_switched_off_client_does_not_send_and_loses_nothing(self):
        enrollment = self.run_db(self.world)
        self.status(CLIENT, "deleted")
        transport = Transport()

        async def tick(db):
            stats = await sequences.process_due(db, transport, now=NOW + timedelta(minutes=5))
            enr = await db.get(LgEnrollment, enrollment)
            return stats, enr.status, enr.current_step, [m.email for m in await sequences.active_mailboxes(db)]
        stats, status, step, boxes = self.run_db(tick)
        self.assertEqual((transport.sent, stats["emails"], status, step, boxes), ([], 0, "active", 0, []))

        self.status(CLIENT, "active")   # вернули клиента — цепочка пошла с того же места
        stats, status, step, boxes = self.run_db(tick)
        self.assertEqual((len(transport.sent), stats["emails"], boxes), (1, 1, [f"anna{CLIENT}@onespot-mail.ru"]))

    def test_warmup_and_weekly_repeat_skip_switched_off_client(self):
        async def seed(db):
            for i in (1, 2):
                db.add(LgMailbox(workspace_id=CLIENT, email=f"w{i}@onespot-mail{i}.ru", smtp_host="s", imap_host="i",
                                 login="l", password_encrypted=encrypt_secret("secret"),
                                 warmup_started_on=(NOW - timedelta(days=3)).date()))
            db.add(LgSourceRun(workspace_id=CLIENT, source="yandex_direct", status="done",
                               params={"keywords": KEYS, "repeat": True}, stats={},
                               finished_at=NOW - timedelta(days=8)))
        self.run_db(seed)
        seen = []

        async def spy(db, transport, ws, *, now=None):
            seen.append(ws)
            return 0
        original, warmup.send_due = warmup.send_due, spy
        self.addCleanup(setattr, warmup, "send_due", original)

        async def tick(db):
            await warmup.run_tick(db, Transport(), now=NOW)
            return [r.workspace_id for r in await scheduler.due_runs(db, NOW)]
        self.assertEqual((self.run_db(tick), seen), ([CLIENT], [CLIENT]))
        self.status(CLIENT, "deleted")
        seen.clear()
        self.assertEqual((self.run_db(tick), seen), ([], []))


class LinksAndSpendTests(SpaceBase):
    def test_hot_links_and_client_name(self):
        async def fn(db):
            return hot._link(LgTouch(id=5), CLIENT), await hot._client_line(db, CLIENT)
        link, line = self.run_db(fn)
        self.assertTrue(link.endswith(f"/admin/outreach/inbox?touch=5&ws={CLIENT}"), link)
        self.assertEqual(line, "Клиент: OneSpot")

    def test_hot_settings_are_per_space(self):
        async def fn(db):
            await service.save_settings(db, CLIENT, {"hot": {"threshold": 40, "remind": False}})
            return await hot.config(db, CLIENT), await hot.config(db, HOME)
        theirs, ours = self.run_db(fn)
        self.assertEqual((theirs["threshold"], theirs["remind"]), (40, False))
        self.assertEqual((ours["threshold"], ours["remind"]), (hot.DEFAULTS["threshold"], hot.DEFAULTS["remind"]))

    def test_spend_journal(self):
        async def fn(db):
            await spend.record(db, CLIENT, "find_site", {"requests": 40, "rub": 1.0}, now=NOW)
            await spend.record(db, CLIENT, "find_site", {"requests": 10, "rub": 0.25}, now=NOW)
            await spend.record(db, CLIENT, "email_by_inn", {"requests": 5, "rub": None}, now=NOW)   # цена не задана
            await spend.record(db, HOME, "serp", {"requests": 7, "rub": 0.18}, now=NOW)
            free = [await spend.record(db, CLIENT, "site_contacts", None, now=NOW),
                    await spend.record(db, CLIENT, "dadata", {"requests": 0, "rub": 0}, now=NOW)]
            return free, await spend.summary(db, CLIENT, now=NOW), await spend.summary(db, HOME, now=NOW)
        free, theirs, ours = self.run_db(fn)
        self.assertEqual(free, [None, None])   # бесплатное в журнал не идёт
        self.assertEqual((theirs["units"], theirs["rub"]), (55, 1.25))
        self.assertEqual(theirs["items"], [{"kind": "email_by_inn", "units": 5, "rub": 0.0, "unpriced_runs": 1},
                                           {"kind": "find_site", "units": 50, "rub": 1.25, "unpriced_runs": 0}])
        self.assertEqual((ours["units"], ours["rub"]), (7, 0.18))

    def test_move_company_to_other_client_agency_only(self):
        cid = self.company(HOME, domain="moved-one.ru")
        self.company(CLIENT, domain="already-there.ru")
        dup = self.company(HOME, domain="already-there.ru")
        r = self.post("/companies/move", {"ids": [cid, dup], "workspace_id": CLIENT})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["done"], 1)
        self.assertEqual(r.json()["skipped"][0]["reason_text"], "такая компания у клиента уже есть")
        self.assertEqual(self.get(f"/companies/{cid}", headers=AS_CLIENT).status_code, 200)
        self.assertEqual(self.get(f"/companies/{cid}").status_code, 404)
        self.assertEqual(self.post("/companies/move", {"ids": [cid], "workspace_id": GONE}, headers=AS_CLIENT).status_code, 404)
        self.client.cookies.clear()
        self.assertIn(self.post("/companies/move", {"ids": [cid], "workspace_id": HOME}, headers=AS_CLIENT).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
