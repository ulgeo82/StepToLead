import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.access import COOKIE, check_origin, hash_password, require_admin, token_digest, verify_password
from app.core.config import settings
from app.db import get_db
from app.models.access import AdminSession, AdminUser

router = APIRouter(prefix="/auth", tags=["access"])
_dummy_hash = hash_password(secrets.token_urlsafe(32))


def client_address(request: Request) -> str:
    """The visitor's IP. Behind Caddy → Next the socket address is the frontend container for everyone, so the
    real one comes from X-Real-IP, which Caddy sets from the TCP connection (deploy/Caddyfile, header_up) and the
    visitor cannot forge. Trusted only when the request itself comes from the private network (our proxies)."""
    import ipaddress
    peer = request.client.host if request.client else "unknown"
    try:
        internal = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        internal = False
    real = (request.headers.get("x-real-ip") or "").strip()
    if internal and real:
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return peer


async def rate_limit(request: Request, scope: str, limit: int, seconds: int):
    address = client_address(request)
    key = f"stl:rate:{scope}:{token_digest(address)}"
    try:
        async with Redis.from_url(settings.redis_url) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, seconds, nx=True)
                count, _ = await pipe.execute()
    except RedisError:
        raise HTTPException(503, "Защита запросов временно недоступна. Повторите позже.")
    if count > limit:
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.", headers={"Retry-After": str(seconds)})


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(payload: Login, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await rate_limit(request, "login", 10, 900)
    user = await db.scalar(select(AdminUser).where(AdminUser.username == payload.username.strip().lower()))
    valid = await run_in_threadpool(verify_password, payload.password, user.password_hash if user else _dummy_hash)
    if not valid or user is None or user.role != "admin":
        raise HTTPException(401, "Неверный логин или пароль")
    now = datetime.now(timezone.utc)
    await db.execute(delete(AdminSession).where(AdminSession.expires_at < now))
    old_token = request.cookies.get(COOKIE)
    if old_token:
        await db.execute(delete(AdminSession).where(AdminSession.token_hash == token_digest(old_token)))
    token = secrets.token_urlsafe(32)
    db.add(AdminSession(token_hash=token_digest(token), user_id=user.id, expires_at=now + timedelta(hours=12)))
    await db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.session_cookie_secure, samesite="strict", max_age=43200, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"username": user.username, "role": user.role}


@router.get("/me")
async def me(response: Response, user: AdminUser = Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    return {"username": user.username, "role": user.role}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await db.execute(delete(AdminSession).where(AdminSession.token_hash == token_digest(request.cookies.get(COOKIE, ""))))
    await db.commit()
    response.delete_cookie(COOKIE, path="/", secure=settings.session_cookie_secure, httponly=True, samesite="strict")
    return {"ok": True}
