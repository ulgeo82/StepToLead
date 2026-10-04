"""Installation notification bot: real DB transactions, mocked Telegram, no Redis."""
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.router import api_router  # register all models
from app.api.routes import crm, messaging, telegram_bot
from app.core.access import token_digest
from app.db import Base
from app.models.access import AdminUser
from app.models.crm import CrmActivity, CrmContact, CrmDeal, CrmInbound, CrmTask
from app.models.marketing import ClientLead, ClientWorkspace, PortalProjectAccess, PortalUser, Project, ProjectNotificationRule
from app.models.system import AppSetting
from app.models.messaging import MessagingChannel
from app.services import notifications, tg_bot
from app.services.tg_preferences import allowed, bounded_html, private_text


class TelegramBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([ClientWorkspace(id=1, name="One"), ClientWorkspace(id=2, name="Other")])
            db.add_all([Project(id=1, workspace_id=1, name="Первый <проект>", timezone="Europe/Samara"),
                        Project(id=2, workspace_id=1, name="Закрытый проект"),
                        Project(id=3, workspace_id=2, name="Чужая компания")])
            db.add_all([PortalUser(id=1, workspace_id=1, username="owner", display_name="Owner", password_hash="x",
                                  role="client_owner", telegram_chat_id="100", active=True),
                        PortalUser(id=2, workspace_id=1, username="manager", display_name="Manager", password_hash="x",
                                  role="sales_manager", telegram_chat_id="200", active=True),
                        AdminUser(id=1, username="admin", password_hash="x", role="admin", telegram_chat_id="900")])
            await db.flush()
            db.add(PortalProjectAccess(user_id=2, project_id=1))
            for project_id in (1, 2, 3):
                pipeline = await crm.pipeline_for(db, project_id, commit=False)
                stage = await db.scalar(select(crm.CrmStage).where(crm.CrmStage.pipeline_id == pipeline.id,
                                                                      crm.CrmStage.analytics_type == "LEAD"))
                contact = CrmContact(workspace_id=1 if project_id < 3 else 2, project_id=project_id, name="Клиент")
                db.add(contact); await db.flush()
                db.add(CrmDeal(id=project_id, workspace_id=contact.workspace_id, project_id=project_id,
                               contact_id=contact.id, pipeline_id=pipeline.id, stage_id=stage.id,
                               name="Моя сделка" if project_id == 1 else "Секретная сделка", responsible_user_id=2))
            db.add(CrmInbound(id=1, workspace_id=1, project_id=1, name="Заявка <Tilda>", phone="+79990000001", raw_payload={"contact": "@test"}))
            db.add_all([CrmTask(id=1, workspace_id=1, project_id=1, deal_id=1, responsible_user_id=2,
                               title="Сегодня <позвонить>", type_code="CALL", due_at=datetime.now(timezone.utc)),
                        CrmTask(id=2, workspace_id=1, project_id=1, deal_id=1, responsible_user_id=1,
                               title="Задача владельца", type_code="CALL", due_at=datetime.now(timezone.utc))])
            await db.commit()
        self.api = AsyncMock(return_value={})
        self.patches = [patch.object(tg_bot, "telegram_api", self.api),
                        patch.object(tg_bot, "flush_telegram"),
                        patch.object(notifications.settings, "telegram_bot_token", "test-token")]
        for p in self.patches: p.start()
        self.update_id = 0

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        await self.engine.dispose()

    async def send(self, data="/menu", chat=100, *, callback=False, update_id=None, chat_type="private"):
        self.update_id += 1
        message = {"message_id": 12, "chat": {"id": chat, "type": chat_type}, "from": {"id": chat, "username": "test"}, "text": data}
        update = {"update_id": update_id if update_id is not None else self.update_id}
        if callback:
            update["callback_query"] = {"id": f"cb{self.update_id}", "data": data, "from": {"id": chat}, "message": message}
        else:
            update["message"] = message
        self.api.reset_mock()
        await tg_bot.process_update(update, self.sessions)
        return [call.args[1]["text"] for call in self.api.await_args_list if "text" in call.args[1]]

    async def test_old_bindings_menu_and_admin_separate(self):
        text = "".join(await self.send())
        self.assertIn("Первый <проект>", str(self.api.await_args_list))  # keyboard labels are plain text
        self.assertIn("StepToLead", text)
        text = "".join(await self.send(chat=900))
        self.assertIn("администратор", text)
        self.assertNotIn("Новые заявки", str(self.api.await_args_list))

    async def test_start_links_portal_and_admin(self):
        async with self.sessions() as db:
            for model in (PortalUser, AdminUser):
                user = await db.get(model, 1)
                user.telegram_chat_id = None
                user.telegram_link_code_hash = token_digest(model.__name__)
                user.telegram_link_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
            await db.commit()
        for model, chat in ((PortalUser, 100), (AdminUser, 900)):
            self.assertIn("Telegram подключён", "".join(await self.send("/start " + model.__name__, chat)))
            async with self.sessions() as db:
                user = await db.get(model, 1)
                self.assertEqual(user.telegram_chat_id, str(chat))
                self.assertIsNone(user.telegram_link_code_hash)

    async def test_expired_link_and_unlinked_help(self):
        async with self.sessions() as db:
            user = await db.get(PortalUser, 1)
            user.telegram_link_code_hash = token_digest("old")
            user.telegram_link_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()
        self.assertIn("устарела", "".join(await self.send("/start old", 777)))
        self.assertIn("Настройки", "".join(await self.send(chat=777)))

    async def test_manager_projects_leads_and_tasks_are_scoped(self):
        await self.send(chat=200)
        self.assertNotIn("Закрытый проект", str(self.api.await_args_list))
        self.assertNotIn("Чужая компания", str(self.api.await_args_list))
        text = "".join(await self.send("/leads", 200))
        self.assertIn("Моя сделка", text)
        self.assertNotIn("Секретная", text)
        self.assertNotIn("Tilda", text)
        text = "".join(await self.send("/tasks", 200))
        self.assertIn("Сегодня &lt;позвонить&gt;", text)
        self.assertNotIn("Задача владельца", text)
        text = "".join(await self.send("/leads"))
        self.assertIn("Заявка &lt;Tilda&gt;", text)

    async def test_take_inbound_uses_existing_accept_and_activity(self):
        self.assertIn("взята", "".join(await self.send("take:i:1", callback=True)))
        async with self.sessions() as db:
            inbound = await db.get(CrmInbound, 1)
            self.assertEqual(inbound.status, "ACCEPTED")
            deal = await db.get(CrmDeal, inbound.deal_id)
            self.assertEqual((deal.project_id, deal.responsible_user_id), (1, 1))
            activity = await db.scalar(select(CrmActivity).where(CrmActivity.deal_id == deal.id,
                                                               CrmActivity.event_type == "TELEGRAM_ACTION"))
            self.assertEqual(activity.actor_id, 1)
            self.assertIn("через Telegram", str(activity.payload))
        methods = [c.args[0] for c in self.api.await_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"])

    async def test_non_target_uses_shared_quality(self):
        await self.send("reason:i:1:0", callback=True)
        async with self.sessions() as db:
            inbound = await db.get(CrmInbound, 1)
            deal = await db.get(CrmDeal, inbound.deal_id)
            lead = await db.get(ClientLead, deal.lead_id)
            self.assertEqual(lead.quality, "non_target")
            self.assertEqual(lead.quality_reason, crm.QUALITY_REASONS[0])

    async def test_complete_and_reschedule_task(self):
        await self.send("tomorrow:1", 200, callback=True)
        async with self.sessions() as db:
            task = await db.get(CrmTask, 1)
            self.assertGreater(task.due_at, datetime.now(timezone.utc).replace(tzinfo=None))
        await self.send("finish:1:0", 200, callback=True)
        async with self.sessions() as db:
            task = await db.get(CrmTask, 1)
            self.assertEqual((task.status, task.result), ("COMPLETED", tg_bot.DONE_RESULTS[0]))
            records = (await db.scalars(select(CrmActivity).where(CrmActivity.event_type == "TELEGRAM_ACTION"))).all()
            self.assertEqual(len(records), 2)
            self.assertTrue(all(a.actor_id == 2 for a in records))

    async def test_duplicate_update_does_not_create_second_task(self):
        await self.send("call1:d:1", 200, callback=True, update_id=42)
        await self.send("call1:d:1", 200, callback=True, update_id=42)
        self.assertEqual([c.args[0] for c in self.api.await_args_list], ["answerCallbackQuery"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count(CrmTask.id))), 3)
            state = await db.get(AppSetting, tg_bot.state_key())
            self.assertEqual(state.value["processed"], [42])

    async def test_forged_foreign_project_deal_and_task_are_denied(self):
        for data in ("take:d:3", "take:d:2", "project:2", "finish:2:0"):
            text = "".join(await self.send(data, 200, callback=True))
            self.assertNotIn("взята в работу", text)
            self.assertNotIn("Задача выполнена", text)
        async with self.sessions() as db:
            self.assertEqual((await db.get(CrmDeal, 3)).responsible_user_id, 2)
            self.assertEqual((await db.get(CrmTask, 2)).status, "OPEN")
            self.assertEqual(await db.scalar(select(func.count(CrmActivity.id))), 0)

    async def test_disabled_user_deleted_company_and_revoked_permission(self):
        async with self.sessions() as db:
            user = await db.get(PortalUser, 2); user.active = False; await db.commit()
        self.assertIn("отключён", "".join(await self.send("take:d:1", 200, callback=True)))
        async with self.sessions() as db:
            user = await db.get(PortalUser, 2); user.active = True; user.permissions = ["view_crm", "view_own_deals"]
            await db.commit()
        self.assertIn("Недостаточно прав", "".join(await self.send("take:d:1", 200, callback=True)))
        async with self.sessions() as db:
            (await db.get(ClientWorkspace, 1)).status = "deleted"; await db.commit()
        self.assertIn("отключён", "".join(await self.send()))

    async def test_group_ignored_but_offset_advanced(self):
        self.assertEqual(await self.send(chat_type="group"), [])
        self.api.assert_not_awaited()
        async with self.sessions() as db:
            self.assertEqual((await db.get(AppSetting, tg_bot.state_key())).value["offset"], 2)

    async def test_invalid_action_rolls_back_inbound_acceptance(self):
        await self.send("reason:i:1:999", callback=True)
        async with self.sessions() as db:
            self.assertEqual((await db.get(CrmInbound, 1)).status, "NEW")
            self.assertEqual(await db.scalar(select(func.count(CrmDeal.id))), 3)

    async def test_personal_notification_preferences_and_project_rule(self):
        async with self.sessions() as db:
            user = await db.get(PortalUser, 1)
            db.add(ProjectNotificationRule(project_id=1, event_key="new_lead", enabled=True, telegram=True, in_app=False))
            user.telegram_state = {"notifications": {"new_lead": False}}
            await notifications.notify(db, 1, "new_lead", "Заявка", "+79999999999")
            self.assertNotIn(notifications.PENDING_KEY, db.sync_session.info)
            user.telegram_state = {}
            await notifications.notify(db, 1, "new_lead", "Заявка", "+79999999999", reply_markup={"inline_keyboard": []})
            pending = db.sync_session.info.pop(notifications.PENDING_KEY)
            self.assertIn("телефон скрыт", pending[0]["text"])
            self.assertIsNotNone(pending[0]["reply_markup"])
            user.telegram_state = {"show_phone": True}
            self.assertEqual(private_text(user, "+79999999999"), "+79999999999")
            rule = await db.scalar(select(ProjectNotificationRule)); rule.enabled = False
            await notifications.notify(db, 1, "new_lead", "Заявка", "body")
            self.assertNotIn(notifications.PENDING_KEY, db.sync_session.info)
            user.telegram_state = {"notifications": {"direct": False}}
            notifications.direct(db, 1, [1], "Задача", "body", {1: user})
            self.assertNotIn(notifications.PENDING_KEY, db.sync_session.info)

    async def test_quiet_hours_timezone_and_settings_saved(self):
        await self.send("hours:22:9", callback=True)
        async with self.sessions() as db:
            user = await db.get(PortalUser, 1)
            self.assertFalse(allowed(user, "new_lead", "Europe/Samara", datetime(2026, 10, 4, 19, tzinfo=timezone.utc)))
            self.assertTrue(allowed(user, "new_lead", "Europe/Samara", datetime(2026, 10, 4, 9, tzinfo=timezone.utc)))
        await self.send("pref:new_lead", callback=True)
        async with self.sessions() as db:
            self.assertFalse((await db.get(PortalUser, 1)).telegram_state["notifications"]["new_lead"])

    async def test_global_bot_cannot_be_crm_channel(self):
        request = Request({"type": "http", "method": "POST", "headers": [(b"origin", b"http://localhost:3000")]})
        async with self.sessions() as db:
            user = await db.get(PortalUser, 1)
            with self.assertRaises(HTTPException) as error:
                await messaging.create_channel(1, messaging.ChannelIn(kind="telegram_bot", name="Bad", token="test-token"), request, db, user)
            self.assertEqual(error.exception.status_code, 422)
            channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_bot", name="Client", config={})
            db.add(channel); await db.commit()
            with self.assertRaises(HTTPException) as error:
                await messaging.edit_channel(channel.id, messaging.ChannelPatch(token="test-token"), request, db, user)
            self.assertEqual(error.exception.status_code, 422)
            self.assertIsNone(channel.secret_encrypted)

    async def test_check_only_reads_database(self):
        async with self.sessions() as db:
            result = await telegram_bot.check_link(db, await db.get(PortalUser, 1))
            self.assertTrue(result["linked"])
        self.api.assert_not_awaited()

    async def test_403_unlinks_and_network_error_does_not_crash(self):
        self.api.side_effect = notifications.TelegramError(403)
        await self.send()
        async with self.sessions() as db:
            self.assertIsNone((await db.get(PortalUser, 1)).telegram_chat_id)
        self.api.side_effect = notifications.TelegramError(502)
        await self.send(chat=777)

    async def test_report_reuses_result_facts(self):
        totals = {"leads": 5, "cpl": 100, "sales": 2, "revenue": 1000, "romi": 50}
        with patch("app.services.result_analytics.result_facts", AsyncMock(return_value={"current": {"totals": totals}})) as facts:
            self.assertIn("1 000.00", "".join(await self.send("report:30", callback=True)))
            facts.assert_awaited_once()
            self.assertEqual((facts.call_args.args[3] - facts.call_args.args[2]).days, 29)

    async def test_html_and_callbacks_safe(self):
        self.assertNotIn("<b>", bounded_html("<b>" + "&amp;" * 4000 + "</b>"))
        with self.assertRaises(ValueError): tg_bot.button("x", "я" * 33)

    async def test_failed_transaction_is_not_marked_processed(self):
        with patch.object(tg_bot, "dispatch", AsyncMock(side_effect=RuntimeError("DB action failed"))):
            with self.assertRaises(RuntimeError): await self.send("call1:d:1", callback=True, update_id=77)
        async with self.sessions() as db:
            state = await db.get(AppSetting, tg_bot.state_key())
            self.assertTrue(state is None or 77 not in state.value.get("processed", []))
        await self.send("call1:d:1", callback=True, update_id=77)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count(CrmTask.id))), 3)

    async def test_leads_paginate_five(self):
        async with self.sessions() as db:
            db.add_all([CrmInbound(workspace_id=1, project_id=1, name=f"Заявка {i}", raw_payload={}) for i in range(10)])
            await db.commit()
        await self.send("leads:0", callback=True)
        markup = self.api.await_args_list[-1].args[1]["reply_markup"]
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(len([b for b in buttons if b.get("callback_data", "").startswith("show:")]), 5)
        self.assertIn("leads:1", [b.get("callback_data") for b in buttons])

    async def test_telegram_429_respects_retry_after_and_sanitizes_network_errors(self):
        rate = SimpleNamespace(status_code=429, json=lambda: {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}})
        success = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"id": 1}})
        client = AsyncMock(); client.post.side_effect = [rate, success]
        client.__aenter__.return_value = client
        with patch.object(notifications.httpx, "AsyncClient", return_value=client), patch.object(notifications.asyncio, "sleep", AsyncMock()) as sleep:
            self.assertEqual(await notifications.telegram_api("getMe"), {"id": 1})
            sleep.assert_awaited_once_with(3)
        client.post.side_effect = notifications.httpx.ConnectError("https://api.telegram.org/botSECRET/getMe")
        with patch.object(notifications.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(notifications.TelegramError) as error:
                await notifications.telegram_api("getMe")
        self.assertNotIn("SECRET", str(error.exception))

    async def test_worker_does_not_poll_when_another_process_has_lease(self):
        redis = AsyncMock(); redis.set.return_value = False; redis.__aenter__.return_value = redis
        with patch.object(tg_bot.Redis, "from_url", return_value=redis), patch.object(tg_bot.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError): await tg_bot.poll()
        self.api.assert_not_awaited()
        self.assertEqual(redis.set.call_args.kwargs, {"nx": True, "ex": 60})

    async def test_worker_polls_with_saved_offset_and_releases_lease(self):
        async with self.sessions() as db:
            db.add(AppSetting(key=tg_bot.state_key(), value={"offset": 99, "processed": [98]})); await db.commit()
        redis = AsyncMock(); redis.set.return_value = True; redis.__aenter__.return_value = redis
        self.api.side_effect = [{}, asyncio.CancelledError()]
        with patch.object(tg_bot.Redis, "from_url", return_value=redis), patch.object(tg_bot, "SessionLocal", self.sessions):
            with self.assertRaises(asyncio.CancelledError): await tg_bot.poll()
        call = self.api.await_args_list[-1]
        self.assertEqual(call.args[0], "getUpdates")
        self.assertEqual(call.args[1], {"offset": 99, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
        self.assertEqual(redis.eval.await_args.args[0], tg_bot.RELEASE)

    async def test_additive_migration_preserves_existing_link(self):
        from app.db_upgrade import upgrade_existing_schema
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE portal_users DROP COLUMN telegram_state"))
            await conn.run_sync(upgrade_existing_schema)
            await conn.run_sync(upgrade_existing_schema)  # repeat startup is safe
        async with self.sessions() as db:
            user = await db.get(PortalUser, 1)
            self.assertEqual(user.telegram_chat_id, "100")
            self.assertIsNone(user.telegram_state)

    async def test_mango_dial_uses_shared_core_and_duplicate_is_not_replayed(self):
        async with self.sessions() as db:
            deal = await db.get(CrmDeal, 1)
            (await db.get(CrmContact, deal.contact_id)).phones = ["+79990000001"]
            await db.commit()
        with patch("app.api.routes.telephony.dial_core", AsyncMock(return_value={"ok": True})) as dial:
            await self.send("dial:d:1", 200, callback=True, update_id=88)
            await self.send("dial:d:1", 200, callback=True, update_id=88)
            dial.assert_awaited_once()
            self.assertEqual(dial.call_args.args[1].phone, "+79990000001")
        async with self.sessions() as db:
            activity = await db.scalar(select(CrmActivity).where(CrmActivity.event_type == "TELEGRAM_ACTION"))
            self.assertEqual(activity.actor_id, 2)


if __name__ == "__main__":
    unittest.main()
