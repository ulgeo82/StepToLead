from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import test_messaging
from app.core.crypto import encrypt_secret
from app.domains.leadgen.sequences import IncomingMail
from app.models.crm import CrmInbound
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import email_channel, messaging


class EmailTests(test_messaging.MessagingTests):
    def connect(self):
        async def run(db):
            channel = MessagingChannel(workspace_id=1, project_id=1, name="Отдел продаж", kind="email",
                config={"email": "shop@example.org", "login": "shop@example.org", "signature": "Анна",
                        "imap_host": "imap.example.org", "smtp_host": "smtp.example.org",
                        "imap_port": 993, "smtp_port": 465, "imap_uid": 0},
                secret_encrypted=encrypt_secret("app-password"))
            db.add(channel)
            await db.commit()
            return channel.id
        return self.read(run)

    def test_receive_deduplicate_bulk_and_thread_reply(self):
        cid = self.connect()
        mail = IncomingMail(uid=1, sender="anna@example.org", subject="Кухня", body="Хочу кухню",
                            message_id="<one@example.org>", references=["<old@example.org>"])
        bulk = IncomingMail(uid=2, sender="news@example.org", subject="Новости", body="Реклама",
                            message_id="<bulk@example.org>", headers={"List-Unsubscribe": "url"})
        transport = SimpleNamespace(fetch=AsyncMock(return_value=[mail, bulk]), send=AsyncMock())
        async def run(db):
            channel = await db.get(MessagingChannel, cid)
            with patch.object(email_channel, "EmailTransport", return_value=transport):
                self.assertEqual(await messaging.poll_channel(db, channel), 1)
                self.assertEqual(await messaging.poll_channel(db, channel), 0)
                conversation = await db.scalar(select(Conversation))
                self.assertEqual(conversation.external_chat_id, "anna@example.org")
                self.assertEqual(channel.config["imap_uid"], 2)
                await messaging.send(db, conversation, "Здравствуйте", None)
            sent = transport.send.call_args.args[2]
            self.assertEqual(sent.in_reply_to, "<one@example.org>")
            self.assertEqual(sent.references, ["<old@example.org>", "<one@example.org>"])
            self.assertEqual(sent.subject, "Re: Кухня")
            self.assertTrue(sent.body.endswith("Анна"))
            inbound = await db.scalar(select(CrmInbound))
            self.assertEqual(inbound.email, "anna@example.org")
            self.assertEqual(len(list(await db.scalars(select(Message)))), 2)
        self.read(run)

    def test_mime_html_quotes_and_attachment_names(self):
        message = EmailMessage()
        message["From"] = "Анна <anna@example.org>"
        message["To"] = "shop@example.org"
        message["Message-ID"] = "<mime@example.org>"
        message.set_content("<p>Нужна кухня</p><blockquote>Старое письмо</blockquote>", subtype="html")
        message.add_attachment(b"data", maintype="application", subtype="pdf", filename="plan.pdf")
        parsed = email_channel.parse_mail(message.as_bytes(), 1)
        self.assertIn("Нужна кухня", parsed.body)
        self.assertNotIn("Старое письмо", parsed.body)
        self.assertIn("plan.pdf", parsed.body)
        self.assertNotIn("<p>", parsed.body)

    def test_ignore_auto_noreply_and_bulk(self):
        for sender, headers in [("noreply@example.org", {}), ("a@example.org", {"Auto-Submitted": "auto-replied"}),
                                ("a@example.org", {"Precedence": "bulk"})]:
            self.assertTrue(email_channel.ignored(IncomingMail(1, sender, "", "", headers=headers)))

    def test_connect_checks_both_servers_and_encrypts_password(self):
        with patch.object(email_channel.EmailTransport, "check", AsyncMock(return_value=99)):
            result = self.client.post("/api/crm/projects/1/email-channel", headers=test_messaging.ORIGIN,
                json={"name": "Почта", "email": "shop@example.org", "login": "shop@example.org",
                      "password": "app-password", "imap_host": "imap.example.org", "smtp_host": "smtp.example.org"})
        self.assertEqual(result.status_code, 201, result.text)
        channel = self.read(lambda db: db.get(MessagingChannel, result.json()["id"]))
        self.assertNotIn("app-password", channel.secret_encrypted)
        self.assertEqual(channel.config["imap_uid"], 99)

    def test_lookup_valid_address_and_history(self):
        cid = self.connect()
        async def run(db):
            channel = await db.get(MessagingChannel, cid)
            transport = SimpleNamespace(history=AsyncMock(return_value=[]))
            with patch.object(email_channel, "EmailTransport", return_value=transport):
                adapter = email_channel.EmailChannel(channel)
                self.assertTrue((await adapter.lookup(email="Anna@example.org"))["external_chat_id"] == "anna@example.org")
                self.assertIsNone(await adapter.lookup(email="invalid"))
        self.read(run)
