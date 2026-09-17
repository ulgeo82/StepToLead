"""Серверные сессии: в cookie только случайный токен, в БД — его хеш."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_db
from app.models.access import AdminSession, AdminUser
from app.models.marketing import PortalSession, PortalUser

COOKIE = "stl_session"
PORTAL_COOKIE = "stl_portal_session"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${key}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        return hmac.compare_digest(hash_password(password, encoded.split("$")[1]), encoded)
    except (ValueError, IndexError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def allowed_origins() -> set[str]:
    origins = {settings.frontend_origin.rstrip("/")}
    if not settings.session_cookie_secure:
        origins.update({"http://localhost:3000", "http://127.0.0.1:3000"})
    return origins


def check_origin(connection: HTTPConnection):
    """Не полагаемся лишь на CORS: отклоняем cross-site запись и WS."""
    if connection.scope["type"] == "websocket" or connection.scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
        if connection.headers.get("origin") not in allowed_origins():
            if connection.scope["type"] == "websocket":
                raise WebSocketException(code=1008)
            raise HTTPException(403, "Недопустимый источник запроса")


async def session_user(connection: HTTPConnection, db: AsyncSession) -> AdminUser | None:
    token = connection.cookies.get(COOKIE, "")
    if len(token) < 40 or len(token) > 128:
        return None
    session = await db.get(AdminSession, token_digest(token))
    if not session:
        return None
    expiry = session.expires_at.replace(tzinfo=timezone.utc) if session.expires_at.tzinfo is None else session.expires_at
    if expiry <= datetime.now(timezone.utc):
        return None
    return await db.get(AdminUser, session.user_id)


async def require_admin(connection: HTTPConnection, db: AsyncSession = Depends(get_db)) -> AdminUser:
    check_origin(connection)
    user = await session_user(connection, db)
    if user is None or user.role != "admin":
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=1008)
        raise HTTPException(401 if user is None else 403, "Требуется вход администратора")
    if connection.scope["type"] == "websocket":
        # Handshake не должен держать соединение БД всё время жизни WebSocket.
        await db.close()
    return user


async def portal_session_user(connection: HTTPConnection, db: AsyncSession) -> PortalUser | None:
    token = connection.cookies.get(PORTAL_COOKIE, "")
    if len(token) < 40 or len(token) > 128:
        return None
    session = await db.get(PortalSession, token_digest(token))
    if not session:
        return None
    expiry = session.expires_at.replace(tzinfo=timezone.utc) if session.expires_at.tzinfo is None else session.expires_at
    if expiry <= datetime.now(timezone.utc):
        return None
    user = await db.get(PortalUser, session.user_id)
    return user if user and user.active else None


async def require_portal_user(connection: HTTPConnection, db: AsyncSession = Depends(get_db)) -> PortalUser:
    check_origin(connection)
    user = await portal_session_user(connection, db)
    if user is None:
        raise HTTPException(401, "Требуется вход в клиентский кабинет")
    return user
