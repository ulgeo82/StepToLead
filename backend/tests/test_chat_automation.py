"""Chat rules: debounce, grounding, human SLA and delivery gates; no external providers."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from test_messaging import MessagingTests, ORIGIN, tg
from app.models.crm import CrmAutomation, CrmDeal, CrmPipeline, CrmStage
from app.models.marketing import Project, PortalUser
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import chat_automation as rules, messaging
from app.services.messaging import TelegramBot


class ChatAutomationTests(MessagingTests):
    def setup_rule(self, *, action="SEND_TEMPLATE", trigger="INBOUND_MESSAGE", conditions=None,
                   params=None, mode="auto", delay=0):
        channel_id = self.connect_telegram()
        self.poll(channel_id, [tg(1, "Здравствуйте")])

        async def seed(db):
            pipeline = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == 1))
            if not pipeline:
                pipeline = CrmPipeline(workspace_id=1, project_id=1, name="Продажи", is_default=True)
                db.add(pipeline)
                await db.flush()
                db.add(CrmStage(pipeline_id=pipeline.id, name="Новый", analytics_type="LEAD"))
            channel = await db.get(MessagingChannel, channel_id)
            channel.ai_mode = mode
            project = await db.get(Project, 1)
            project.portal_state = {"ai": {"knowledge": "Кухни. Консультация по записи.", "tone": "вежливо"}}
            rule = CrmAutomation(workspace_id=1, project_id=1, pipeline_id=pipeline.id, name="Ответ",
                                 trigger=trigger, action=action, delay_minutes=delay,
                                 conditions=conditions or {}, params=params or {"text": "Здравствуйте, {имя}!"})
            db.add(rule)
            await db.commit()
            return rule.id

        rule_id = self.read(seed)
        return channel_id, rule_id

    def process(self, *, seconds=30, minutes=0):
        async def run(db):
            conversation = await db.scalar(select(Conversation))
            return await rules.process(db, conversation, at=rules.now() + timedelta(seconds=seconds, minutes=minutes))
        return self.read(run)

    def conversation(self):
        return self.read(lambda db: db.scalar(select(Conversation)))

    def outgoing(self):
        return list(self.read(lambda db: db.scalars(select(Message).where(Message.direction == "out"))))

    def test_debounce_batch_first_only_and_no_loop(self):
        channel, rid = self.setup_rule(conditions={"first_only": True})
        self.poll(channel, [tg(2, "Ещё вопрос"), tg(3, "Размеры")])
        sender = AsyncMock(return_value="out1")
        with patch.object(TelegramBot, "send", sender):
            self.assertEqual(self.process(seconds=10), 0)
            self.assertEqual(self.process(), 1)
            self.assertEqual(self.process(), 0)
            self.poll(channel, [tg(4, "А ещё?")])
            self.assertEqual(self.process(), 0)
        sender.assert_awaited_once()
        self.assertEqual(self.outgoing()[0].meta, {"auto": "template", "rule_id": rid})
        self.assertIsNotNone(self.conversation().waiting_since)
        self.assertNotIn("auto_pending", self.conversation().meta)

    def test_working_hours_and_channel_conditions(self):
        channel, rid = self.setup_rule(conditions={"time": "nonworking", "channel_kinds": ["email"]})
        with patch.object(TelegramBot, "send", AsyncMock(return_value="out1")) as sender:
            self.assertEqual(self.process(), 0)
            async def change(db):
                rule = await db.get(CrmAutomation, rid)
                rule.conditions = {"time": "nonworking", "channel_ids": [channel]}
                await db.commit()
            self.read(change)
            self.poll(channel, [tg(2, "Новый вопрос")])
            with patch.object(rules, "working_hours", AsyncMock(return_value=True)):
                self.assertEqual(self.process(), 0)
            self.poll(channel, [tg(3, "Поздний вопрос")])
            with patch.object(rules, "working_hours", AsyncMock(return_value=False)):
                self.assertEqual(self.process(), 1)
        sender.assert_awaited_once()

    def test_ai_two_replies_then_human_resets_and_sla_preserved(self):
        channel, _ = self.setup_rule(action="AI_REPLY", params={"mode": "auto"})
        inbound = self.conversation().inbound_id
        result = self.client.post(f"/api/crm/inbound/{inbound}/accept", headers=ORIGIN, json={})
        self.assertEqual(result.status_code, 200, result.text)
        deal_id = result.json()["deal_id"]
        original_wait = self.conversation().waiting_since
        sender = AsyncMock(side_effect=["a1", "a2", "human", "a3"])
        provider = AsyncMock(return_value="Уточню у менеджера.")
        with patch.object(TelegramBot, "send", sender), patch.object(rules.ai, "configured", return_value=True), \
                patch.object(rules.plans, "has", AsyncMock(return_value=True)), patch.object(rules.ai, "complete", provider):
            self.assertEqual(self.process(), 1)
            self.poll(channel, [tg(2, "Ещё вопрос")])
            self.assertEqual(self.process(), 1)
            self.poll(channel, [tg(3, "Третий вопрос")])
            self.assertEqual(self.process(), 0)
            self.assertEqual(self.conversation().waiting_since, original_wait)
            deal = self.read(lambda db: db.get(CrmDeal, deal_id))
            self.assertIsNone(deal.first_response_at)
            self.assertEqual(provider.await_count, 2)
            sent = self.client.post(f"/api/crm/conversations/{self.conversation().id}/messages",
                                    headers=ORIGIN, json={"text": "Ответ менеджера"})
            self.assertEqual(sent.status_code, 201, sent.text)
            self.assertEqual(self.conversation().meta["ai_consecutive"], 0)
            self.assertIsNotNone(self.read(lambda db: db.get(CrmDeal, deal_id)).first_response_at)
            self.poll(channel, [tg(4, "После ответа человека")])
            self.assertEqual(self.process(), 1)
        self.assertEqual(provider.await_args.kwargs["temperature"], 0.1)

    def test_no_ai_fallback_and_journal(self):
        _, rid = self.setup_rule(action="AI_REPLY", params={"fallback_template": "{имя}, менеджер ответит."})
        with patch.object(rules.ai, "configured", return_value=False), \
                patch.object(TelegramBot, "send", AsyncMock(return_value="a1")), \
                patch.object(rules.ai, "complete", AsyncMock()) as provider:
            self.assertEqual(self.process(), 1)
            provider.assert_not_awaited()
        self.assertEqual(self.outgoing()[0].text, "Анна, менеджер ответит.")
        self.assertEqual(self.outgoing()[0].meta, {"auto": "template", "rule_id": rid})
        self.assertIn("ИИ не подключён", self.conversation().meta["automation_log"][0]["detail"])

    def test_no_ai_without_fallback_is_logged_once(self):
        self.setup_rule(action="AI_REPLY", params={"mode": "auto"}, trigger="NO_REPLY")
        with patch.object(rules.ai, "configured", return_value=False):
            self.assertEqual(self.process(minutes=11), 0)
            self.assertEqual(self.process(minutes=11), 0)
        self.assertEqual(len(self.conversation().meta["automation_log"]), 1)

    def test_channel_off_and_draft(self):
        channel, _ = self.setup_rule(action="AI_REPLY", params={"mode": "draft"}, mode="off")
        with patch.object(rules.ai, "complete", AsyncMock(return_value="Уточню у менеджера.")) as provider, \
                patch.object(rules.ai, "configured", return_value=True), \
                patch.object(rules.plans, "has", AsyncMock(return_value=True)):
            self.assertEqual(self.process(), 0)
            provider.assert_not_awaited()
            async def enable(db):
                (await db.get(MessagingChannel, channel)).ai_mode = "suggest"
                await db.commit()
            self.read(enable)
            self.poll(channel, [tg(2, "Ещё сообщение")])
            self.assertEqual(self.process(), 1)
        self.assertEqual(self.outgoing(), [])
        self.assertEqual(self.conversation().meta["auto_draft"]["text"], "Уточню у менеджера.")
        self.assertIsNotNone(self.conversation().waiting_since)

    def test_no_reply_default_ten_minutes(self):
        self.setup_rule(trigger="NO_REPLY")
        with patch.object(TelegramBot, "send", AsyncMock(return_value="a1")) as sender:
            self.assertEqual(self.process(seconds=0, minutes=9), 0)
            self.assertEqual(self.process(seconds=0, minutes=11), 1)
            self.assertEqual(self.process(seconds=0, minutes=20), 0)
        sender.assert_awaited_once()

    def test_new_batch_during_provider_call_prevents_stale_reply(self):
        channel, _ = self.setup_rule(action="AI_REPLY", params={"mode": "auto"})
        async def provider(*args, **kwargs):
            async with self.sessions() as db:
                await messaging.ingest(db, await db.get(MessagingChannel, channel), tg(2, "Новый вопрос"))
                await db.commit()
            return "Уточню у менеджера."
        with patch.object(rules.ai, "configured", return_value=True), \
                patch.object(rules.plans, "has", AsyncMock(return_value=True)), \
                patch.object(rules.ai, "complete", AsyncMock(side_effect=provider)), \
                patch.object(TelegramBot, "send", AsyncMock()) as sender:
            self.assertEqual(self.process(), 0)
        sender.assert_not_awaited()
        self.assertIn("auto_pending", self.conversation().meta)

    def test_guard_unsupported_price_and_term(self):
        self.assertEqual(rules.grounded("Цена 5000 рублей, привезём завтра", "Кухни"), "Уточню у менеджера.")
        self.assertEqual(rules.grounded("Скидка бесплатно", "Кухни"), "Уточню у менеджера.")
        self.assertEqual(rules.grounded("Консультация 500 рублей", "Консультация 500 рублей"),
                         "Консультация 500 рублей")

    def test_rule_api_validates_chat_actions_and_channel_ai_mode(self):
        channel, _ = self.setup_rule()
        pipeline = self.read(lambda db: db.scalar(select(CrmPipeline.id)))
        payload = {"pipeline_id": pipeline, "name": "ИИ-правило", "trigger": "INBOUND_MESSAGE",
                   "action": "AI_REPLY", "params": {"mode": "auto"}, "conditions": {"channel_ids": [channel]}}
        response = self.client.post("/api/crm/projects/1/automations", headers=ORIGIN, json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        payload["trigger"] = "DEAL_CREATED"
        self.assertEqual(self.client.post("/api/crm/projects/1/automations", headers=ORIGIN, json=payload).status_code, 422)
        response = self.client.patch(f"/api/crm/channels/{channel}", headers=ORIGIN, json={"ai_mode": "auto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ai_mode"], "auto")
        self.assertEqual(self.client.patch(f"/api/crm/channels/{channel}", headers=ORIGIN,
                                         json={"ai_mode": "bad"}).status_code, 422)
