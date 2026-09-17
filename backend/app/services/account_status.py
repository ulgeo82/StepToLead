import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon.errors import FloodWaitError

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import SessionLocal
from app.models.telegram_account import TelegramAccount
from app.schemas.telegram_account import TelegramAccountRead
from app.services.telegram import make_client


class StatusHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, account: TelegramAccount) -> None:
        payload = {"type": "account_status", "account": TelegramAccountRead.model_validate(account).model_dump(mode="json")}
        stale: list[WebSocket] = []
        for websocket in self.connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)


status_hub = StatusHub()


async def check_one(account: TelegramAccount) -> None:
    session_value = decrypt_secret(account.session_encrypted)
    if not session_value:
        return
    client = make_client(session_value, account.proxy)
    account.status = "checking"
    await status_hub.broadcast(account)
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        if not await client.is_user_authorized():
            account.status = "disconnected"
            account.last_error = "Сессия Telegram больше не авторизована"
            return
        me = await client.get_me()
        account.status = "online" if me else "offline"
        account.last_seen_at = datetime.now(timezone.utc)
        account.last_error = None
        account.session_encrypted = encrypt_secret(client.session.save())
    except FloodWaitError as exc:
        account.status = "flood_wait"
        account.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
        account.last_error = f"Ожидание Telegram: {exc.seconds} сек."
    except TimeoutError:
        account.status = "error"
        account.last_error = "Telegram не ответил за 20 секунд. Проверьте назначенный прокси."
    except Exception as exc:
        account.status = "error"
        account.last_error = str(exc)[:500]
    finally:
        await client.disconnect()


async def monitor_accounts() -> None:
    while True:
        try:
            if settings.encryption_secret != "change-this-before-production":
                async with SessionLocal() as db:
                    query = select(TelegramAccount).options(selectinload(TelegramAccount.proxy)).where(TelegramAccount.session_encrypted.is_not(None))
                    accounts = list((await db.scalars(query)).all())
                    for account in accounts:
                        await check_one(account)
                        await db.commit()
                        await status_hub.broadcast(account)
            await asyncio.sleep(max(settings.account_status_interval, 10))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(10)
