import asyncio
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from sqlalchemy import select, func
from app.core.access import COOKIE
from app.core.config import settings
from app.models.brief import ClientBrief, ExpressAssessment
from app.models.crm import CrmInbound
from app.models.marketing import ClientWorkspace, Project
from app.services import brief, ai
import test_stage2


class BriefTests(unittest.TestCase):
    tearDown, as_user, run_db = test_stage2.Stage2Tests.tearDown, test_stage2.Stage2Tests.as_user, test_stage2.Stage2Tests.run_db

    def setUp(self):
        test_stage2.Stage2Tests.setUp(self)
        p = patch.object(settings, "agency_workspace_id", 1); p.start(); self.addCleanup(p.stop)
        p = patch.object(settings, "brief_privacy_url", "/test-privacy"); p.start(); self.addCleanup(p.stop)
        p = patch.object(settings, "brief_demo_daily_cap", 30); p.start(); self.addCleanup(p.stop)
        p = patch.object(ai, "configured", return_value=False); p.start(); self.addCleanup(p.stop)
        from app.api.routes import brief as route
        p = patch.object(route, "rate_limit", AsyncMock()); self.limiter=p.start(); self.addCleanup(p.stop)
        # Вход в портал внутри теста тоже ограничен по частоте: без подмены тест требует живой Redis.
        p = patch("app.api.routes.portal.rate_limit", AsyncMock()); p.start(); self.addCleanup(p.stop)
        self.original_alert=route.admin_alert
        p = patch.object(route, "admin_alert", AsyncMock()); self.alert=p.start(); self.addCleanup(p.stop)
        self.answers = {k: "Нет данных" for k,q in brief.QUESTIONS.items() if q["required"]}
        self.answers.update({"company.name_site":"Тестовая стоматология · https://example.test", "company.activity":"Стоматология",
            "ai.tone":"На вы, спокойно", "ai.goal":"Уточнить запрос", "ai.prices":"Цены только после осмотра", "ai.prohibitions":"Не обещать результат"})

    def payload(self):
        return {"answers":{"what":"Стоматология","city":"Самара","check":"5000","conv":"2","pay":"1500",
            "leads":"100","cpl":"400","pain":["Заявки дорогие"],"channels":["Яндекс Директ"],
            "budget":"До 50 тыс. ₽","speed":"До 5 минут","goal":"40 продаж","start":"В этом месяце","dm":"Я сам",
            "name":"Тестовый клиент","company":"Тестовая стоматология","contact":"@test_brief"},
            "consent":True,"utm":{"utm_source":"website","utm_campaign":"brief_test"},"referrer":"https://example.test/start"}

    def admin(self):
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a"*43)

    async def fake_demo(self, db, *, commit=True):
        from app.services.project_scope import default_project
        ws = ClientWorkspace(name="Демо тест", plan="system"); db.add(ws); await db.flush()
        p = await default_project(db, ws.id); p.status="demo"
        return {"workspace_id":ws.id, "username":"demo-test", "password":"one-time-password"}

    def submit(self, **changes):
        from app.api.routes import brief as route
        payload={**self.payload(),**changes}
        with patch.object(route.demo,"create_demo",self.fake_demo):
            return self.client.post("/api/brief/submit",headers=test_stage2.ORIGIN,json=payload)

    def test_required_consent_contact_origin_honeypot(self):
        for key,q in brief.QUESTIONS.items():
            if q["required"]:
                with self.assertRaises(HTTPException):
                    brief.clean_answers({k:v for k,v in self.answers.items() if k!=key},True)
        for change in ({"consent":False},{"website":"bot.test"},{"answers":{**self.payload()["answers"],"contact":"invalid"}},{"answers":{}}):
            self.assertEqual(self.submit(**change).status_code,422)
        self.assertEqual(self.client.post("/api/brief/submit",json=self.payload()).status_code,403)
        self.assertEqual(self.run_db(lambda db:db.scalar(select(func.count(CrmInbound.id)))),0)

    def test_public_lead_demo_knowledge_and_utms(self):
        response=self.submit();self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()["demo"]["username"],"demo-test")
        self.alert.assert_awaited_once_with(response.json()["assessment_id"])
        async def check(db):
            row=await db.scalar(select(ExpressAssessment));p=await db.scalar(select(Project).where(Project.workspace_id==row.demo_workspace_id));lead=await db.get(CrmInbound,row.lead_id)
            self.assertEqual(lead.workspace_id,1);self.assertEqual(row.limit_cpl,300)
            self.assertIn("Стоматология",p.portal_state["ai"]["knowledge"])
            self.assertEqual(p.portal_state["baseline"]["conversion"],20)
            self.assertEqual(lead.raw_payload["brief_answers"],self.payload()["answers"])
            self.assertEqual(lead.attribution["utm_source"],"website")
            self.assertEqual(lead.raw_payload["assessment_id"],row.id)
            access=lead.raw_payload["brief_demo_access"]
            self.assertEqual(access["username"],"demo-test")
            self.assertEqual(access["reset_url"],settings.frontend_origin.rstrip("/")+"/portal/reset?username=demo-test")
            self.assertNotIn("password",access)
            self.assertNotIn("one-time-password",json.dumps(lead.raw_payload))
            self.assertAlmostEqual((row.expires_at.replace(tzinfo=timezone.utc)-row.created_at.replace(tzinfo=timezone.utc)).total_seconds()/86400,14,places=3)
        self.run_db(check)

    def test_global_demo_cap_keeps_lead_and_does_not_create_another_demo(self):
        from app.api.routes import brief as route
        with patch.object(settings,"brief_demo_daily_cap",1):
            first=self.submit()
            self.assertEqual(first.status_code,201,first.text)
            second_payload=self.payload()
            second_payload["answers"]["contact"]="@another_brief"
            # A different IP must still share the same global allowance.
            with patch.object(route,"client_address",return_value="192.0.2.42"),patch.object(route.demo,"create_demo",AsyncMock()) as create:
                second=self.client.post("/api/brief/submit",headers=test_stage2.ORIGIN,json=second_payload)
            self.assertEqual(second.status_code,201,second.text)
            self.assertIsNone(second.json()["demo"])
            self.assertIsNone(second.json()["expires_at"])
            create.assert_not_awaited()
            async def check(db):
                self.assertEqual(await db.scalar(select(func.count(CrmInbound.id))),2)
                self.assertEqual(await db.scalar(select(func.count(ExpressAssessment.id))),2)
                row=await db.get(ExpressAssessment,second.json()["assessment_id"])
                self.assertIsNone(row.demo_workspace_id)
                lead=await db.get(CrmInbound,row.lead_id)
                self.assertEqual(lead.workspace_id,1)
                self.assertNotIn("brief_demo_access",lead.raw_payload)
            self.run_db(check)
        self.assertEqual(self.alert.await_count,2)

    def test_zero_demo_cap_still_accepts_leads(self):
        with patch.object(settings,"brief_demo_daily_cap",0):
            for i in range(4):
                r=self.submit(answers={**self.payload()["answers"],"contact":f"@brief_user_{i}"})
                self.assertEqual(r.status_code,201,r.text)
                self.assertIsNone(r.json()["demo"])
        self.assertEqual(self.run_db(lambda db:db.scalar(select(func.count(CrmInbound.id)))),4)

    def test_ip_limit_applies_even_when_demo_cap_is_exhausted(self):
        hits={}
        async def limiter(request,scope,limit,seconds):
            hits[scope]=hits.get(scope,0)+1
            if hits[scope]>limit: raise HTTPException(429,"Слишком много запросов. Попробуйте позже.")
        self.limiter.side_effect=limiter
        with patch.object(settings,"brief_demo_daily_cap",0):
            for i in range(5):
                r=self.submit(answers={**self.payload()["answers"],"contact":f"@brief_ip_{i}"})
                self.assertEqual(r.status_code,201,r.text)
            r=self.submit(answers={**self.payload()["answers"],"contact":"@brief_ip_extra"})
            self.assertEqual(r.status_code,429,r.text)
        self.assertEqual(self.run_db(lambda db:db.scalar(select(func.count(CrmInbound.id)))),5)
        self.assertEqual(self.limiter.await_args.args[1:],("brief_submit",5,3600))

    def test_telegram_alert_contains_client_numbers_and_lead_link(self):
        from app.api.routes import brief as route
        from app.services import monitor
        aid=self.submit().json()["assessment_id"]
        send=AsyncMock()
        with patch.object(route,"SessionLocal",self.sessions),patch.object(monitor,"send",send):
            asyncio.run(self.original_alert(aid))
        send.assert_awaited_once()
        message=send.call_args.args[1]
        for value in ("Тестовый клиент","Стоматология","До 50 тыс. ₽","300 ₽","400 ₽","/crm?project_id=1"):
            self.assertIn(value,message)

    def test_ip_limit_three_and_unconfigured_agency(self):
        ip_hash=hashlib.sha256((settings.encryption_secret+":brief:testclient").encode()).hexdigest()
        async def seed(db):
            for _ in range(3):db.add(ClientBrief(answers={},source="public",ip_hash=ip_hash))
            await db.commit()
        self.run_db(seed)
        self.assertEqual(self.submit().status_code,429)
        with patch.object(settings,"agency_workspace_id",0):self.assertEqual(self.submit().status_code,503)

    def test_preview_single_call_history_and_stale_preview(self):
        self.as_user(1)
        row=self.client.post("/api/brief/projects/1",headers=test_stage2.ORIGIN,json={"answers":self.answers}).json()
        data={"knowledge":"Только факты компании", "tone":"На вы", "goal":"Уточнить", "checklist":[]}
        with patch.object(ai,"configured",return_value=True),patch.object(ai,"complete",AsyncMock(return_value=json.dumps(data))) as complete:
            preview=self.client.post(f"/api/brief/projects/1/{row['id']}/preview",headers=test_stage2.ORIGIN)
            self.assertEqual(preview.status_code,200,preview.text);complete.assert_awaited_once()
        p=self.run_db(lambda db:db.get(Project,1));self.assertNotEqual((p.portal_state or {}).get("ai",{}).get("knowledge"),data["knowledge"])
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/apply",headers=test_stage2.ORIGIN).status_code,409)
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/confirm",headers=test_stage2.ORIGIN).status_code,200)
        for _ in range(7):
            response=self.client.post(f"/api/brief/projects/1/{row['id']}/apply",headers=test_stage2.ORIGIN)
            self.assertEqual(response.status_code,200,response.text)
        p=self.run_db(lambda db:db.get(Project,1));self.assertEqual(len(p.portal_state["ai_history"]),5)
        changed={**self.answers,"ai.goal":"Другая цель"}
        self.client.put(f"/api/brief/projects/1/{row['id']}",headers=test_stage2.ORIGIN,json={"answers":changed})
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/apply",headers=test_stage2.ORIGIN).status_code,409)

    def test_portal_scope_and_upload_md_docx(self):
        self.as_user(1)
        md=brief.markdown({"company.activity":"Стоматология","ai.tone":"На вы"})
        for filename,content in [("brief.md",md.encode()),("brief.docx",self.docx(md))]:
            r=self.client.post("/api/brief/projects/1/upload",headers=test_stage2.ORIGIN,files={"file":(filename,content)})
            self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()["answers"]["company.activity"],"Стоматология")
            self.assertEqual(r.json()["source"],"upload")
        async def other(db):
            ws=ClientWorkspace(name="Чужая компания");db.add(ws);await db.flush();p=Project(workspace_id=ws.id,name="Чужой");db.add(p);await db.commit();return p.id
        pid=self.run_db(other)
        self.assertIn(self.client.get(f"/api/brief/projects/{pid}").status_code,(403,404))
        self.as_user(2)
        self.assertEqual(self.client.post("/api/brief/projects/1",headers=test_stage2.ORIGIN,json={"answers":{}}).status_code,403)

    def docx(self,text):
        from docx import Document
        doc=Document()
        for line in text.splitlines():doc.add_paragraph(line)
        output=io.BytesIO();doc.save(output);return output.getvalue()

    def test_import_llm_and_invalid_upload(self):
        with patch.object(ai,"configured",return_value=True),patch.object(ai,"complete",AsyncMock(return_value=json.dumps({"company.activity":"Стоматология"}))):
            self.assertEqual(asyncio.run(brief.import_answers("Занимаемся стоматологией",1)),{"company.activity":"Стоматология"})
        for content,filename in [(b"bad","file.exe"),(b"bad","file.pdf"),(b"x"*(5*1024*1024+1),"file.txt")]:
            with self.assertRaises(HTTPException):brief.extract_text(content,filename)

    def test_ttl_archive_and_admin_listing(self):
        response=self.submit();aid=response.json()["assessment_id"]
        self.admin()
        listing=self.client.get("/api/admin/briefs/assessments/list")
        self.assertEqual(listing.status_code,200,listing.text)
        self.assertEqual(listing.json()[0]["id"],aid)
        async def check(db):
            row=await db.get(ExpressAssessment,aid)
            row.expires_at=datetime.now(timezone.utc)-timedelta(days=1);await db.commit()
            self.assertEqual(await brief.expire_demos(db),1)
            self.assertEqual((await db.get(ClientWorkspace,row.demo_workspace_id)).status,"deleted")
            self.assertEqual((await db.get(ClientWorkspace,1)).status,"active")
            self.assertEqual(await brief.expire_demos(db),0)
        self.run_db(check)

    def test_public_demo_failure_rolls_back_lead(self):
        from app.api.routes import brief as route
        with patch.object(route.demo,"create_demo",AsyncMock(side_effect=RuntimeError("demo failed"))):
            with self.assertRaises(RuntimeError):self.client.post("/api/brief/submit",headers=test_stage2.ORIGIN,json=self.payload())
        self.assertEqual(self.run_db(lambda db:db.scalar(select(func.count(CrmInbound.id)))),0)

    def test_preview_hash_prevents_concurrent_changed_answers(self):
        async def check(db):
            project=await db.get(Project,1)
            row=ClientBrief(workspace_id=1,project_id=1,answers=self.answers);db.add(row);await db.flush()
            await brief.apply_to_ai(db,row,project)
            row.answers={**self.answers,"ai.goal":"Обновили пока ИИ думал"}
            await db.commit()
            with self.assertRaises(HTTPException) as error:await brief.apply_to_ai(db,row,project,apply=True)
            self.assertEqual(error.exception.status_code,409)
        self.run_db(check)

    def test_long_llm_input_rejected_without_silent_truncation(self):
        post=AsyncMock()
        with patch.object(ai,"configured",return_value=True),patch.object(ai,"complete",post),patch.object(settings,"llm_max_input_chars",100):
            with self.assertRaises(HTTPException):asyncio.run(brief.import_answers("x"*1000,1))
        post.assert_not_awaited()

    def test_public_llm_failure_preserves_capture_and_deterministic_knowledge(self):
        with patch.object(ai,"configured",return_value=True),patch.object(ai,"complete",AsyncMock(side_effect=ai.AIError("Провайдер недоступен"))):
            response=self.submit()
        self.assertEqual(response.status_code,201,response.text)
        async def check(db):
            row=await db.scalar(select(ExpressAssessment));project=await db.scalar(select(Project).where(Project.workspace_id==row.demo_workspace_id))
            self.assertIn("Стоматология",project.portal_state["ai"]["knowledge"])
            self.assertIsNotNone(await db.get(CrmInbound,row.lead_id))
        self.run_db(check)

    def test_real_demo_owner_can_login_and_read_personal_knowledge(self):
        response=self.client.post("/api/brief/submit",headers=test_stage2.ORIGIN,json=self.payload())
        self.assertEqual(response.status_code,201,response.text)
        credentials=response.json()["demo"]
        login=self.client.post("/api/portal/auth/login",headers=test_stage2.ORIGIN,
            json={"username":credentials["username"],"password":credentials["password"]})
        self.assertEqual(login.status_code,200,login.text)
        row=self.run_db(lambda db:db.scalar(select(ExpressAssessment)))
        pid=self.run_db(lambda db:db.scalar(select(Project.id).where(Project.workspace_id==row.demo_workspace_id)))
        knowledge=self.client.get(f"/api/crm/projects/{pid}/ai-settings")
        self.assertEqual(knowledge.status_code,200,knowledge.text)
        self.assertIn("Стоматология",knowledge.json()["knowledge"])

    def test_express_formula_and_unknown_values(self):
        a=self.payload()["answers"]
        self.assertEqual(brief.express_calculation(a)["limit_cpl"],300)
        self.assertEqual(brief.express_calculation({**a,"conv":"6+"})["limit_cpl"],900)
        self.assertIsNone(brief.express_calculation({**a,"conv_idk":True})["limit_cpl"])
        self.assertIsNone(brief.express_calculation({**a,"now_idk":True})["current_cpl"])
        self.assertLessEqual(len(brief.express_calculation({**a,"speed":"В течение дня","conv":"1"})["recommendations"]),3)

    def test_transcript_and_audio_review_confirm_apply(self):
        self.as_user(1)
        facts={"company.what":"Стоматология", "clients.pain":"Хочу увеличить записи", "econ.check":""}
        mocked=AsyncMock(return_value=json.dumps(facts,ensure_ascii=False))
        with patch.object(ai,"configured",return_value=True),patch.object(ai,"complete",mocked):
            r=self.client.post("/api/brief/projects/1/text",headers=test_stage2.ORIGIN,json={"text":"Мы стоматология. Хочу увеличить записи."})
        self.assertEqual(r.status_code,200,r.text);mocked.assert_awaited_once()
        row=r.json();self.assertEqual(row["status"],"review")
        self.assertEqual(row["answers"]["econ.check"],"");self.assertNotIn("econ.margin",row["answers"])
        self.assertIn("clients.pain",row["ai_fields"]);self.assertEqual(row["source"],"call")
        self.assertEqual(row["transcript"],"Мы стоматология. Хочу увеличить записи.")
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/apply",headers=test_stage2.ORIGIN).status_code,409)
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/confirm",headers=test_stage2.ORIGIN).status_code,200)
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/preview",headers=test_stage2.ORIGIN).status_code,200)
        self.assertEqual(self.client.post(f"/api/brief/projects/1/{row['id']}/apply",headers=test_stage2.ORIGIN).status_code,200)
        with patch.object(brief,"transcribe_meeting",AsyncMock(return_value="company.what: Стоматология")) as stt:
            r=self.client.post("/api/brief/projects/1/recording",headers=test_stage2.ORIGIN,files={"file":("meeting.wav",b"RIFFtest")})
        self.assertEqual(r.status_code,200,r.text);stt.assert_awaited_once()
        self.assertEqual(r.json()["source"],"call")

    def test_baseline_fix_owner_and_admin_history(self):
        self.as_user(1)
        body={"values":{"leads":100,"cpl":400,"conversion":20,"sales":20,"average_check":5000,"budget":40000,
            "start_date":"2026-10-05","goal_metric":"sales","goal_value":40,"goal_text":"40 продаж"},"fix":True}
        r=self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body)
        self.assertEqual(r.status_code,403,r.text)
        self.assertFalse(self.client.get("/api/brief/projects/1/baseline").json()["can_fix"])
        body["fix"]=False
        r=self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body)
        self.assertEqual(r.status_code,200,r.text);self.assertFalse(r.json()["values"]["fixed"])
        self.admin();body["fix"]=True
        r=self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body)
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()["values"]["fixed"])
        self.assertTrue(r.json()["can_fix"])
        self.as_user(1)
        self.assertEqual(self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body).status_code,403)
        self.as_user(2)
        self.assertEqual(self.client.get("/api/brief/projects/1/baseline").status_code,200)
        self.assertEqual(self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body).status_code,403)
        self.admin();body["values"]["leads"]=120
        r=self.client.put("/api/brief/projects/1/baseline",headers=test_stage2.ORIGIN,json=body)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(len(r.json()["history"]),3)
        self.assertEqual(r.json()["history"][-1]["before"]["leads"],100)

    def test_reference_keys_and_types(self):
        self.assertEqual(len(brief.EXPRESS),12);self.assertEqual(len(brief.BRIEF),10)
        for key in ("company.name","econ.margin","sales.conv","goals.budgetReady","ai.never","access.access_note"):
            self.assertIn(key,brief.QUESTIONS)
        self.assertEqual(brief.QUESTIONS["sales.conv"]["unit"],"из 100")
        self.assertEqual(brief.clean_answers({"sales.crm":["Таблица","Мессенджеры"]})["sales.crm"],"Таблица, Мессенджеры")

    def test_assessment_assignment_seeds_client_baseline_without_moving_lead(self):
        response=self.submit();aid=response.json()["assessment_id"]
        self.admin()
        for _ in range(2):
            r=self.client.post(f"/api/admin/briefs/assessments/{aid}/assign",headers=test_stage2.ORIGIN,json={"project_id":1})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()["answers"]["company.what"],"Стоматология")
        async def check(db):
            self.assertEqual(await db.scalar(select(func.count(ClientBrief.id))),1)
            project=await db.get(Project,1)
            self.assertEqual(project.portal_state["baseline"]["leads"],100)
            self.assertEqual(project.portal_state["baseline"]["conversion"],20)
            assessment=await db.get(ExpressAssessment,aid)
            self.assertEqual((await db.get(CrmInbound,assessment.lead_id)).workspace_id,1)
        self.run_db(check)

    def test_additive_migration_preserves_old_brief(self):
        async def check(db):
            from app.db_upgrade import upgrade_existing_schema
            from sqlalchemy import text,inspect
            row=ClientBrief(workspace_id=1,project_id=1,answers={"company.what":"Стоматология"})
            db.add(row);await db.commit()
            async with self.engine.begin() as conn:
                await conn.execute(text("ALTER TABLE client_briefs DROP COLUMN transcript"))
                await conn.execute(text("ALTER TABLE client_briefs DROP COLUMN ai_fields"))
                await conn.run_sync(upgrade_existing_schema)
                await conn.run_sync(upgrade_existing_schema)
                columns=await conn.run_sync(lambda c:{x["name"] for x in inspect(c).get_columns("client_briefs")})
                self.assertTrue({"transcript","ai_fields"}<=columns)
            await db.refresh(row)
            self.assertEqual(row.answers,{"company.what":"Стоматология"})
        self.run_db(check)


if __name__=="__main__":unittest.main()
