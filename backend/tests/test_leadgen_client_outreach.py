"""Аутрич как услуга: кампания «для клиента» -> ответ лида в CRM клиента (воронка «Аутрич»), задача, стоп-лист клиента."""
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.crypto import encrypt_secret
from app.domains.leadgen import campaigns, client_outreach, sequences, service
from app.domains.leadgen.models import LgCampaign, LgCompany, LgDnc, LgEnrollment, LgMailbox, LgSequence
from app.models.crm import CrmActivity, CrmDeal, CrmPipeline, CrmStage, CrmTask
from app.models.marketing import ClientWorkspace, PortalUser, Project, ProjectSource
from test_leadgen_campaigns import CampaignApiTests, box
from test_leadgen_sequences import NOW, ORIGIN, FakeTransport

STEP = [{"channel": "email", "delay_days": 0, "subject": "Вопрос для {{company}}", "body": "Здравствуйте!"}]
SCHEDULE = {"timezone": "Europe/Moscow", "days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}


class ClientOutreachTests(CampaignApiTests):
    def setUp(self):
        super().setUp()

        async def seed(db):
            db.add(ClientWorkspace(id=8, name="Кухни Про"))
            await db.flush()
            db.add(Project(id=80, workspace_id=8, name="Кухни Про", portal_state={"work_schedule": SCHEDULE}))
            db.add(PortalUser(id=81, workspace_id=8, username="owner@kp.ru", password_hash="x", display_name="Ольга",
                              role="client_owner"))
            db.add(PortalUser(id=82, workspace_id=8, username="rop@kp.ru", password_hash="x", display_name="Роман",
                              role="sales_head"))
        self.run_db(seed)

    async def client_campaign(self, db, n=1, auto=True, responsible=None):
        mb = box("anna@kuhnipro-mail.ru")
        mb.client_project_id = 80
        db.add(mb)
        await db.flush()
        seq = LgSequence(workspace_id=7, name="Кухни Про", steps=STEP, campaign_owned=True,
                         window={"tz": "Europe/Moscow", "gap_minutes": 0})
        db.add(seq)
        await db.flush()
        camp = LgCampaign(workspace_id=7, name="Для Кухни Про", status="active", sequence_id=seq.id,
                          mailbox_ids=[mb.id], variables=[], client_project_id=80, auto_handoff=auto,
                          responsible_user_id=responsible, handoff_cc="rop@kp.ru")
        db.add(camp)
        await db.flush()
        for i in range(n):
            c = (await service.upsert_company(db, 7, service.FindingIn(
                source="import", name=f"k{i}", domain=f"k{i}.ru",
                contacts=[{"kind": "email", "value": f"info@k{i}.ru"}]), now=NOW)).company
            assert (await sequences.enroll(db, seq, c, now=NOW, campaign=camp)).ok
        return mb, camp

    def reply(self, t, text, uid=1, minutes=5, sender="info@k0.ru"):
        async def fn(db):
            mb = await db.scalar(select(LgMailbox).where(LgMailbox.client_project_id == 80))
            return await sequences.handle_inbound(db, mb, sequences.IncomingMail(
                uid=uid, sender=sender, subject="Re: Вопрос", body=text, message_id=f"<r{uid}@k.ru>",
                in_reply_to=t.sent[0].message_id), now=NOW + timedelta(minutes=minutes))
        return self.run_db(fn)

    def deals(self):
        async def fn(db):
            return (await db.scalars(select(CrmDeal).where(CrmDeal.project_id == 80))).all()
        return self.run_db(fn)

    def test_hot_reply_goes_to_client_crm_with_task(self):
        t = FakeTransport()

        async def seed(db):
            await self.client_campaign(db, responsible=82)
            await sequences.process_due(db, t, now=NOW)
        self.run_db(seed)
        self.assertEqual(t.sent[0].sender, "anna@kuhnipro-mail.ru")   # письмо ушло с ящика клиента
        self.reply(t, "Интересно, позвоните мне завтра, +7 900 123-45-67")

        async def check(db):
            deal = await db.scalar(select(CrmDeal).where(CrmDeal.project_id == 80))
            pipeline = await db.get(CrmPipeline, deal.pipeline_id)
            stage = await db.get(CrmStage, deal.stage_id)
            source = await db.get(ProjectSource, deal.source_id)
            task = await db.scalar(select(CrmTask).where(CrmTask.deal_id == deal.id))
            feed = (await db.scalars(select(CrmActivity.payload).where(CrmActivity.deal_id == deal.id))).all()
            company = await db.scalar(select(LgCompany).where(LgCompany.domain == "k0.ru"))
            enr = await db.scalar(select(LgEnrollment).where(LgEnrollment.company_id == company.id))
            return pipeline.name, stage.name, source.name, deal.origin, task, feed, company, enr
        pipeline, stage, source, origin, task, feed, company, enr = self.run_db(check)
        self.assertEqual((pipeline, stage, source, origin), ("Аутрич", "Интерес", "Аутрич (email)", "OUTREACH"))
        self.assertEqual(task.type_code, "CALL")
        self.assertEqual(task.responsible_user_id, 82)        # ответственный из кампании
        self.assertEqual(task.priority, "HIGH")
        text = " ".join(p.get("text", "") for p in feed)
        self.assertIn("Здравствуйте!", text)                  # вся переписка в ленте сделки
        self.assertIn("rop@kp.ru", text)
        self.assertIsNone(company.crm_deal_id)                # CRM агентства не тронута
        self.assertIsNotNone(enr.client_deal_id)
        # второе письмо — в ту же сделку, новой не появилось
        self.reply(t, "Забыл: лучше после обеда", uid=2, minutes=9)
        self.assertEqual(len(self.deals()), 1)

        async def feed_count(db):
            return await db.scalar(select(func.count(CrmActivity.id)).where(
                CrmActivity.deal_id == enr.client_deal_id, CrmActivity.event_type == "COMMENT_ADDED"))
        self.assertGreaterEqual(self.run_db(feed_count), 3)

    def test_plain_reply_waits_for_button_and_owner_gets_task(self):
        t = FakeTransport()

        async def seed(db):
            await self.client_campaign(db)
            await sequences.process_due(db, t, now=NOW)
        self.run_db(seed)
        self.reply(t, "А сколько это стоит?")
        self.assertEqual(self.deals(), [])                    # не горячий — ждём решения человека
        item = self.client.get(f"{self.base}/inbox?status=all").json()["items"][0]
        self.assertEqual(item["campaign"]["client"], "Кухни Про")
        self.assertEqual(item["campaign"]["handoff_cc"], "rop@kp.ru")
        out = self.client.post(f"{self.base}/inbox/{item['id']}/to-client", headers=ORIGIN).json()
        self.assertIsNotNone(out["client_deal_id"])
        again = self.client.post(f"{self.base}/inbox/{item['id']}/to-client", headers=ORIGIN).json()
        self.assertEqual(again["client_deal_id"], out["client_deal_id"])   # без дублей

        async def check(db):
            deal = await db.get(CrmDeal, out["client_deal_id"])
            return (await db.get(CrmStage, deal.stage_id)).name, \
                await db.scalar(select(CrmTask).where(CrmTask.deal_id == deal.id))
        stage, task = self.run_db(check)
        self.assertEqual(stage, "Ответил")
        self.assertEqual(task.responsible_user_id, 81)        # по умолчанию — собственник клиента
        self.assertEqual(len(self.deals()), 1)

    def test_manual_mode_does_not_auto_handoff(self):
        t = FakeTransport()

        async def seed(db):
            await self.client_campaign(db, auto=False)
            await sequences.process_due(db, t, now=NOW)
        self.run_db(seed)
        self.reply(t, "Интересно, позвоните, +7 900 123-45-67")
        self.assertEqual(self.deals(), [])

    def test_unsubscribe_goes_to_client_stop_list_only(self):
        t = FakeTransport()

        async def seed(db):
            await self.client_campaign(db)
            await sequences.process_due(db, t, now=NOW)
        self.run_db(seed)
        self.reply(t, "Отпишите нас, больше не пишите")

        async def check(db):
            company = await db.scalar(select(LgCompany).where(LgCompany.domain == "k0.ru"))
            rows = (await db.scalars(select(LgDnc))).all()
            agency = await service.check_can_contact(db, company.id, None, "email", now=NOW + timedelta(days=60))
            client = await service.check_can_contact(db, company.id, None, "email", now=NOW + timedelta(days=60),
                                                     client_project_id=80)
            return company.stage, rows, agency, client
        stage, rows, agency, client = self.run_db(check)
        self.assertTrue(rows and all(r.client_project_id == 80 for r in rows))
        self.assertNotEqual(stage, "dnc")                     # этапы базы агентства не тронуты
        self.assertNotEqual(agency.reason, "dnc")
        self.assertFalse(client.ok)
        self.assertEqual(self.deals(), [])                    # отписку клиенту не передаём

    def test_mailbox_belongs_to_its_owner(self):
        async def fn(db):
            agency, mine = box("egor@team-a.ru"), box("anna@kp-mail.ru")
            mine.client_project_id = 80
            db.add_all([agency, mine])
            await db.flush()
            return (await campaigns.valid_mailboxes(db, 7, [agency.id, mine.id]),
                    await campaigns.valid_mailboxes(db, 7, [agency.id, mine.id], 80), agency.id, mine.id)
        for_agency, for_client, a, m = self.run_db(fn)
        self.assertEqual((for_agency, for_client), ([a], [m]))

    def test_api_clients_campaign_settings(self):
        b = self.base
        clients = self.client.get(f"{b}/clients").json()["items"]
        self.assertEqual([c["name"] for c in clients], ["Кухни Про"])
        self.assertEqual({u["id"] for u in clients[0]["team"]}, {81, 82})
        c = self.client.post(f"{b}/campaigns", headers=ORIGIN, json={"name": "Для КП", "client_project_id": 80}).json()
        self.assertEqual(c["client"]["name"], "Кухни Про")
        self.assertTrue(c["auto_handoff"])
        p = self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN,
                              json={"responsible_user_id": 82, "handoff_cc": "ROP@kp.ru", "auto_handoff": False}).json()
        self.assertEqual((p["responsible_user_id"], p["handoff_cc"], p["auto_handoff"]), (82, "rop@kp.ru", False))
        bad = self.client.patch(f"{b}/campaigns/{c['id']}", headers=ORIGIN, json={"responsible_user_id": 1})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.post(f"{b}/campaigns", headers=ORIGIN,
                                          json={"name": "Себе", "client_project_id": 999}).status_code, 422)


class ClientReportTests(ClientOutreachTests):
    def test_report_counts_written_replied_handed(self):
        t = FakeTransport()

        async def seed(db):
            await self.client_campaign(db, n=2)
            await sequences.process_due(db, t, now=NOW)
        self.run_db(seed)
        self.reply(t, "Интересно, позвоните, +7 900 123-45-67")

        async def fn(db):
            project = await db.get(Project, 80)
            data = await client_outreach.report(db, project, NOW - timedelta(days=1), NOW + timedelta(days=1))
            other = await client_outreach.report(db, await db.get(Project, 80), NOW + timedelta(days=5), NOW + timedelta(days=6))
            camp = await db.scalar(select(LgCampaign))
            return data, other, (await campaigns.stats(db, camp))["handed"]
        data, other, handed = self.run_db(fn)
        self.assertTrue(data["enabled"])
        self.assertEqual({k: data["totals"][k] for k in ("written", "replied", "handed")}, {"written": 2, "replied": 1, "handed": 1})
        self.assertEqual(data["totals"]["reply_rate"], 50.0)
        self.assertEqual(sum(s["count"] for s in data["funnel"]), 1)
        self.assertEqual(other["totals"]["written"], 0)
        self.assertEqual(handed, 1)


class DueTests(unittest.TestCase):
    def test_hot_15_business_minutes_else_end_of_day(self):
        msk = timezone(timedelta(hours=3))
        wed_noon = datetime(2026, 10, 28, 12, 0, tzinfo=msk)
        self.assertEqual(client_outreach.due_at(wed_noon, SCHEDULE, True), wed_noon + timedelta(minutes=15))
        self.assertEqual(client_outreach.due_at(wed_noon, SCHEDULE, False).hour, 18)
        fri_night = datetime(2026, 10, 30, 22, 0, tzinfo=msk)
        due = client_outreach.due_at(fri_night, SCHEDULE, True).astimezone(msk)
        self.assertEqual((due.weekday(), due.hour, due.minute), (0, 9, 15))   # понедельник 9:15


if __name__ == "__main__":
    unittest.main()
