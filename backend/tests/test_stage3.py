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
from app.models.website import WebsiteSite
from app.api.routes import widget as widget_routes
from app.services import ai, call_ai, care, documents
import io
from openpyxl import Workbook
from app.models.marketing import ClientLead, ClientSale
from app.models.crm import CrmContact, CrmStage
from app.core.crypto import encrypt_secret
from app.models.crm import CareEvent, CrmTask
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services.messaging import WhatsAppGreen
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

    # ------------------------------------------------------------------ site widget
    def test_site_widget_callback_lead(self):
        async def add_site(db):
            db.add(WebsiteSite(id=5, workspace_id=1, project_id=1, name="romax63.ru", origin="https://romax63.ru", public_key="k" * 40))
            await db.commit()
        self.run_db(add_site)
        site_origin = {"Origin": "https://romax63.ru"}
        self.assertEqual(self.client.get("/api/website/widget/" + "k" * 40, headers=site_origin).status_code, 404)  # not enabled yet
        bad = self.client.put("/api/website/sites/5/widget", headers=ORIGIN, json={"enabled": True, "callback": False})
        self.assertEqual(bad.status_code, 422)
        saved = self.client.put("/api/website/sites/5/widget", headers=ORIGIN, json={
            "enabled": True, "title": "Замер бесплатно", "whatsapp": "+7 927 000-11-01", "telegram": "@romax63",
            "privacy_url": "https://romax63.ru/privacy", "color": "#E4572E"})
        self.assertEqual(saved.status_code, 200, saved.text)
        source_id = self.run_db(lambda db: db.get(WebsiteSite, 5)).widget["source_id"]
        # Re-saving keeps the same CRM source.
        self.client.put("/api/website/sites/5/widget", headers=ORIGIN, json={**saved.json()["widget"], "delay_sec": 5})
        self.assertEqual(self.run_db(lambda db: db.get(WebsiteSite, 5)).widget["source_id"], source_id)

        self.client.cookies.clear()
        conf = self.client.get("/api/website/widget/" + "k" * 40, headers=site_origin)
        self.assertEqual(conf.headers["access-control-allow-origin"], "https://romax63.ru")
        self.assertEqual((conf.json()["whatsapp"], conf.json()["telegram"], conf.json()["title"]), ("79270001101", "romax63", "Замер бесплатно"))
        self.assertNotIn("source_id", conf.json())
        self.assertEqual(self.client.get("/api/website/widget/" + "k" * 40, headers={"Origin": "https://evil.ru"}).status_code, 403)

        url = "/api/website/widget/" + "k" * 40 + "/lead"
        post = lambda body: self.client.post(url, headers={**site_origin, "Content-Type": "text/plain"}, content=json.dumps(body))
        with patch.object(widget_routes, "rate_limit", AsyncMock()):
            self.assertEqual(post({"phone": "8 927 555-12-34", "consent": False}).status_code, 422)
            self.assertEqual(post({"phone": "12", "consent": True}).status_code, 422)
            self.assertEqual(post({"phone": "89275551234", "consent": True, "website": "http://spam"}).status_code, 201)
            self.assertIsNone(self.run_db(lambda db: db.scalar(select(CrmDeal))))
            ok = post({"name": "Ирина", "phone": "8 (927) 555-12-34", "consent": True, "page": "https://romax63.ru/kuhni?utm_source=yandex",
                       "utm_source": "yandex", "utm_campaign": "kuhni-samara", "click_id": "998877665544", "click_type": "yclid"})
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(ok.headers["access-control-allow-origin"], "https://romax63.ru")
        deal = self.run_db(lambda db: db.scalar(select(CrmDeal)))
        self.assertIsNotNone(deal)
        self.as_user(1)
        card = self.client.get(f"/api/crm/deals/{deal.id}").json()
        self.assertEqual(card["contact"]["name"], "Ирина")
        self.assertIn("+79275551234", card["contact"]["phones"])
        self.assertEqual(card["attribution_snapshot"].get("utm_campaign") or card["form_data"].get("utm_campaign"), "kuhni-samara")

    # ------------------------------------------------------------------ client care
    def test_client_care_reminder_review_repeat(self):
        meeting = self.lead("Ирина Петрова", "89271112233", "care1")
        bought = self.lead("Олег", "89274445566", "care2")
        old = self.lead("Нина", "89277778899", "care3")
        now = datetime.now(timezone.utc)
        task = self.client.post("/api/crm/tasks", headers=ORIGIN, json={"project_id": 1, "deal_id": meeting, "type_code": "MEETING", "title": "Замер кухни",
                                                                        "due_at": (now + timedelta(hours=20)).isoformat()})
        self.assertIn(task.status_code, (200, 201), task.text)
        for deal_id, days in ((bought, 5), (old, 190)):
            r = self.client.post(f"/api/crm/deals/{deal_id}/sales", headers=ORIGIN, json={"amount": 100000, "occurred_at": (now - timedelta(days=days)).isoformat()})
            self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.client.put("/api/crm/projects/1/care", headers=ORIGIN, json={"review": True}).status_code, 422)
        saved = self.client.put("/api/crm/projects/1/care", headers=ORIGIN, json={
            "reminders": True, "review": True, "review_url": "https://yandex.ru/maps/org/romax/1", "repeat": True})
        self.assertEqual(saved.status_code, 200, saved.text)

        async def channel(db):
            db.add(MessagingChannel(id=3, workspace_id=1, project_id=1, kind="whatsapp", name="WhatsApp", active=True, status="connected",
                                    config={"instance_id": "1101"}, secret_encrypted=encrypt_secret("tok")))
            await db.commit()
        self.run_db(channel)
        send = AsyncMock(return_value="wamid-1")
        with patch.object(care, "working", return_value=True), patch.object(WhatsAppGreen, "send", send):
            self.assertEqual(self.run_db(care.run), 3)
            self.assertEqual(self.run_db(care.run), 0)  # each touch happens once
        texts = [call.args[1] for call in send.await_args_list]
        self.assertTrue(any(t.startswith("Здравствуйте, Ирина! Напоминаем:") and "Замер кухни" in t for t in texts), texts)
        self.assertTrue(any("https://yandex.ru/maps/org/romax/1" in t and t.startswith("Олег,") for t in texts), texts)
        events = self.run_db(lambda db: db.scalars(select(CareEvent).order_by(CareEvent.id))).all()
        self.assertEqual(sorted((e.kind, e.status) for e in events), [("reminder", "sent"), ("repeat", "task"), ("review", "sent")])
        repeat = self.run_db(lambda db: db.scalar(select(CrmTask).where(CrmTask.title == "Повторная продажа")))
        self.assertEqual(repeat.deal_id, old)
        authors = self.run_db(lambda db: db.scalars(select(Message.author_name).where(Message.direction == "out"))).all()
        self.assertEqual(set(authors), {"Автоматически"})
        stats = self.client.get("/api/crm/projects/1/care").json()["stats"]
        self.assertEqual((stats["reminder"]["sent"], stats["review"]["sent"], stats["repeat"]["task"]), (1, 1, 1))

    # ------------------------------------------------------------------ amoCRM import
    def amo_file(self):
        wb = Workbook(); ws = wb.active
        ws.append(["ID", "Название сделки", "Бюджет ₽", "Ответственный", "Дата создания", "Дата закрытия", "Этап сделки", "Воронка", "Теги",
                   "Основной контакт", "Компания", "Рабочий телефон (контакт)", "Мобильный телефон (контакт)", "Рабочий email (контакт)", "Примечание", "utm_source"])
        ws.append([101, "Кухня угловая", "350 000", "User 2", "01.09.2026 10:15:00", "20.09.2026 18:00:00", "Успешно реализовано", "Кухни", "сайт, рассрочка",
                   "Анна Смирнова", "", "+7 (927) 100-20-30", "", "anna@mail.ru", "Хочет фасады МДФ", "yandex"])
        ws.append([102, "Шкаф-купе", 90000, "User 2", datetime(2026, 9, 5, 12, 0), None, "Переговоры", "Кухни", "", "Анна Смирнова", "", "", "89271002030", "", "", ""])
        ws.append([103, "Кухня прямая", "", "Пётр", "07.09.2026", "08.09.2026", "Закрыто и не реализовано", "Кухни", "", "Борис", "", "", "", "", "", ""])
        ws.append([104, "Остров", 500000, "Неизвестный", "10.09.2026", "", "Первичный контакт", "Кухни", "", "Вера", "", "8 927 555 66 77", "", "", "", ""])
        buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

    def test_amocrm_import(self):
        content = self.amo_file()
        files = lambda: {"file": ("amocrm_export_leads_2026-10-03.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        self.as_user(2)
        self.assertEqual(self.client.post("/api/crm/projects/1/import/amocrm/preview", headers=ORIGIN, files=files()).status_code, 403)
        self.as_user(1)
        prev = self.client.post("/api/crm/projects/1/import/amocrm/preview", headers=ORIGIN, files=files())
        self.assertEqual(prev.status_code, 200, prev.text)
        p = prev.json()
        self.assertEqual((p["total"], p["no_contact"]), (4, 1))
        stages = {s["name"]: s["suggested"] for s in p["stages"]}
        types = {s["id"]: s["analytics_type"] for s in p["target_stages"]}
        self.assertEqual(types[stages["Успешно реализовано"]], "WON")
        self.assertEqual(types[stages["Закрыто и не реализовано"]], "LOST")
        self.assertEqual({u["name"]: u["suggested"] for u in p["users"]}["User 2"], 2)
        self.assertEqual(p["columns"]["phones"], "Рабочий телефон (контакт), Мобильный телефон (контакт)")
        options = {"pipeline_id": p["pipeline_id"], "stages": stages, "users": {"User 2": 2}, "default_user_id": 1, "create_sales": True}
        done = self.client.post("/api/crm/projects/1/import/amocrm", headers=ORIGIN, files=files(), data={"options": json.dumps(options)})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json(), {"created": 3, "skipped_duplicates": 0, "skipped_no_contact": 1, "contacts_new": 2, "contacts_merged": 1, "sales": 1})
        deals = self.run_db(lambda db: db.scalars(select(CrmDeal).order_by(CrmDeal.id))).all()
        self.assertEqual([d.name for d in deals], ["Кухня угловая", "Шкаф-купе", "Остров"])
        self.assertEqual(deals[0].contact_id, deals[1].contact_id)  # 8 927 100-20-30 == +7 (927) 100-20-30
        self.assertEqual((deals[0].responsible_user_id, deals[2].responsible_user_id), (2, 1))
        self.assertEqual(deals[0].tags, ["сайт", "рассрочка"])
        self.assertEqual(deals[0].attribution_snapshot["utm_source"], "yandex")
        self.assertEqual(deals[0].created_at.date().isoformat(), "2026-09-01")
        sale = self.run_db(lambda db: db.scalar(select(ClientSale)))
        self.assertEqual((float(sale.amount), sale.occurred_at.date().isoformat()), (350000.0, "2026-09-20"))
        self.assertEqual(self.run_db(lambda db: db.get(ClientLead, deals[0].lead_id)).status, "won")
        again = self.client.post("/api/crm/projects/1/import/amocrm", headers=ORIGIN, files=files(), data={"options": json.dumps(options)}).json()
        self.assertEqual((again["created"], again["skipped_duplicates"]), (0, 3))
        bad = self.client.post("/api/crm/projects/1/import/amocrm/preview", headers=ORIGIN, files={"file": ("x.txt", b"hello", "text/plain")})
        self.assertEqual(bad.status_code, 422)


del Base  # keep stage-2 tests from being collected twice

if __name__ == "__main__":
    unittest.main()
