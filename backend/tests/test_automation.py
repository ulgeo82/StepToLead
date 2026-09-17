"""Scheduler checks use an isolated database and fake Telegram, never real sends."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db import Base
from app.models import Campaign, Lead, TelegramAccount
from app.models.automation import CampaignRuntime, CampaignAccount, CampaignDialog, CampaignMessage, CampaignEvent, GlobalBlock
from app.core.crypto import encrypt_secret
from app.schemas.automation import AutomationSettings, AutomationUpdate
from app.services.campaign_runner import AIProviderError, sleeping, blacklisted, stopped_by_recipient, send, first_message, process_dialog, telegram_read
from app.api.routes.automation import save_automation, start_campaign, pause_campaign
from fastapi import HTTPException


class PolicyTests(unittest.TestCase):
    def test_sleep_crosses_midnight_and_uses_timezone(self):
        settings = AutomationSettings(sleep_periods="23:00-08:00", timezone_offset=4)
        self.assertTrue(sleeping(settings, datetime(2026, 9, 6, 20, tzinfo=timezone.utc)))
        self.assertFalse(sleeping(settings, datetime(2026, 9, 6, 10, tzinfo=timezone.utc)))

    def test_validation_rejects_reversed_pause(self):
        with self.assertRaises(ValueError):
            AutomationSettings(action_min=100, action_max=10)

    def test_blacklist_and_opt_out(self):
        self.assertTrue(blacklisted(AutomationSettings(blacklist="@SpamBot, ULgeo82"), "ulgeo82"))
        self.assertTrue(stopped_by_recipient("Стоп!"))
        self.assertFalse(stopped_by_recipient("Расскажите подробнее"))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.campaign = Campaign(name="Isolated test")
        self.account = TelegramAccount(phone="test-account", session_encrypted="fake")
        self.db.add_all([self.campaign, self.account]); await self.db.commit()
        self.lead = Lead(campaign_id=self.campaign.id, username="test_user", first_message="Test")
        self.db.add(self.lead); await self.db.commit()
        self.runtime = CampaignRuntime(campaign_id=self.campaign.id, status="paused", settings={})
        self.dialog = CampaignDialog(campaign_id=self.campaign.id, account_id=self.account.id, lead_id=self.lead.id,
                                     peer_id=123, access_hash=456, username="test_user", state="active")
        self.db.add_all([self.runtime, self.dialog]); await self.db.commit()
        self.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=99)), get_messages=AsyncMock(return_value=[]), send_read_acknowledge=AsyncMock())

    async def asyncTearDown(self):
        await self.db.close(); await self.engine.dispose()

    async def test_paused_campaign_never_sends_then_resume_sends_once(self):
        self.assertFalse(await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first"))
        self.client.send_message.assert_not_awaited()
        self.runtime.status = "running"; await self.db.commit()
        self.assertTrue(await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first"))
        self.assertTrue(await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first"))
        self.client.send_message.assert_awaited_once()
        events = list(await self.db.scalars(select(CampaignEvent).where(CampaignEvent.event == "message_sent")))
        self.assertEqual(len(events), 1)

    async def test_uncertain_send_not_retried(self):
        self.runtime.status = "running"; await self.db.commit()
        self.client.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first")
        self.assertFalse(await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first"))
        row = await self.db.scalar(select(CampaignMessage))
        self.assertEqual(row.status, "unknown")
        self.assertEqual(self.dialog.state, "review")
        self.client.send_message.assert_awaited_once()

    async def test_global_block_prevents_send(self):
        self.runtime.status = "running"
        self.db.add(GlobalBlock(peer_id=123)); await self.db.commit()
        self.assertFalse(await send(self.db, self.client, self.runtime, self.dialog, "first", "Test", "first"))
        self.client.send_message.assert_not_awaited()

    async def test_zero_daily_limit(self):
        self.assertFalse(await first_message(self.db, self.client, self.runtime, AutomationSettings(daily_limit=0), self.account))
        self.client.send_message.assert_not_awaited()

    async def test_safe_telegram_read_reconnects_and_retries_once(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError()
            return ["ok"]

        self.client.disconnect = AsyncMock()
        self.client.connect = AsyncMock()
        result = await telegram_read(self.db, self.client, self.runtime, self.dialog, "test stage", operation, 1)

        self.assertEqual(result, ["ok"])
        self.assertEqual(attempts, 2)
        self.client.disconnect.assert_awaited_once()
        self.client.connect.assert_awaited_once()
        event = await self.db.scalar(select(CampaignEvent).where(CampaignEvent.event == "telegram_read_retry"))
        self.assertIsNotNone(event)

    async def test_followup_only_once(self):
        self.runtime.status = "running"
        self.dialog.started_at = datetime.now(timezone.utc) - timedelta(days=2)
        self.db.add(CampaignMessage(campaign_id=self.campaign.id, dialog_id=self.dialog.id, account_id=self.account.id,
                                   kind="first", body="Test", dedupe_key="first", status="sent", telegram_message_id=1))
        await self.db.commit()
        settings = AutomationSettings(auto_reply=False, followup_enabled=True, followup_text="Reminder")
        self.assertTrue(await process_dialog(self.db, self.client, self.runtime, settings, self.dialog))
        self.assertFalse(await process_dialog(self.db, self.client, self.runtime, settings, self.dialog))
        self.client.send_message.assert_awaited_once()

    async def test_opt_out_prevents_ai_and_followup(self):
        self.runtime.status = "running"
        self.db.add(CampaignMessage(campaign_id=self.campaign.id, dialog_id=self.dialog.id, account_id=self.account.id,
                                   kind="first", body="Test", dedupe_key="first", status="sent", telegram_message_id=1))
        await self.db.commit()
        self.client.get_messages.return_value = [SimpleNamespace(id=2, out=False, message="Стоп")]
        self.assertFalse(await process_dialog(self.db, self.client, self.runtime, AutomationSettings(), self.dialog))
        self.assertEqual(self.dialog.state, "stopped")
        self.assertIsNotNone(await self.db.get(GlobalBlock, 123))
        self.client.send_message.assert_not_awaited()

    async def test_start_requires_ai_and_saving_does_not_start(self):
        payload = AutomationUpdate(settings=AutomationSettings(), account_ids=[self.account.id])
        result = await save_automation(self.campaign.id, payload, self.db)
        self.assertEqual(result["status"], "paused")
        with self.assertRaises(HTTPException) as error:
            await start_campaign(self.campaign.id, self.db)
        self.assertEqual(error.exception.status_code, 422)
        await self.db.rollback()

    async def test_start_with_complete_ai_settings(self):
        self.runtime.settings = AutomationSettings(
            system_prompt="Test prompt",
            ai_url="https://api.openai.com/v1/chat/completions",
            ai_model="gpt-5.4-mini",
        ).model_dump()
        self.runtime.api_key_encrypted = encrypt_secret("test-key")
        self.db.add(CampaignAccount(campaign_id=self.campaign.id, account_id=self.account.id))
        await self.db.commit()

        with patch("app.api.routes.automation.request_ai", return_value="готово"):
            result = await start_campaign(self.campaign.id, self.db)

        self.assertEqual(result["status"], "running")
        event = await self.db.scalar(select(CampaignEvent).where(CampaignEvent.event == "campaign_started"))
        self.assertIsNotNone(event)

    async def test_start_is_blocked_when_ai_key_is_rejected(self):
        self.runtime.settings = AutomationSettings(
            system_prompt="Test prompt",
            ai_url="https://api.groq.com/openai/v1/chat/completions",
            ai_model="test-model",
        ).model_dump()
        self.runtime.api_key_encrypted = encrypt_secret("invalid-key")
        self.db.add(CampaignAccount(campaign_id=self.campaign.id, account_id=self.account.id))
        await self.db.commit()

        with patch("app.api.routes.automation.request_ai", side_effect=AIProviderError("Ключ неверный", 401, "invalid_api_key")):
            with self.assertRaises(HTTPException) as error:
                await start_campaign(self.campaign.id, self.db)

        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.runtime.status, "paused")
        event = await self.db.scalar(select(CampaignEvent).where(CampaignEvent.event == "campaign_start_blocked"))
        self.assertIsNotNone(event)

    async def test_ai_reply_is_not_sent_twice_and_handoff_stops_dialog(self):
        self.runtime.status = "running"
        self.dialog.last_incoming_id = 2
        self.dialog.next_reply_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        for kind, message_id in [("first", 1), ("incoming", 2)]:
            self.db.add(CampaignMessage(campaign_id=self.campaign.id, dialog_id=self.dialog.id, account_id=self.account.id,
                                       kind=kind, body="Test", dedupe_key=kind, status="sent" if kind == "first" else "received", telegram_message_id=message_id))
        await self.db.commit()
        settings = AutomationSettings(positive_chat="test_manager")
        with patch("app.services.campaign_runner.request_ai", return_value=settings.positive_trigger):
            self.assertTrue(await process_dialog(self.db, self.client, self.runtime, settings, self.dialog))
            self.assertFalse(await process_dialog(self.db, self.client, self.runtime, settings, self.dialog))
        self.assertEqual(self.dialog.state, "handoff")
        self.assertEqual(self.client.send_message.await_count, 2)  # reply and manager summary

    async def test_reply_cancels_followup(self):
        self.runtime.status = "running"
        self.dialog.started_at = datetime.now(timezone.utc) - timedelta(days=2)
        self.dialog.last_incoming_id = 2
        self.dialog.last_reply_id = 2
        self.db.add(CampaignMessage(campaign_id=self.campaign.id, dialog_id=self.dialog.id, account_id=self.account.id,
                                   kind="first", body="Test", dedupe_key="first", status="sent", telegram_message_id=1))
        await self.db.commit()
        settings = AutomationSettings(auto_reply=False, followup_enabled=True, followup_text="Reminder")
        self.assertFalse(await process_dialog(self.db, self.client, self.runtime, settings, self.dialog))
        self.client.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
