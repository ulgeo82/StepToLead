"""Лидогенерация: нормализация, склейка, скоринг, can_contact — ядро и сервисы на SQLite."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app import models  # noqa: F401  (регистрирует все таблицы)
from app.domains.leadgen import service
from app.domains.leadgen.core.contact_policy import CompanyState, ContactState, DncEntry, can_contact
from app.domains.leadgen.core.matching import Candidate, Finding, plan_match
from app.domains.leadgen.core.normalize import (
    clean_company_name, is_platform_domain, name_key, normalize_domain, normalize_phone, validate_inn,
)
from app.domains.leadgen.core.scoring import compute_score
from app.domains.leadgen.models import LgCompany, LgCompanyKey, LgContact, LgDnc, LgSignal, LgTouch
from app.models.marketing import ClientWorkspace

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
INN_A = "7707083893"


def inn12(first10: str) -> str:
    d = [int(c) for c in first10]
    d.append(sum(a * b for a, b in zip(d, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10)
    d.append(sum(a * b for a, b in zip(d, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10)
    return "".join(map(str, d))


INN_B = inn12("5001003001")


class NormalizeTests(unittest.TestCase):
    def test_domain(self):
        cases = {
            "https://www.Kuhni-Sever.ru/contacts?x=1": "kuhni-sever.ru",
            "kuhni-sever.ru": "kuhni-sever.ru",
            "http://kuhni-sever.ru:8080/": "kuhni-sever.ru",
            "WWW.kuhni-sever.ru.": "kuhni-sever.ru",
            "https://кухни-север.рф/": "xn----dtbibllv3afqw.xn--p1ai",
            "https://mysite.tilda.ws/page": "mysite.tilda.ws",
            "": None, None: None, "localhost": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_domain(raw), expected, raw)

    def test_platforms(self):
        self.assertTrue(is_platform_domain("ekaterinburg.avito.ru"))
        self.assertFalse(is_platform_domain("notavito.ru"))

    def test_phone(self):
        for raw in ("8 (912) 345-67-89", "+7 912 345 67 89", "79123456789", "9123456789"):
            self.assertEqual(normalize_phone(raw), "+79123456789", raw)
        for raw in ("+380 44 123 4567", "12345", None):
            self.assertIsNone(normalize_phone(raw))

    def test_inn(self):
        self.assertEqual(validate_inn("7707 083 893"), INN_A)
        self.assertIsNone(validate_inn("7707083894"))
        self.assertEqual(validate_inn(INN_B), INN_B)
        self.assertIsNone(validate_inn(INN_B[:-1] + str((int(INN_B[-1]) + 1) % 10)))
        self.assertIsNone(validate_inn("0000000000"))

    def test_names(self):
        self.assertEqual(clean_company_name("ООО «Кухни  Северная»"), "Кухни Северная")
        self.assertEqual(clean_company_name("Мебель-Лайн ООО"), "Мебель-Лайн")
        self.assertEqual(name_key("ООО «Кухни Северная»", "Екатеринбург"), "кухни северная|екатеринбург")


class ScoringTests(unittest.TestCase):
    def test_sum_dedup_and_clamp(self):
        self.assertEqual(compute_score(["ad_direct", "has_messenger", "site_quiz", "site_quiz"]).score, 6)
        self.assertEqual(compute_score(["ad_direct", "ad_premium", "new_advertiser", "has_messenger",
                                        "site_quiz", "no_crm", "legal_active", "hh_marketing"]).score, 10)
        self.assertEqual(compute_score(["chain"]).score, 0)

    def test_hard_rules(self):
        kinds = ["ad_direct", "has_messenger"]
        self.assertEqual(compute_score(kinds, in_dnc=True).score, 0)
        self.assertEqual(compute_score(kinds, legal_status="liquidated").score, 0)
        self.assertEqual(compute_score(kinds, fit_label="no").score, 2)


class MatchingTests(unittest.TestCase):
    @staticmethod
    def index(rows):
        return lambda kind, value: [c for k, v, c in rows if k == kind and v == value]

    def test_order_and_conflicts(self):
        idx = self.index([("inn", INN_A, Candidate("A")), ("domain", "x.ru", Candidate("B", inn=INN_A))])
        self.assertEqual(plan_match(Finding(inn=INN_A, domain="x.ru"), idx).company_id, "A")
        r = plan_match(Finding(domain="x.ru", inn=INN_B), self.index([("domain", "x.ru", Candidate("B", inn=INN_A))]))
        self.assertEqual((r.action, r.reason), ("review", "domain_inn_conflict"))

    def test_phone_city(self):
        idx = self.index([("phone", "+79123456789", Candidate("A", city="Екатеринбург"))])
        self.assertEqual(plan_match(Finding(phones=["89123456789"], city="екатеринбург"), idx).action, "attach")
        self.assertEqual(plan_match(Finding(phones=["89123456789"], city="Казань"), idx).reason, "phone_other_city")

    def test_name_only_suggests_and_platform_skipped(self):
        idx = self.index([("name_city", "кухни северная|екатеринбург", Candidate("A"))])
        r = plan_match(Finding(name="Кухни Северная", city="Екатеринбург"), idx)
        self.assertEqual((r.action, r.suggestions), ("create", ["A"]))
        self.assertEqual(plan_match(Finding(domain="https://www.avito.ru/x"), self.index([])).action, "skip")


class ContactPolicyTests(unittest.TestCase):
    def company(self, **kw):
        base = dict(id="1", stage="ready", domain="kuhni.ru")
        base.update(kw)
        return CompanyState(**base)

    def test_refusals(self):
        cases = [
            (dict(stage="rejected"), None, [], "stage:rejected"),
            ({}, None, [DncEntry("domain", "kuhni.ru")], "dnc:domain"),
            ({}, ContactState("email", "Info@Other.ru"), [DncEntry("email", "info@other.ru")], "dnc:email"),
            ({}, ContactState("whatsapp", "8 912 345-67-89"), [DncEntry("phone", "+79123456789")], "dnc:phone"),
            (dict(is_client=True), None, [], "crm:client"),
            (dict(has_open_sales_deal=True), None, [], "crm:open_deal"),
            (dict(last_touch_at=NOW - timedelta(days=10)), None, [], "recently_contacted"),
            ({}, ContactState("email", "a@kuhni.ru", bounced=True), [], "contact:invalid"),
            ({}, ContactState("email", "ivan@kuhni.ru", is_personal=True), [], "contact:personal_while_shared_exists"),
            (dict(active_sequence_id="S1"), None, [], "sequence:another_active"),
        ]
        for comp, contact, dnc, expected in cases:
            d = can_contact(self.company(**comp), contact, "email", dnc=dnc, now=NOW)
            self.assertEqual((d.ok, d.reason), (False, expected), expected)

    def test_allowed(self):
        self.assertTrue(can_contact(self.company(), None, "email", dnc=[], now=NOW).ok)
        expired = [DncEntry("company", "1", until=NOW - timedelta(days=1))]
        self.assertTrue(can_contact(self.company(last_touch_at=NOW - timedelta(days=120)), None, "email",
                                    dnc=expired, now=NOW).ok)
        self.assertTrue(can_contact(self.company(active_sequence_id="S1", last_touch_at=NOW - timedelta(days=3)),
                                    None, "email", dnc=[], now=NOW, sequence_id="S1").ok)


class LeadgenServiceTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=1, name="StepToLead Agency"))
                await db.commit()
        asyncio.run(seed())

    def tearDown(self):
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                result = await fn(db)
                await db.commit()
                return result
        return asyncio.run(go())

    def finding(self, **kw):
        base = dict(source="yandex_direct", name="ООО «Кухни Северная»", domain="https://www.kuhni-sever.ru/",
                    city="Екатеринбург", niche="Кухни на заказ")
        base.update(kw)
        return service.FindingIn(**base)

    def test_upsert_creates_then_attaches_by_domain(self):
        async def fn(db):
            first = await service.upsert_company(db, 1, self.finding(phones=["8 912 345-67-89"]), now=NOW)
            second = await service.upsert_company(
                db, 1, self.finding(source="site", domain="kuhni-sever.ru", inn=INN_A,
                                    contacts=[{"kind": "email", "value": "Info@Kuhni-Sever.ru"},
                                              {"kind": "telegram", "value": "https://t.me/kuhni_sever"}]),
                now=NOW)
            count = await db.scalar(select(func.count(LgCompany.id)))
            contacts = (await db.execute(select(LgContact.kind, LgContact.value_norm))).all()
            return first, second, count, set(contacts)
        first, second, count, contacts = self.run_db(fn)
        self.assertTrue(first.created)
        self.assertEqual((second.match.action, second.match.matched_by), ("attach", "domain"))
        self.assertEqual(count, 1)
        self.assertEqual(second.company.display_name, "Кухни Северная")
        self.assertEqual(second.company.inn, INN_A)
        self.assertEqual(second.company.provenance["inn"]["source"], "site")
        self.assertIn(("email", "info@kuhni-sever.ru"), contacts)
        self.assertIn(("telegram", "kuhni_sever"), contacts)
        self.assertIn(("phone", "+79123456789"), contacts)

    def test_higher_rank_source_wins_lower_does_not_overwrite(self):
        async def fn(db):
            await service.upsert_company(db, 1, self.finding(source="dadata", inn=INN_A, legal_name="ООО «Северная мебель»"), now=NOW)
            r = await service.upsert_company(db, 1, self.finding(inn=INN_A, legal_name="Кухни какие-то"), now=NOW)
            return r.company.legal_name
        self.assertEqual(self.run_db(fn), "ООО «Северная мебель»")

    def test_platform_skipped_and_phone_other_city_review(self):
        async def fn(db):
            skipped = await service.upsert_company(db, 1, self.finding(domain="https://www.avito.ru/ekb"), now=NOW)
            await service.upsert_company(db, 1, self.finding(domain=None, phones=["89123456789"]), now=NOW)
            other = await service.upsert_company(
                db, 1, self.finding(name="Окна Плюс", domain=None, phones=["89123456789"], city="Казань"), now=NOW)
            return skipped, other
        skipped, other = self.run_db(fn)
        self.assertIsNone(skipped.company)
        self.assertTrue(other.created)
        self.assertTrue(other.company.needs_review.startswith("phone_other_city:"))

    def test_domain_inn_conflict_creates_flagged_company(self):
        async def fn(db):
            a = await service.upsert_company(db, 1, self.finding(inn=INN_A), now=NOW)
            b = await service.upsert_company(db, 1, self.finding(inn=INN_B), now=NOW)
            domain_keys = await db.scalar(select(func.count(LgCompanyKey.id)).where(LgCompanyKey.kind == "domain"))
            return a, b, domain_keys
        a, b, domain_keys = self.run_db(fn)
        self.assertNotEqual(a.company.id, b.company.id)
        self.assertEqual(b.company.needs_review, f"domain_inn_conflict:{a.company.id}")
        self.assertEqual(domain_keys, 1)

    def test_signals_score_and_dnc(self):
        async def fn(db):
            c = (await service.upsert_company(db, 1, self.finding(), now=NOW)).company
            await service.add_signal(db, c, "ad_direct", now=NOW)
            await service.add_signal(db, c, "has_messenger", now=NOW)
            before = c.score
            db.add(LgDnc(workspace_id=1, kind="domain", value_norm="kuhni-sever.ru", reason="refused"))
            await db.flush()
            after = await service.recalc_score(db, c, now=NOW)
            return before, after, c.score_reasons
        before, after, reasons = self.run_db(fn)
        self.assertEqual(before, 5)
        self.assertEqual(after, 0)
        self.assertEqual(reasons[-1]["label"], "В стоп-листе")

    def test_merge_moves_everything(self):
        async def fn(db):
            shared_email = [{"kind": "email", "value": "info@kuhni-sever.ru"}]
            a = (await service.upsert_company(db, 1, self.finding(phones=["89120000001"], contacts=shared_email),
                                              now=NOW)).company
            b = (await service.upsert_company(db, 1, self.finding(
                source="dadata", name="Северная", domain="severnaya-mebel.ru", inn=INN_A,
                phones=["89120000002", "89120000003"], contacts=shared_email), now=NOW)).company
            self.assertNotEqual(a.id, b.id)
            await service.add_signal(db, b, "ad_premium", now=NOW)
            db.add(LgTouch(company_id=b.id, channel="email", direction="out", status="sent", happened_at=NOW))
            await db.flush()
            kept = await service.merge_companies(db, a.id, b.id, reason="manual", now=NOW)
            companies = await db.scalar(select(func.count(LgCompany.id)))
            phones = await db.scalar(select(func.count(LgContact.id)).where(LgContact.company_id == a.id,
                                                                            LgContact.kind == "phone"))
            emails = await db.scalar(select(func.count(LgContact.id)).where(LgContact.kind == "email"))
            merged = await db.scalar(select(func.count(LgSignal.id)).where(LgSignal.kind == "merged"))
            touches = await db.scalar(select(func.count(LgTouch.id)).where(LgTouch.company_id == a.id))
            return kept, companies, phones, emails, merged, touches
        kept, companies, phones, emails, merged, touches = self.run_db(fn)
        self.assertEqual((companies, phones, emails, merged, touches), (1, 3, 1, 1, 1))
        self.assertEqual(kept.inn, INN_A)
        self.assertEqual(kept.score, 2)

    def test_merge_refuses_different_inn(self):
        async def fn(db):
            a = (await service.upsert_company(db, 1, self.finding(inn=INN_A), now=NOW)).company
            b = (await service.upsert_company(db, 1, self.finding(domain="other.ru", inn=INN_B), now=NOW)).company
            with self.assertRaises(ValueError):
                await service.merge_companies(db, a.id, b.id, reason="manual", now=NOW)
        self.run_db(fn)

    def test_check_can_contact_with_db_state(self):
        async def fn(db):
            await service.ensure_settings(db)
            c = (await service.upsert_company(db, 1, self.finding(contacts=[
                {"kind": "email", "value": "info@kuhni-sever.ru"},
                {"kind": "email", "value": "ivan@kuhni-sever.ru", "is_personal": True}]), now=NOW)).company
            shared, personal = (await db.execute(select(LgContact).order_by(LgContact.id))).scalars().all()
            ok = await service.check_can_contact(db, c.id, shared.id, "email", now=NOW)
            blocked_personal = await service.check_can_contact(db, c.id, personal.id, "email", now=NOW)
            c.stage = "in_outreach"
            db.add(LgTouch(company_id=c.id, contact_id=shared.id, channel="email", direction="out",
                           status="sent", sequence_id="S1", happened_at=NOW - timedelta(days=3)))
            await db.flush()
            followup = await service.check_can_contact(db, c.id, shared.id, "email", sequence_id="S1", now=NOW)
            other_seq = await service.check_can_contact(db, c.id, shared.id, "email", sequence_id="S2", now=NOW)
            return ok, blocked_personal, followup, other_seq
        ok, personal, followup, other_seq = self.run_db(fn)
        self.assertTrue(ok.ok)
        self.assertEqual(personal.reason, "contact:personal_while_shared_exists")
        self.assertTrue(followup.ok)
        self.assertEqual(other_seq.reason, "sequence:another_active")


if __name__ == "__main__":
    unittest.main()
