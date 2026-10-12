"""CRM mail adapter. Lead-generation transport remains unchanged."""
import asyncio
import email
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import make_msgid, parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from types import SimpleNamespace

from app.core.crypto import decrypt_secret
from app.domains.leadgen.sequences import (
    IncomingMail, OutgoingMail, SmtpImapTransport, build_message, imaplib, smtplib, ssl,
)

PRESETS = {
    "yandex": {"imap_host": "imap.yandex.ru", "smtp_host": "smtp.yandex.ru"},
    "mailru": {"imap_host": "imap.mail.ru", "smtp_host": "smtp.mail.ru"},
    "gmail": {"imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"},
}


def valid_email(value):
    return bool(re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value or ""))


class PlainHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li"}:
            self.parts.append("\n")
        if tag == "blockquote":
            self.parts.append("\n> ")
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "blockquote"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def strip_quotes(text):
    lines = []
    for line in (text or "").splitlines():
        if re.match(r"\s*(>|On .+ wrote:|.+ пишет:|From:|От:|[-_]{3,})", line, re.I):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_mail(raw, uid):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body and body.get_content_type() == "text/html":
        parser = PlainHtml()
        parser.feed(text)
        text = "".join(parser.parts)
    text = strip_quotes(text)
    names = [part.get_filename() for part in message.walk() if part.get_filename()]
    if names:
        text += "\nВложения: " + ", ".join(names)
    return IncomingMail(uid=uid, sender=parseaddr(str(message.get("From", "")))[1].lower(),
        subject=str(message.get("Subject", "")), body=text[:20000], message_id=message.get("Message-ID"),
        in_reply_to=message.get("In-Reply-To"), references=str(message.get("References", "")).split(),
        headers={key.lower(): str(value) for key, value in message.items() if key.lower() in {
            "auto-submitted", "list-unsubscribe", "precedence", "x-autoreply", "date", "to"}})


def ignored(mail):
    headers = {k.lower(): v for k, v in mail.headers.items()}
    sender = mail.sender.split("@")[0].lower()
    return (not valid_email(mail.sender) or bool(re.search(r"no[-_.]?reply|mailer-daemon", sender))
            or headers.get("auto-submitted", "no").lower() != "no" or "list-unsubscribe" in headers
            or headers.get("precedence", "").lower() in {"bulk", "list", "junk"} or "x-autoreply" in headers)


def sent_at(mail):
    try:
        value = parsedate_to_datetime(mail.headers.get("date", ""))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except (ValueError, TypeError, IndexError):
        return datetime.now(timezone.utc)


class EmailTransport(SmtpImapTransport):
    """Reuse transport login checks and mail builders; add CRM MIME/thread support only."""
    async def check(self, mailbox, password):
        await super().check(mailbox, password)
        def cursor():
            with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=self.timeout) as box:
                box.login(mailbox.login, password)
                box.select("INBOX", readonly=True)
                value = box.response("UIDNEXT")[1]
                return max(0, int(value[0]) - 1) if value and value[0] else 0
        return await asyncio.to_thread(cursor)

    async def fetch(self, mailbox, password, after_uid):
        def go():
            with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=self.timeout) as box:
                box.login(mailbox.login, password)
                box.select("INBOX", readonly=True)
                status, data = box.uid("search", None, f"UID {int(after_uid or 0) + 1}:*")
                if status != "OK":
                    raise ValueError("Не удалось получить новые письма")
                result = []
                for raw_uid in (data[0] or b"").split():
                    if int(raw_uid) <= int(after_uid or 0):
                        continue
                    status, parts = box.uid("fetch", raw_uid, "(BODY.PEEK[])")
                    if status != "OK":
                        raise ValueError("Не удалось прочитать письмо")
                    raw = next((part[1] for part in parts if isinstance(part, tuple)), b"")
                    result.append(parse_mail(raw, int(raw_uid)))
                    if len(result) == 200:
                        break
                return result
        return await asyncio.to_thread(go)

    async def history(self, mailbox, password, address, limit):
        def go():
            with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=self.timeout) as box:
                box.login(mailbox.login, password)
                _, folders = box.list()
                sent = []
                for line in folders or []:
                    if line and b"\\Sent" in line:
                        match = re.search(rb'"([^"\\]*(?:\\.[^"\\]*)*)"\s*$', line)
                        if match:
                            sent.append(b'"' + match.group(1) + b'"')
                result = []
                for folder in ["INBOX", *sent, "Sent", "Sent Messages"]:
                    status, _ = box.select(folder, readonly=True)
                    if status != "OK":
                        continue
                    status, data = box.uid("search", None, "OR", "FROM", f'"{address}"', "TO", f'"{address}"')
                    if status != "OK":
                        continue
                    for uid in (data[0] or b"").split()[-min(limit, 50):]:
                        status, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
                        if status != "OK":
                            continue
                        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                        mail = parse_mail(raw, int(uid))
                        if ignored(mail) or not mail.message_id:
                            continue
                        result.append(mail)
                return result
        return await asyncio.to_thread(go)

    async def send(self, mailbox, password, mail):
        message = build_message(mail)
        del message["List-Unsubscribe"]  # CRM reply is not an outreach mailing.
        if message.get("References"):
            del message["References"]
        if getattr(mail, "references", None):
            message["References"] = " ".join(mail.references[-20:])
        def go():
            context = ssl.create_default_context()
            if mailbox.smtp_port == 465:
                server = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port,
                                         timeout=self.timeout, context=context)
            else:
                server = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=self.timeout)
            with server:
                if mailbox.smtp_port != 465:
                    server.starttls(context=context)
                server.login(mailbox.login, password)
                if server.send_message(message):
                    raise ValueError("Почтовый сервер отказал в доставке")
        await asyncio.to_thread(go)


class EmailChannel:
    def __init__(self, channel):
        self.channel = channel
        self.transport = EmailTransport()
        self.mailbox = SimpleNamespace(**(channel.config or {}))
        self.password = decrypt_secret(channel.secret_encrypted)

    async def test(self):
        from app.services.messaging import ChannelError
        try:
            cursor = await self.transport.check(self.mailbox, self.password)
            return {"name": self.mailbox.email, "imap_uid": cursor or 0}
        except (ConnectionError, PermissionError) as exc:
            raise ChannelError(str(exc)) from None
        except Exception as exc:
            raise ChannelError(f"Не удалось войти в IMAP/SMTP ({type(exc).__name__}: {str(exc)[:160]}). "
                               "Проверьте серверы и пароль приложения.") from None

    async def poll(self):
        from app.services.messaging import ChannelError, Incoming
        try:
            mails = await self.transport.fetch(self.mailbox, self.password,
                                               (self.channel.config or {}).get("imap_uid", 0))
        except Exception:
            raise ChannelError("Почтовый сервер недоступен. Проверьте подключение IMAP.") from None
        result = []
        cursor = int((self.channel.config or {}).get("imap_uid", 0))
        for mail in sorted(mails, key=lambda m: m.uid):
            cursor = max(cursor, mail.uid)
            if ignored(mail):
                continue
            result.append(Incoming(chat_id=mail.sender.lower(), title=mail.sender,
                text=mail.body or "Письмо без текста", external_id=mail.message_id or f"uid:{mail.uid}",
                direction="in", sent_at=sent_at(mail), phone=None,
                meta={"email": mail.sender, "subject": mail.subject, "message_id": mail.message_id,
                      "references": mail.references}))
        self.channel.config = {**(self.channel.config or {}), "imap_uid": cursor}
        return result

    async def send(self, conversation, text):
        from app.services.messaging import ChannelError
        meta = conversation.meta or {}
        subject = meta.get("subject") or "Сообщение"
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        message_id = make_msgid()
        body = text + ("\n\n" + self.mailbox.signature if self.mailbox.signature else "")
        mail = OutgoingMail(sender=self.mailbox.email, sender_name=self.channel.name,
            to=conversation.external_chat_id, subject=subject, body=body,
            message_id=message_id, in_reply_to=meta.get("message_id"))
        mail.references = list(dict.fromkeys([*(meta.get("references") or []),
                                             *([meta["message_id"]] if meta.get("message_id") else [])]))
        try:
            await self.transport.send(self.mailbox, self.password, mail)
        except Exception:
            raise ChannelError("Не удалось отправить письмо. Проверьте SMTP и пароль приложения.") from None
        conversation.meta = {**meta, "subject": subject, "message_id": message_id,
                             "references": mail.references}
        return message_id

    async def lookup(self, *, phone=None, username=None, email=None):
        if not valid_email(email):
            return None
        address = email.lower()
        history = await self.history(address, limit=1)
        return {"external_chat_id": address, "title": address, "has_history": bool(history)}

    async def history(self, external_chat_id, limit=50):
        from app.services.messaging import ChannelError
        try:
            mails = await self.transport.history(self.mailbox, self.password, external_chat_id, limit)
        except Exception:
            raise ChannelError("Не удалось загрузить историю почты") from None
        unique = {mail.message_id: mail for mail in mails if mail.message_id}
        return [{"external_id": mail.message_id, "text": mail.body, "sent_at": sent_at(mail),
                 "direction": "out" if mail.sender == self.mailbox.email else "in",
                 "meta": {"subject": mail.subject, "message_id": mail.message_id,
                          "references": mail.references}}
                for mail in sorted(unique.values(), key=sent_at)[-min(limit, 50):]]
