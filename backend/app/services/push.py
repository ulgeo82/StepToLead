"""Web push to installed portal apps (PWA) and browsers: a new lead reaches the manager's phone."""
import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.system import AppSetting, PushSubscription

logger = logging.getLogger("uvicorn.error.push")
PENDING_KEY = "pending_push"
_background: set[asyncio.Task] = set()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_from_private(private_der: str) -> str:
    key = serialization.load_der_private_key(unb64(private_der), password=None)
    return b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


async def vapid_keys(db: AsyncSession) -> tuple[str, str]:
    """(private DER base64url, public uncompressed point base64url). Env wins; otherwise generated once and kept in the DB."""
    if settings.vapid_private_key:
        return settings.vapid_private_key, settings.vapid_public_key or _public_from_private(settings.vapid_private_key)
    row = await db.get(AppSetting, "vapid")
    if row is None:
        key = ec.generate_private_key(ec.SECP256R1())
        private = b64(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        row = AppSetting(key="vapid", value={"private": private, "public": _public_from_private(private)})
        db.add(row); await db.commit()
    return row.value["private"], row.value["public"]


def queue(db: AsyncSession, user_id: int, title: str, body: str, url: str = "/crm") -> None:
    """Remember a push for after the commit (same pattern as Telegram notifications)."""
    db.sync_session.info.setdefault(PENDING_KEY, []).append((user_id, title[:120], body[:240], url))


def _send(subscription: dict, payload: str, private_der: str) -> int:
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    vapid = Vapid(private_key=serialization.load_der_private_key(unb64(private_der), password=None))
    try:
        webpush(subscription, data=payload, vapid_private_key=vapid, vapid_claims={"sub": settings.vapid_subject}, ttl=3600, timeout=10)
        return 200
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 500


async def _deliver(items: list[tuple[int, str, str, str]]) -> None:
    from app.db import SessionLocal
    try:
        async with SessionLocal() as db:
            private, _ = await vapid_keys(db)
            for user_id, title, body, url in items:
                rows = (await db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id))).all()
                for row in rows:
                    status = await asyncio.to_thread(_send, {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}},
                                                     json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False), private)
                    if status in {404, 410}:
                        await db.delete(row)  # the device unsubscribed or the app was removed
            await db.commit()
    except Exception:
        logger.exception("push delivery failed")


def flush(db: AsyncSession) -> None:
    items = db.sync_session.info.pop(PENDING_KEY, None)
    if not items:
        return
    try:
        task = asyncio.get_running_loop().create_task(_deliver(items))
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def discard(db: AsyncSession) -> None:
    db.sync_session.info.pop(PENDING_KEY, None)
