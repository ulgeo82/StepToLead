"""Audit stage 3: AI call analysis, offers and invoices."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import test_stage2
from app.core.config import settings
from app.models.crm import CrmActivity, CrmDeal, CrmDocument
from app.models.telephony import Call
from app.services import ai, call_ai, documents
from app.services.telephony import Mango

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class Stage3Tests(unittest.TestCase):
    setUp, tearDown, as_user, run_db, lead = Base.setUp, Base.tearDown, Base.as_user, Base.run_db, Base.lead

    # ------------------------------------------------------------------ calls
    def test_call_analysis_pipeline(self):
        deal_id = self.lead("Марина", "89270001101", "a1")
        with patch.object(Mango, "users", AsyncMock(return_value=[{"extension": "101", "name": "User 2", "numbers": []}])):
            conn = self.client.post("/api/crm/projects/1/telephony", headers=ORIGIN, json={"api_key": "key-123", "api_salt": "salt-456"}).json()
        records = tempfile.TemporaryDirectory()
        Path(records.name, "1").mkdir()
        Path(records.name, "1", "c.mp3").write_bytes(b"ID3" + b"\0" * 400)

        async def add_call(db):
            call = Call(workspace_id=1, project_id=1, connection_id=conn["id"], entry_id="e1", direction="out", status="answered",
                        client_phone="79270001101", user_id=2, started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                        duration_sec=284, deal_id=deal_id, recording_id="r1", recording_path="1/c.mp3", processed=True, meta={})
            db.add(call); await db.commit()
            return call.id
        call_id = self.run_db(add_call)

        segments = [{"ch": "0", "start": 0, "text": "Здравствуйте, это студия кухонь Ромакс, меня зовут Егор."},
                    {"ch": "1", "start": 5, "text": "Нужна угловая кухня три двадцать, ключи в ноябре."},
                    {"ch": "0", "start": 40, "text": "Запишу вас на бесплатный замер в субботу в одиннадцать."}]
        answer = json.dumps({"summary": "Клиентке нужна угловая кухня, записана на замер.", "need": "Угловая кухня 3,2 м",
                             "next_step": "Замер в субботу 11:00", "objections": [], "client_mood": "Позитив",
                             "checklist": [{"item": x, "ok": i != 3, "comment": ""} for i, x in enumerate(call_ai.DEFAULT_CHECKLIST)],
                             "advice": "Уточняйте бюджет."}, ensure_ascii=False)
        complete = AsyncMock(return_value=f"Вот разбор:\n```json\n{answer}\n```")
        with patch.object(settings, "recordings_dir", records.name), patch.object(settings, "stt_provider", "openai"), \
                patch.object(settings, "stt_api_key", "sk-test"), patch.object(settings, "llm_provider", "anthropic"), \
                patch.object(settings, "llm_api_key", "sk-ant"), \
                patch.object(call_ai, "whisper", AsyncMock(return_value=segments)), patch.object(ai, "complete", complete):
            self.assertEqual(self.run_db(call_ai.process), 1)
            detail = self.client.get(f"/api/crm/calls/{call_id}/ai").json()
            # Re-analysis on demand is queued again.
            self.assertEqual(self.client.post(f"/api/crm/calls/{call_id}/ai", headers=ORIGIN).json(), {"status": "queued"})
        records.cleanup()
        analysis = detail["analysis"]
        self.assertEqual(analysis["status"], "done")
        self.assertEqual(analysis["score"], 80)
        self.assertEqual(analysis["client_mood"], "позитив")
        self.assertIn("[0:40]", analysis["transcript"])
        self.assertIn("Голос 1", analysis["transcript"])
        self.assertIn("Расшифровка звонка (4 мин)", complete.await_args.args[1][0]["content"])
        timeline = self.run_db(lambda db: db.scalars(select(CrmActivity).where(CrmActivity.event_type == "CALL_ANALYZED"))).all()
        self.assertEqual(timeline[0].deal_id, deal_id)
        self.assertEqual(timeline[0].payload["score"], 80)
        deal_calls = self.client.get(f"/api/crm/deals/{deal_id}/calls").json()
        self.assertEqual(deal_calls[0]["ai"]["status"], "queued")

    def test_call_analysis_disabled_and_errors(self):
        self.assertFalse(call_ai.available())
        state = call_ai.project_settings(None)
        self.assertTrue(state["enabled"]); self.assertEqual(len(state["checklist"]), 5)
        raw = "\n".join(json.dumps({"result": r}) for r in [
            {"channelTag": "0", "final": {"alternatives": [{"text": "алло", "startTimeMs": "1000"}]}},
            {"channelTag": "0", "finalRefinement": {"normalizedText": {"alternatives": [{"text": "Алло.", "startTimeMs": "1000"}]}}},
            {"channelTag": "1", "finalRefinement": {"normalizedText": {"alternatives": [{"text": "Добрый день!", "startTimeMs": "3500"}]}}}])
        self.assertEqual(call_ai.parse_yandex(raw), [{"ch": "0", "start": 1, "text": "Алло."}, {"ch": "1", "start": 3, "text": "Добрый день!"}])
        call = Call(meta={}, ai_status="queued")
        for _ in range(3):
            call_ai._fail(call, "сбой")
        self.assertEqual(call.ai_status, "error")
        self.assertEqual(call_ai.public(call)["error"], "сбой")

    # ------------------------------------------------------------------ documents
    def test_offer_invoice_public_link_and_payment(self):
        deal_id = self.lead("Ольга", "89270002202", "d1")
        self.assertTrue(documents.amount_in_words(Decimal("125000.50")).lower().startswith("сто двадцать пять тысяч"))
        offer = self.client.post(f"/api/crm/deals/{deal_id}/documents", headers=ORIGIN, json={
            "kind": "offer", "title": "Кухня с островом", "update_amount": True,
            "items": [{"name": "Корпус и фасады МДФ", "qty": 1, "unit": "компл.", "price": 300000, "discount": 10},
                      {"name": "Монтаж", "qty": 1, "price": 30000}]})
        self.assertEqual(offer.status_code, 201, offer.text)
        offer = offer.json()
        self.assertEqual((offer["number"], offer["total"], offer["buyer"]["name"]), (1, 300000.0, "Ольга"))
        self.assertTrue(offer["url"].endswith(f"/d/{self.run_db(lambda db: db.scalar(select(CrmDocument.public_token)))}"))
        self.assertEqual(self.run_db(lambda db: db.get(CrmDeal, deal_id)).amount, 300000)

        blocked = self.client.post(f"/api/crm/deals/{deal_id}/documents", headers=ORIGIN, json={"kind": "invoice", "items": [{"name": "Предоплата", "price": 150000}]})
        self.assertEqual(blocked.status_code, 422)
        self.as_user(2)
        self.assertEqual(self.client.put("/api/crm/projects/1/requisites", headers=ORIGIN, json={"company": "ООО Ромакс"}).status_code, 403)
        self.as_user(1)
        saved = self.client.put("/api/crm/projects/1/requisites", headers=ORIGIN, json={
            "company": "ООО «Ромакс»", "inn": "6316000000", "kpp": "631601001", "bank": "ПАО Сбербанк", "bik": "043601607",
            "account": "40702810000000000001", "corr_account": "30101810200000000607", "director": "Иванов И. И.", "vat": "20"})
        self.assertEqual(saved.status_code, 200, saved.text)
        invoice = self.client.post(f"/api/crm/deals/{deal_id}/documents", headers=ORIGIN, json={
            "kind": "invoice", "items": [{"name": "Предоплата 50% по договору", "price": 150000}]}).json()
        self.assertEqual(invoice["number"], 1)

        listing = self.client.get(f"/api/crm/deals/{deal_id}/documents").json()
        self.assertEqual(len(listing["items"]), 2)
        self.assertTrue(listing["invoice_ready"])
        self.assertEqual(listing["catalog"][0]["name"], "Предоплата 50% по договору")

        token = invoice["url"].rsplit("/", 1)[1]
        staff = self.client.get(f"/api/public/documents/{token}")
        self.assertIn("ООО «Ромакс»", staff.text)
        self.assertEqual(self.client.get(f"/api/crm/deals/{deal_id}/documents").json()["items"][0]["views"], 0)
        self.client.cookies.clear()
        page = self.client.get(f"/api/public/documents/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Сто пятьдесят тысяч рублей", page.text)
        self.assertIn("В том числе НДС (20%):", page.text)
        self.assertIn("25 000,00", page.text)
        self.assertEqual(self.client.get("/api/public/documents/" + "x" * 32).status_code, 404)
        self.as_user(1)
        doc = [d for d in self.client.get(f"/api/crm/deals/{deal_id}/documents").json()["items"] if d["kind"] == "invoice"][0]
        self.assertEqual((doc["status"], doc["views"]), ("viewed", 1))
        events = self.run_db(lambda db: db.scalars(select(CrmActivity.event_type).where(CrmActivity.deal_id == deal_id))).all()
        self.assertIn("DOCUMENT_VIEWED", events)

        paid = self.client.put(f"/api/crm/documents/{doc['id']}", headers=ORIGIN, json={"status": "paid"}).json()
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(self.client.delete(f"/api/crm/documents/{doc['id']}", headers=ORIGIN).status_code, 422)
        self.client.put(f"/api/crm/documents/{offer['id']}", headers=ORIGIN, json={"status": "canceled"})
        self.client.cookies.clear()
        self.assertEqual(self.client.get(offer["url"].replace("http://localhost:3000/d/", "/api/public/documents/")).status_code, 404)


del Base  # keep stage-2 tests from being collected twice

if __name__ == "__main__":
    unittest.main()
