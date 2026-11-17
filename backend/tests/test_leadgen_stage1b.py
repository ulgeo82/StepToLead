"""SQLite tests for the shared warmup pool and suppression list. No network."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.domains.leadgen import inbox, sequences, service, warmup
from app.domains.leadgen.models import LgContact, LgMailbox, LgTouch, LgWarmupMail
from test_leadgen_spaces import AS_CLIENT, BASE, CLIENT, GONE, HOME, NOW, ORIGIN, SpaceBase, Transport


def mailbox(workspace_id, email, **kwargs):
    return LgMailbox(workspace_id=workspace_id, email=email, smtp_host="smtp.test", imap_host="imap.test",
                     login=email, password_encrypted="", **kwargs)


class SharedPoolTests(SpaceBase):
    def setUp(self):
        super().setUp()
        decrypt = patch.object(warmup, "decrypt_secret", return_value="")
        decrypt.start()
        self.addCleanup(decrypt.stop)

    def test_cross_workspace_pool_excludes_inactive_and_same_domain(self):
        transport = Transport()

        async def run(db):
            boxes = [mailbox(HOME, "info@a.example"), mailbox(CLIENT, "info@b.example"),
                     mailbox(CLIENT, "sales@A.example"), mailbox(GONE, "info@c.example"),
                     mailbox(CLIENT, "info@d.example", warmup_enabled=False)]
            db.add_all(boxes)
            await db.flush()
            await warmup.run_tick(db, transport, now=NOW)
            rows = (await db.scalars(select(LgWarmupMail))).all()
            owners = {box.id: box.workspace_id for box in boxes}
            self.assertTrue(all(row.workspace_id == owners[row.from_mailbox_id] for row in rows))

        self.run_db(run)
        pairs = {(mail.sender, mail.to) for mail in transport.sent}
        self.assertIn(("info@a.example", "info@b.example"), pairs)
        self.assertIn(("sales@A.example", "info@b.example"), pairs)
        self.assertEqual(len(pairs), 3)
        for sender, recipient in pairs:
            self.assertNotEqual(sender.split("@")[1].lower(), recipient.split("@")[1].lower())
            self.assertNotIn(sender, ("info@c.example", "info@d.example"))
            self.assertNotIn(recipient, ("info@c.example", "info@d.example"))

    def test_same_domain_and_opt_out_also_block_scheduled_replies(self):
        transport = Transport()

        async def run(db):
            a, b, c = (mailbox(HOME, "info@a.example"), mailbox(CLIENT, "sales@a.example"),
                       mailbox(CLIENT, "info@b.example", warmup_shared_pool=False))
            db.add_all([a, b, c])
            await db.flush()
            replies = [LgWarmupMail(workspace_id=HOME, from_mailbox_id=a.id, to_mailbox_id=target.id,
                                    subject="Ответ", body="Ответ", status="scheduled", send_after=NOW)
                       for target in (b, c)]
            db.add_all(replies)
            await db.flush()
            self.assertEqual(await warmup.send_due(db, transport, HOME, now=NOW), 0)
            self.assertTrue(all(row.status == "failed" for row in replies))
            self.assertEqual(await warmup.send_due(db, transport, CLIENT, now=NOW), 2)

        self.run_db(run)
        self.assertEqual({(mail.sender, mail.to) for mail in transport.sent},
                         {("sales@a.example", "info@b.example"), ("info@b.example", "sales@a.example")})

    def test_tracked_cross_workspace_warmup_is_not_a_lead_reply(self):
        transport = Transport()

        async def run(db):
            sender, recipient = mailbox(HOME, "info@a.example"), mailbox(CLIENT, "info@b.example")
            db.add_all([sender, recipient])
            await db.flush()
            self.assertEqual(await warmup.send_due(db, transport, HOME, now=NOW), 1)
            mail = sequences.IncomingMail(1, sender.email, "Письмо", "Текст", transport.sent[0].message_id)
            untracked = sequences.IncomingMail(2, sender.email, "Ответ", "Текст", "<untracked@example>")
            transport.fetch = AsyncMock(return_value=[mail, untracked])
            handler = AsyncMock(return_value="reply")
            with patch.object(sequences, "handle_inbound", handler), patch.object(
                sequences, "decrypt_secret", return_value=""
            ):
                result = await sequences.poll_mailbox(db, transport, recipient, now=NOW + timedelta(minutes=1))
            self.assertEqual(result, {"warmup": 1, "reply": 1})
            self.assertIs(handler.await_args.args[2], untracked)

        self.run_db(run)

    def test_overview_hides_partner_addresses_and_pool_flag_is_scoped(self):
        async def seed(db):
            own = mailbox(HOME, "info@ours.example")
            foreign = mailbox(CLIENT, "info@partner.example")
            foreign.smtp_host = "smtp.private-partner.example"
            db.add_all([own, foreign])
            await db.flush()
            return own.id, foreign.id

        own, foreign = self.run_db(seed)
        result = self.get("/warmup").json()
        self.assertEqual(result["pool"]["partners"], 1)
        self.assertTrue(result["running"])
        self.assertEqual([item["id"] for item in result["items"]], [own])
        self.assertNotIn("partner.example", str(result))
        self.assertTrue(result["items"][0]["warmup_shared_pool"])
        self.assertEqual(self.client.patch(f"{BASE}/mailboxes/{foreign}", headers=ORIGIN,
                                          json={"warmup_shared_pool": False}).status_code, 404)
        change = self.client.patch(f"{BASE}/mailboxes/{own}", headers=ORIGIN,
                                   json={"warmup_shared_pool": False})
        self.assertEqual(change.status_code, 200)
        self.assertFalse(change.json()["warmup_shared_pool"])
        self.assertEqual(self.get("/warmup").json()["pool"]["partners"], 0)


class SharedDncTests(SpaceBase):
    def test_unsubscribe_shared_refusal_local_and_existing_refusal_promoted(self):
        company_id = self.company(CLIENT, domain="target.example")

        async def can_contact(db):
            contact = await db.scalar(select(LgContact).where(LgContact.company_id == company_id))
            return await service.check_can_contact(db, company_id, contact.id, "email", now=NOW)

        value = "info@target.example"
        self.assertEqual(self.post("/dnc", {"kind": "email", "value": value, "reason": "refused"}).status_code, 201)
        self.assertTrue(self.run_db(can_contact).ok)
        self.assertEqual(self.get("/dnc", AS_CLIENT).json()["items"], [])

        async def opt_out(db):
            await sequences._dnc(db, HOME, "email", value, "unsubscribed")

        self.run_db(opt_out)
        self.assertEqual(self.run_db(can_contact).reason, "dnc:email")
        row, = self.get("/dnc", AS_CLIENT).json()["items"]
        self.assertTrue(row["shared"])
        self.assertFalse(row["can_delete"])
        self.assertIsNone(row["note"])
        self.assertEqual(self.client.delete(f"{BASE}/dnc/{row['id']}", headers=AS_CLIENT).status_code, 404)
        self.post("/dnc", {"kind": "email", "value": value, "reason": "refused"})
        self.assertEqual(self.run_db(can_contact).reason, "dnc:email")

    def test_domain_opt_out_blocks_contact_policy_and_manual_reply(self):
        company_id = self.company(CLIENT, domain="target.example")
        row = self.post("/dnc", {"kind": "domain", "value": "target.example", "reason": "unsubscribed"})
        self.assertEqual(row.status_code, 201)

        async def run(db):
            from app.domains.leadgen.models import LgCompany

            contact = await db.scalar(select(LgContact).where(LgContact.company_id == company_id))
            decision = await service.check_can_contact(db, company_id, contact.id, "email", now=NOW)
            self.assertEqual(decision.reason, "dnc:domain")
            box = mailbox(CLIENT, "info@b.example")
            db.add(box)
            await db.flush()
            touch = LgTouch(mailbox_id=box.id, address=contact.value_norm)
            company = await db.get(LgCompany, company_id)
            self.assertIn("стоп-листе", await inbox.reply_blocker(db, touch, company))

        self.run_db(run)

    def test_expired_shared_entry_does_not_block_other_clients(self):
        company_id = self.company(CLIENT, domain="target.example")

        async def run(db):
            await service.record_dnc(db, HOME, "email", "info@target.example", "unsubscribed",
                                     until=NOW - timedelta(minutes=1))
            contact = await db.scalar(select(LgContact).where(LgContact.company_id == company_id))
            decision = await service.check_can_contact(db, company_id, contact.id, "email", now=NOW)
            self.assertTrue(decision.ok)

        self.run_db(run)

    def test_unmatched_bounce_does_not_create_a_shared_block(self):
        async def run(db):
            box = mailbox(HOME, "info@a.example")
            db.add(box)
            await db.flush()
            mail = sequences.IncomingMail(
                uid=1, sender="mailer-daemon@mx.example", subject="Undelivered Mail",
                body="Final-Recipient: rfc822; info@target.example\nStatus: 5.1.1",
            )
            self.assertEqual(await sequences.handle_inbound(db, box, mail, now=NOW), "unmatched")
            self.assertEqual(await service._active_dnc(db, CLIENT, NOW), [])

        self.run_db(run)
