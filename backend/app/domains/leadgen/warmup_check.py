"""Проверка прогрева на настоящих ящиках: python -m app.domains.leadgen.warmup_check

Для каждого активного ящика: удаётся ли вход по IMAP, какие папки «Спам» видны, сколько прогревочных писем
ждут проверки и сколько из них нашлось во «Входящих» / в «Спаме». Пароли не выводятся. Статусы в базе не меняет
(найденные письма в ящике отмечаются прочитанными — как при обычной работе прогрева).
"""
import asyncio

from sqlalchemy import select

from app.core.crypto import decrypt_secret
from app.db import SessionLocal
from app.domains.leadgen import service, warmup
from app.domains.leadgen.models import LgMailbox
from app.domains.leadgen.routes import unicode_email
from app.domains.leadgen.sequences import SmtpImapTransport, _spam_folders


async def main() -> None:
    transport = SmtpImapTransport()
    now = service.utcnow()
    async with SessionLocal() as db:
        boxes = (await db.scalars(select(LgMailbox).where(LgMailbox.is_active.is_(True)).order_by(LgMailbox.id))).all()
        if not boxes:
            print("Активных ящиков нет")
        for mb in boxes:
            name = unicode_email(mb.email)
            rows = await warmup.pending_for(db, mb, now)
            ids = [r.message_id for r in rows if r.message_id]
            password = decrypt_secret(mb.password_encrypted)
            try:
                folders = await transport._session(mb, password, _spam_folders)
                found = await transport.warm_locate(mb, password, ids) if ids else {}
            except Exception as exc:  # noqa: BLE001
                print(f"✕ {name}: вход по IMAP не удался — {type(exc).__name__}: {exc}")
                continue
            inbox = sum(1 for v in found.values() if v["folder"] == "inbox")
            spam = sum(1 for v in found.values() if v["folder"] == "spam")
            print(f"✓ {name}: вход OK · папка «Спам»: {', '.join(folders) or 'не найдена'} · "
                  f"ждут проверки {len(ids)} · найдено во «Входящих» {inbox}, в «Спаме» {spam}, "
                  f"не найдено {len(ids) - inbox - spam}")
        await db.rollback()


if __name__ == "__main__":
    asyncio.run(main())
