"""Сканеры радара: шаблоны, несколько запросов, общий запрос к площадкам, действия (база, CRM клиента, Telegram),
проверки при включении, предпросмотр черновиков, лента, перенос старых фильтров. Сеть не трогаем."""
import json
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.router import api_router
from app.core.access import COOKIE
from app.core.config import settings
from app.db import get_db
from app.domains.jobradar import crm, platforms, runner, scanners
from app.domains.jobradar.models import JrFilter, JrMatch, JrQuery
from app.domains.jobradar.sources import REGISTRY, Vacancy
from app.models.crm import CrmActivity, CrmDeal, CrmPipeline, CrmStage, CrmTask
from app.models.marketing import ClientWorkspace, PortalUser, Project, ProjectSource
from test_jobradar import MOMENT, respond
from test_leadgen_sequences import ORIGIN, FlowBase

SCHEDULE = {"timezone": "Europe/Moscow", "days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}


async def noop(*_):
    return None


class ScannerTests(FlowBase):
    def setUp(self):
        super().setUp()
        self._old = (settings.agency_workspace_id, runner.session_factory, runner.send, runner.sleep,
                     platforms.transport, platforms.sleep)
        settings.agency_workspace_id = 7
        runner.session_factory = self.sessions
        self.calls: list = []
        platforms.transport = httpx.MockTransport(lambda r: respond(r, None, self.calls))
        platforms.sleep = noop
        runner.sleep = noop
        self.sent = []

        async def send(chat, text, markup=None):
            self.sent.append((chat, text))
            return len(self.sent)
        runner.send = send

        async def seed(db):
            db.add(ClientWorkspace(id=8, name="Кухни Про"))
            await db.flush()
            db.add(Project(id=80, workspace_id=8, name="Кухни Про", portal_state={"work_schedule": SCHEDULE}))
            db.add(PortalUser(id=81, workspace_id=8, username="o@kp.ru", password_hash="x", display_name="Ольга",
                              role="client_owner"))
            db.add(PortalUser(id=82, workspace_id=8, username="r@kp.ru", password_hash="x", display_name="Роман",
                              role="sales_head"))
            rows = await platforms.ensure(db)
            platforms.apply(REGISTRY["hh"], rows["hh"], {"token": "test"})
        self.run_db(seed)

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.c = TestClient(app)
        self.c.cookies.set(COOKIE, "a" * 43)
        self.b = "/api/admin/jobradar"

    def tearDown(self):
        (settings.agency_workspace_id, runner.session_factory, runner.send, runner.sleep,
         platforms.transport, platforms.sleep) = self._old
        super().tearDown()

    def run_scanner(self, scanner_id):
        async def fn(db):
            return await runner.run_filter(db, await db.get(JrFilter, scanner_id), moment=MOMENT)
        return self.run_db(fn)

    def scanner(self, **body):
        r = self.c.post(f"{self.b}/scanners", headers=ORIGIN, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # ------------------------------------------------------------ шаблоны

    def test_templates_and_create_for_agency_and_client(self):
        codes = [t["code"] for t in self.c.get(f"{self.b}/templates").json()["items"]]
        self.assertEqual(codes, ["hiring_marketing", "hiring_sales", "jobs_for_me", "freelance", "blank"])
        agency = self.scanner(name="Лиды Самара", template="hiring_marketing")
        self.assertEqual((agency["owner"], agency["actions"]["companies"]["enabled"], len(agency["query_items"])),
                         (None, True, 1))
        self.assertEqual(agency["actions"]["companies"]["list_name"], "Нанимает маркетолога")
        client = self.scanner(name="Для Кухни Про", template="hiring_marketing", project_id=80)
        self.assertEqual(client["owner"]["project_id"], 80)
        self.assertEqual((client["actions"]["companies"]["enabled"], client["actions"]["client_crm"]["enabled"]),
                         (False, True))   # клиенту — в его CRM, а не в базу агентства
        self.assertEqual(self.c.post(f"{self.b}/scanners", headers=ORIGIN,
                                     json={"name": "x2", "template": "nope"}).status_code, 422)
        self.assertEqual(self.c.post(f"{self.b}/scanners", headers=ORIGIN,
                                     json={"name": "x3", "project_id": 7}).status_code, 422)   # агентство — не клиент

    # ------------------------------------------------------------ проверки при включении

    def test_enable_validation(self):
        s = self.scanner(name="Пустой", template="blank")
        bad = self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={"enabled": True})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("ключевого слова", bad.json()["detail"])
        q = s["query_items"][0]["id"]
        self.c.patch(f"{self.b}/queries/{q}", headers=ORIGIN, json={"keywords": ["маркетолог"]})
        self.assertIn("действие", self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN,
                                               json={"enabled": True}).json()["detail"])
        self.assertIn("Telegram-чат", self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={
            "actions": {"telegram": {"enabled": True}}}).json()["detail"])
        self.assertIn("только для сканера клиента", self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={
            "actions": {"client_crm": {"enabled": True}}}).json()["detail"])
        ok = self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={
            "enabled": True, "actions": {"telegram": {"enabled": True, "chat_id": "-1001"}}}).json()
        self.assertEqual((ok["enabled"], ok["actions"]["telegram"]["chat_id"], ok["actions"]["client_crm"]["enabled"]),
                         (True, "-1001", False))
        client = self.scanner(name="Клиент", template="hiring_marketing", project_id=80)
        self.assertIn("только для сканеров агентства", self.c.patch(f"{self.b}/scanners/{client['id']}", headers=ORIGIN,
                      json={"actions": {"companies": {"enabled": True}}}).json()["detail"])
        self.assertIn("из команды клиента", self.c.patch(f"{self.b}/scanners/{client['id']}", headers=ORIGIN, json={
            "actions": {"client_crm": {"enabled": True, "responsible_user_id": 1}}}).json()["detail"])
        # последний запрос удалили — сканер выключается, а не ломается
        self.assertEqual(self.c.delete(f"{self.b}/queries/{q}", headers=ORIGIN).status_code, 204)
        self.assertFalse(self.c.get(f"{self.b}/scanners/{s['id']}").json()["enabled"])

    # ------------------------------------------------------------ несколько запросов + CRM клиента

    def test_two_queries_one_fetch_and_client_crm(self):
        s = self.scanner(name="Кухни Про: кто нанимает", template="blank", project_id=80)
        first = s["query_items"][0]["id"]
        self.c.patch(f"{self.b}/queries/{first}", headers=ORIGIN, json={
            "name": "Маркетолог", "keywords": ["маркетолог"], "exclude": ["senior", "мерчендайзер"]})
        second = self.c.post(f"{self.b}/scanners/{s['id']}/queries", headers=ORIGIN, json={
            "name": "Директолог", "keywords": ["директолог"]}).json()
        r = self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={
            "sources": ["hh", "trudvsem"], "enabled": True,
            "actions": {"client_crm": {"enabled": True, "responsible_user_id": 82}}})
        self.assertEqual(r.status_code, 200, r.text)
        result = self.run_scanner(s["id"])
        hh_calls = [c for c in self.calls if c.url.host == "api.hh.ru" and c.url.path == "/vacancies"]
        self.assertEqual(len(hh_calls), 1)                                  # одна выборка на сканер, не на запрос
        self.assertIn('"директолог"', hh_calls[0].url.params["text"])     # слова всех запросов вместе
        self.assertEqual(result.deals, 1)          # Ромашка: телефон и почта есть в вакансии; у Гаммы контактов нет
        self.assertEqual({v["name"]: v["passed"] for v in result.by_query.values()}, {"Маркетолог": 1, "Директолог": 1})

        async def check(db):
            deals = (await db.scalars(select(CrmDeal).where(CrmDeal.project_id == 80))).all()
            pipeline = await db.get(CrmPipeline, deals[0].pipeline_id)
            source = await db.get(ProjectSource, deals[0].source_id)
            tasks = (await db.scalars(select(CrmTask).where(CrmTask.project_id == 80))).all()
            matches = (await db.execute(select(JrMatch.status, JrMatch.query_id).where(JrMatch.status == "deal"))).all()
            return deals, pipeline.name, source.name, tasks, matches
        deals, pipeline, source, tasks, matches = self.run_db(check)
        self.assertEqual((len(deals), pipeline, source), (1, "Радар", "Радар вакансий"))
        self.assertEqual({t.responsible_user_id for t in tasks}, {82})
        self.assertEqual({q for _, q in matches}, {first})
        feed = self.c.get(f"{self.b}/scanners/{s['id']}/feed", params={"status": "deal"}).json()
        self.assertEqual([i["query"] for i in feed["items"]], ["Маркетолог"])
        self.assertTrue(feed["items"][0]["deal_id"])
        found = self.c.get(f"{self.b}/scanners/{s['id']}/feed", params={"status": "found"}).json()["items"]
        self.assertEqual([(i["query"], i["vacancy"]["company"]) for i in found], [("Директолог", "Гамма")])
        self.assertIn("нет телефона и почты", found[0]["reason"])        # сигнал не потерян, видно почему не в CRM
        short = next(x for x in self.c.get(f"{self.b}/scanners").json()["items"] if x["id"] == s["id"])
        self.assertEqual((short["stats"]["deals_7d"], short["actions"], short["queries"]), (1, ["client_crm"], 2))

        async def contact(db):
            from app.models.crm import CrmContact
            return await db.scalar(select(CrmContact).where(CrmContact.project_id == 80))
        c = self.run_db(contact)
        self.assertEqual((c.phones, c.emails), (["78462000000"], ["hr@romashka.example"]))

    def test_same_company_goes_to_open_deal(self):
        s = self.scanner(name="Кухни", template="hiring_marketing", project_id=80)

        async def fn(db):
            from app.domains.leadgen.models import LgCompany, LgContact
            from app.models.crm import CrmContact
            base = LgCompany(workspace_id=7, display_name="Ромашка", inn="7707083893")
            db.add(base)
            await db.flush()
            db.add_all([LgContact(company_id=base.id, kind="email", value="ivan@romashka.example",
                                  value_norm="ivan@romashka.example", is_personal=True),
                        LgContact(company_id=base.id, kind="email", value="info@romashka.example",
                                  value_norm="info@romashka.example")])
            f = await db.get(JrFilter, s["id"])
            nobody = Vacancy(source="hh", ext_id="0", title="Маркетолог", company="Неизвестная", url="https://hh.ru/0")
            none_id, none_action = await crm.push(db, f, [nobody], responsible_user_id=None, query_name="Q", score=None,
                                                  why="", now=MOMENT)
            a = Vacancy(source="trudvsem", ext_id="1", title="Маркетолог", company="ООО Ромашка",
                        company_inn="7707083893", url="https://trudvsem.ru/vacancy/1")
            b = Vacancy(source="hh", ext_id="2", title="Таргетолог", company="Ромашка", url="https://hh.ru/vacancy/2",
                        company_inn="7707083893")
            first, action1 = await crm.push(db, f, [a], responsible_user_id=None, query_name="Q", score=None, why="",
                                            now=MOMENT)
            db.add(JrMatch(filter_id=f.id, fingerprint="x", vacancy_id=0, status="deal", deal_id=first,
                           company_key=crm.company_key(a)))
            await db.flush()
            second, action2 = await crm.push(db, f, [b], responsible_user_id=None, query_name="Q", score=None, why="",
                                             now=MOMENT)
            deals = await db.scalar(select(func.count(CrmDeal.id)).where(CrmDeal.project_id == 80))
            notes = await db.scalar(select(func.count(CrmActivity.id)).where(CrmActivity.deal_id == first))
            owner = await db.scalar(select(CrmTask.responsible_user_id))
            emails = await db.scalar(select(CrmContact.emails))
            return (none_id, none_action), action1, action2, first == second, deals, notes, owner, emails
        nothing, a1, a2, same, deals, notes, owner, emails = self.run_db(fn)
        self.assertEqual(nothing, (None, "no_contact"))                # без телефона и почты сделку не создаём
        self.assertEqual(emails, ["info@romashka.example"])            # из базы агентства, личный адрес не берём
        self.assertEqual((a1, a2, same, deals), ("added", "appended", True, 1))
        self.assertGreaterEqual(notes, 2)
        self.assertEqual(owner, 81)                                     # по умолчанию — собственник клиента

    # ------------------------------------------------------------ база компаний + Telegram, предпросмотр

    def test_companies_with_own_list_and_signal_plus_telegram(self):
        s = self.scanner(name="РОПы", template="blank")
        q = s["query_items"][0]["id"]
        self.c.patch(f"{self.b}/queries/{q}", headers=ORIGIN, json={"keywords": ["директолог", "маркетолог"],
                                                                     "exclude": ["senior", "мерчендайзер"]})
        self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={
            "sources": ["hh"], "enabled": True,
            "actions": {"companies": {"enabled": True, "list_name": "Ищут РОПа", "signal": "hiring"},
                        "telegram": {"enabled": True, "chat_id": "-1009"}}})
        result = self.run_scanner(s["id"])
        self.assertEqual((result.leads, result.sent), (2, 2))
        self.assertTrue(all("📇" in t for _, t in self.sent))

        async def check(db):
            from app.domains.leadgen.models import LgList, LgSignal
            lst = await db.scalar(select(LgList.name).where(LgList.kind == "jobradar"))
            kinds = set((await db.scalars(select(LgSignal.kind))).all())
            return lst, kinds
        lst, kinds = self.run_db(check)
        self.assertEqual(lst, "Ищут РОПа")
        self.assertIn("hiring", kinds)

    def test_preview_drafts_write_nothing(self):
        s = self.scanner(name="Черновики", template="blank")
        self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={"sources": ["trudvsem"]})
        pv = self.c.post(f"{self.b}/scanners/{s['id']}/preview", headers=ORIGIN, json={"days": 7, "queries": [
            {"name": "Маркетолог", "keywords": ["маркетолог"], "exclude": ["мерчендайзер"]},
            {"name": "Директолог", "keywords": ["директолог"]}]}).json()
        self.assertEqual([q["name"] for q in pv["by_query"]], ["Маркетолог", "Директолог"])
        self.assertEqual(pv["matched"], 1)
        self.assertEqual(next(i for i in pv["items"] if i["ok"])["query"], "Маркетолог")
        self.assertEqual(self.c.get(f"{self.b}/scanners/{s['id']}/feed").json()["total"], 0)
        self.assertEqual(self.c.post(f"{self.b}/scanners/{s['id']}/preview", headers=ORIGIN,
                                     json={"days": 3}).status_code, 422)   # в сохранённом запросе нет слов

    # ------------------------------------------------------------ перенос старых фильтров

    def test_legacy_filter_becomes_scanner(self):
        async def seed(db):
            f = JrFilter(workspace_id=7, name="Старый", mode="leads", keywords=["маркетолог"], exclude=["senior"],
                         regions=["Самара"], remote_only=False, salary_from=0, allow_no_salary=True, experience=[],
                         sources=["trudvsem"], chat_id="-100500", ai_min_score=0, enabled=True, last_result={},
                         source_settings={})
            db.add(f)
            await db.flush()
            return f.id
        fid = self.run_db(seed)
        items = {x["id"]: x for x in self.c.get(f"{self.b}/scanners").json()["items"]}
        self.assertEqual(sorted(items[fid]["actions"]), ["companies", "telegram"])
        full = self.c.get(f"{self.b}/scanners/{fid}").json()
        q = full["query_items"][0]
        self.assertEqual((q["name"], q["keywords"], q["regions"], full["actions"]["telegram"]["chat_id"]),
                         ("Основной", ["маркетолог"], ["Самара"], "-100500"))

        async def count(db):
            return await db.scalar(select(func.count(JrQuery.id)).where(JrQuery.filter_id == fid))
        self.c.get(f"{self.b}/scanners")
        self.assertEqual(self.run_db(count), 1)                      # перенос не дублирует запрос

    def test_channels_by_scanner_id(self):
        s = self.scanner(name="Каналы", template="blank")
        added = self.c.post(f"{self.b}/channels", headers=ORIGIN, json={"channel": "@jobs_samara", "scanner_id": s["id"]})
        self.assertEqual(added.status_code, 200, added.text)
        listed = self.c.get(f"{self.b}/channels").json()["items"]
        self.assertEqual(listed[0]["scanners"], [{"id": s["id"], "name": "Каналы"}])
        left = self.c.delete(f"{self.b}/channels/jobs_samara", params={"scanner_id": s["id"]}, headers=ORIGIN).json()
        self.assertEqual(left["source_settings"]["telegram"]["channels"], [])

    def test_merged_query(self):
        q1 = scanners.query_dict({"keywords": ["a"], "regions": ["Самара"], "remote_only": True, "salary_from": 50000,
                                  "experience": ["noExperience"]}, strict_city=True)
        q2 = scanners.query_dict({"keywords": ["b", "a"], "regions": [], "remote_only": False, "salary_from": 0},
                                 strict_city=True)
        m = scanners.merged([q1, q2])
        self.assertEqual((m["keywords"], m["regions"], m["remote_only"], m["salary_from"], m["experience"]),
                         (["a", "b"], [], False, 0, []))


class ParsingTests(FlowBase):
    def test_contacts_and_spaces_from_platforms(self):
        from app.domains.jobradar.sources import hh, trudvsem
        tv = trudvsem.parse({"meta": {"total": 1}, "results": {"vacancies": [{"vacancy": {
            "id": "x1", "job-name": "Менеджер  по   маркетингу", "company": {"name": "ФГБУЗ   МРЦ", "inn": "6381000000"},
            "region": {"name": "Самарская область", "region_code": "6300000000000"},
            "contact_list": [{"contact_type": "Телефон", "contact_value": "+7 937 000-00-00"},
                             {"contact_type": "Эл. почта", "contact_value": "hr@mrc.example"}],
            "contact_person": "Иванова Анна"}}]}})[0]
        self.assertEqual((tv.title, tv.company), ("Менеджер по маркетингу", "ФГБУЗ МРЦ"))
        self.assertEqual((tv.employer["phones"], tv.employer["emails"]), (["+7 937 000-00-00"], ["hr@mrc.example"]))
        self.assertNotIn("Иванова", json.dumps(tv.employer, ensure_ascii=False))   # ФИО не берём
        item = hh.parse_item({"id": "9", "name": "SMM", "employer": {"id": "1", "name": "Альфа"}, "area": {"name": "Самара"},
                              "contacts": {"email": "job@alfa.example", "phones": [{"country": "7", "city": "846",
                                                                                     "number": "2000000"}]}})
        self.assertEqual((item.employer["phones"], item.employer["emails"]), (["78462000000"], ["job@alfa.example"]))
        self.assertNotIn("phones", hh.parse_item({"id": "8", "name": "SMM", "employer": {"name": "Бета"}}).employer)

    def test_empty_platform_reports_zeros(self):
        async def fn(db):
            f = JrFilter(workspace_id=7, name="Пусто", mode="work", keywords=["маркетолог"], exclude=[], regions=[],
                         remote_only=False, salary_from=0, allow_no_salary=True, experience=[], sources=["trudvsem"],
                         ai_min_score=0, enabled=True, last_result={}, source_settings={})
            db.add(f)
            await db.flush()
            await platforms.ensure(db)
            ctx = await platforms.context(db, first_run_days=1)
            ctx.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"meta": {"total": 0}}))
            _, by_source, _ = await runner.collect(db, f, MOMENT, ctx, MOMENT, record=False,
                                                   query=scanners.merged([scanners.query_dict(
                                                       {"keywords": ["маркетолог"]}, strict_city=False)]),
                                                   checks=[])
            return by_source["trudvsem"]
        self.assertEqual(self.run_db(fn), {"state": "ok", "found": 0, "passed": 0, "dropped": 0})
