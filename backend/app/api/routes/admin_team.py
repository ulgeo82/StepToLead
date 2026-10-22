"""Команда агентства (Система → Команда): полный доступ к админке, как у владельца.

Пароль генерирует портал и показывает один раз — ни в базе, ни в логах его нет (только хеш).
Отключённый админ получает role="disabled": вход и все сессии закрываются сразу.
Нельзя отключить себя и последнего активного админа — иначе в портал никто не войдёт.
"""
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.access import COOKIE, hash_password, require_admin, token_digest, verify_password
from app.db import get_db
from app.models.access import AdminAudit, AdminSession, AdminUser

router = APIRouter(prefix="/admin/team", tags=["admin-team"])
LOGIN = re.compile(r"^[a-z0-9][a-z0-9._@+-]{2,253}$")
ACTIONS = {"created": "добавил", "disabled": "отключил", "enabled": "вернул доступ", "password_reset": "сбросил пароль",
           "sessions_revoked": "завершил все сеансы", "renamed": "переименовал", "password_changed": "сменил свой пароль"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _password() -> str:
    # 16 символов без похожих (0/O, l/1) — удобно продиктовать или переслать.
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


class MemberIn(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    display_name: str | None = Field(default=None, max_length=120)


class MemberPatch(BaseModel):
    display_name: str = Field(max_length=120)


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=12, max_length=256)


async def _log(db: AsyncSession, actor: AdminUser, action: str, target: AdminUser | None = None) -> None:
    db.add(AdminAudit(actor_id=actor.id, actor=actor.display_name or actor.username, action=action,
                      target=(target.display_name or target.username) if target else None, created_at=_now()))


async def _sessions(db: AsyncSession) -> dict[int, int]:
    rows = (await db.execute(select(AdminSession.user_id, func.count()).where(AdminSession.expires_at > _now())
                             .group_by(AdminSession.user_id))).all()
    return {uid: int(n) for uid, n in rows}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _member(user: AdminUser, sessions: dict[int, int], names: dict[int, str], me: AdminUser) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "active": user.role == "admin",
            "is_me": user.id == me.id, "telegram_linked": bool(user.telegram_chat_id), "telegram_username": user.telegram_username,
            "created_at": _iso(user.created_at), "created_by": names.get(user.created_by_id or 0),
            "last_login_at": _iso(user.last_login_at), "disabled_at": _iso(user.disabled_at),
            "sessions": sessions.get(user.id, 0)}


async def _target(db: AsyncSession, user_id: int) -> AdminUser:
    user = await db.get(AdminUser, user_id)
    if user is None or user.role not in ("admin", "disabled"):
        raise HTTPException(404, "Сотрудник не найден")
    return user


async def _active_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AdminUser).where(AdminUser.role == "admin")) or 0)


async def _payload(db: AsyncSession, me: AdminUser) -> dict:
    users = (await db.scalars(select(AdminUser).where(AdminUser.role.in_(("admin", "disabled"))).order_by(AdminUser.id))).all()
    names = {u.id: u.display_name or u.username for u in users}
    sessions = await _sessions(db)
    log = (await db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(50))).all()
    members = sorted((_member(u, sessions, names, me) for u in users), key=lambda m: (not m["active"], m["id"]))
    return {"members": members,
            "log": [{"id": r.id, "actor": r.actor, "action": r.action, "action_name": ACTIONS.get(r.action, r.action),
                     "target": r.target, "created_at": _iso(r.created_at)} for r in log]}


@router.get("")
async def team(db: AsyncSession = Depends(get_db), me: AdminUser = Depends(require_admin)):
    return await _payload(db, me)


@router.post("", status_code=201)
async def add(payload: MemberIn, db: AsyncSession = Depends(get_db), me: AdminUser = Depends(require_admin)):
    username = payload.username.strip().lower()
    if not LOGIN.match(username):
        raise HTTPException(422, "Логин: латиница, цифры и . _ @ + -, от 3 символов. Удобно использовать рабочую почту.")
    if await db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
        raise HTTPException(409, "Такой логин уже есть")
    password = _password()
    user = AdminUser(username=username, display_name=(payload.display_name or "").strip() or None, role="admin",
                     password_hash=await run_in_threadpool(hash_password, password), created_at=_now(), created_by_id=me.id)
    db.add(user)
    await db.flush()
    await _log(db, me, "created", user)
    await db.commit()
    return {"password": password, **await _payload(db, me)}


@router.patch("/{user_id}")
async def rename(user_id: int, payload: MemberPatch, db: AsyncSession = Depends(get_db),
                 me: AdminUser = Depends(require_admin)):
    user = await _target(db, user_id)
    user.display_name = payload.display_name.strip() or None
    await _log(db, me, "renamed", user)
    await db.commit()
    return await _payload(db, me)


@router.post("/{user_id}/disable")
async def disable(user_id: int, db: AsyncSession = Depends(get_db), me: AdminUser = Depends(require_admin)):
    user = await _target(db, user_id)
    if user.id == me.id:
        raise HTTPException(409, "Себя отключить нельзя — попросите другого админа")
    if user.role == "admin" and await _active_count(db) <= 1:
        raise HTTPException(409, "Это последний активный админ")
    user.role, user.disabled_at = "disabled", _now()
    await db.execute(delete(AdminSession).where(AdminSession.user_id == user.id))
    await _log(db, me, "disabled", user)
    await db.commit()
    return await _payload(db, me)


@router.post("/{user_id}/enable")
async def enable(user_id: int, db: AsyncSession = Depends(get_db), me: AdminUser = Depends(require_admin)):
    user = await _target(db, user_id)
    user.role, user.disabled_at = "admin", None
    await _log(db, me, "enabled", user)
    await db.commit()
    return await _payload(db, me)


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: int, db: AsyncSession = Depends(get_db), me: AdminUser = Depends(require_admin)):
    user = await _target(db, user_id)
    if user.id == me.id:
        raise HTTPException(409, "Свой пароль меняйте в блоке «Мой пароль» — нужен текущий пароль")
    password = _password()
    user.password_hash = await run_in_threadpool(hash_password, password)
    await db.execute(delete(AdminSession).where(AdminSession.user_id == user.id))
    await _log(db, me, "password_reset", user)
    await db.commit()
    return {"password": password, **await _payload(db, me)}


@router.post("/{user_id}/logout-all")
async def logout_all(user_id: int, request: Request, db: AsyncSession = Depends(get_db),
                     me: AdminUser = Depends(require_admin)):
    user = await _target(db, user_id)
    query = delete(AdminSession).where(AdminSession.user_id == user.id)
    if user.id == me.id:  # свои — все, кроме текущего
        query = query.where(AdminSession.token_hash != token_digest(request.cookies.get(COOKIE, "")))
    await db.execute(query)
    await _log(db, me, "sessions_revoked", user)
    await db.commit()
    return await _payload(db, me)


@router.post("/me/password")
async def change_password(payload: PasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                          me: AdminUser = Depends(require_admin)):
    if not await run_in_threadpool(verify_password, payload.current, me.password_hash):
        raise HTTPException(400, "Текущий пароль неверный")
    if payload.new == payload.current:
        raise HTTPException(400, "Новый пароль совпадает с текущим")
    me.password_hash = await run_in_threadpool(hash_password, payload.new)
    await db.execute(delete(AdminSession).where(AdminSession.user_id == me.id,
                                                AdminSession.token_hash != token_digest(request.cookies.get(COOKIE, ""))))
    await _log(db, me, "password_changed")
    await db.commit()
    return {"ok": True}
