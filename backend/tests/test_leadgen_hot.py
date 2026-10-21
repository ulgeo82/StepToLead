"""Триггер «Явный интерес»: признаки, задача 15 минут, этап «Интерес», Telegram, напоминание, отмена человеком."""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.domains.leadgen import hot, inbox, service
from app.models.crm import CrmDeal, CrmStage, CrmTask
import test_leadgen_inbox as base


class HotRuleTests(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(hot.rule_signals("Давайте созвонимся, мой номер +7 927 123-45-67"), ["phone", "call"])
        self.assertEqual(hot.rule_signals("Сколько стоит и можно попробовать?"), ["price", "trial"])
        self.assertEqual(hot.rule_signals("Не звоните, не интересно"), [])
        self.assertTrue(hot.is_hot(None, ["call"], 70))
        self.assertTrue(hot.is_hot(80, [], 70))
        self.assertFalse(hot.is_hot(40, ["price"], 70))


class HotFlowTests(base.Base_):

    def test_hot_by_ai_task_stage_telegram_and_cool(self):
        tg = AsyncMock(return_value=1)
        ids = self.seed_replies()
        with patch("app.services.monitor.send", tg):
            async def fn(db):
                company = await db.get(base.LgCompany, ids["a"])
                touch = await self.touch_of(db, company.id)
                touch.label_source = "rule"
                res = await inbox.classify(db, touch, complete=base.fake_model(
                    '{"label": "interested", "summary": "Просят цену", "score": 85, "signals": ["price"]}'))
                deal = await db.get(CrmDeal, company.crm_deal_id)
                stage = await db.get(CrmStage, deal.stage_id)
                task = await db.get(CrmTask, touch.hot_task_id)
                hot_state = (touch.hot_at is not None, stage.name, task.priority, task.status)
                due_minutes = (service._aware(task.due_at) - service._aware(touch.hot_at))
                # человек решил: не горячий — задача закрыта, сделка на «Ответил»
                await inbox.apply_label(db, touch, company, "question")
                stage2 = await db.get(CrmStage, (await db.get(CrmDeal, company.crm_deal_id)).stage_id)
                return res, hot_state, due_minutes, (await db.get(CrmTask, task.id)).status, stage2.name, touch.hot_at
            res, state, due, task_status, stage_after, hot_after = self.run_db(fn)
        self.assertEqual(res["score"], 85)
        self.assertEqual(state, (True, "Интерес", "HIGH", "OPEN"))
        self.assertGreaterEqual(due, timedelta(minutes=15))
        self.assertTrue(tg.await_count >= 1)
        self.assertEqual((task_status, stage_after, hot_after), ("CANCELLED", "Ответил", None))

    def test_reminder_once_after_deadline(self):
        tg = AsyncMock(return_value=1)
        ids = self.seed_replies()
        with patch("app.services.monitor.send", tg):
            async def fn(db):
                company = await db.get(base.LgCompany, ids["a"])
                touch = await self.touch_of(db, company.id)
                touch.label_source, touch.label = "rule", "interested"
                await hot.mark(db, touch, company, score=None, signals=["call"], now=base.NOW)
                later = base.NOW + timedelta(days=3)
                return await hot.remind_due(db, now=later), await hot.remind_due(db, now=later + timedelta(hours=1))
            first, second = self.run_db(fn)
        self.assertEqual((first, second), (1, 0))


if __name__ == "__main__":
    unittest.main()
