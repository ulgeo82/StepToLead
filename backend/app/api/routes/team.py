"""Organization-scoped client team. Never exposes StepToLead administrators."""
import secrets
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.access import check_origin, hash_password, require_portal_user
from app.core.permissions import CAPABILITIES, ROLE_DEFAULTS, SECTION_CAPABILITIES, effective_permissions, require_permission
from app.db import get_db
from app.models.marketing import PortalProjectAccess, PortalSession, PortalUser, Project

router = APIRouter(prefix="/team", tags=["client-team"])
ROLES = {"client_owner": "Владелец", "client_marketer": "Маркетолог", "sales_head": "Руководитель продаж",
         "sales_manager": "Менеджер продаж", "viewer": "Только просмотр"}


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=160)
    email: str = Field(min_length=3, max_length=254)
    phone: str | None = Field(default=None, max_length=64)
    role: str
    permissions: list[str] | None = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, value):
        if value not in ROLES:
            raise ValueError("Неизвестная клиентская роль")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Некорректный email")
        return value


class MemberPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=2, max_length=160)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    phone: str | None = Field(default=None, max_length=64)
    role: str | None = None
    permissions: list[str] | None = None
    status: str | None = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, value):
        if value is not None and value not in ROLES:
            raise ValueError("Неизвестная клиентская роль")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        return Invite.valid_email(value) if value is not None else None

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in {"active", "blocked"}:
            raise ValueError("Статус приглашения активируется после входа и смены пароля")
        return value


def member_payload(member):
    return {"id": member.id, "display_name": member.display_name, "email": member.username,
            "phone": member.phone, "role": member.role, "role_name": ROLES.get(member.role, member.role),
            "permissions": sorted(effective_permissions(member)),
            "status": "blocked" if not member.active else "invited" if member.must_change_password else "active",
            "created_at": member.created_at, "last_activity_at": member.last_activity_at}


def validate_delegation(actor, role, permissions):
    if role == "client_owner" and actor.role != "client_owner":
        raise HTTPException(403, "Назначить владельца может только владелец")
    if permissions is None:
        permissions = set(ROLE_DEFAULTS[role])
    else:
        permissions = set(permissions)
    if not permissions <= CAPABILITIES:
        raise HTTPException(422, "Неизвестное разрешение")
    if role == "client_owner":
        permissions = set(CAPABILITIES)
    for view, manage in SECTION_CAPABILITIES.values():
        if manage and manage in permissions:
            permissions.add(view)
    if not permissions <= effective_permissions(actor):
        raise HTTPException(403, "Нельзя делегировать права, которых нет у вас")
    return sorted(permissions)


async def member_or_404(db, actor, member_id):
    member = await db.get(PortalUser, member_id)
    if not member or member.workspace_id != actor.workspace_id:
        raise HTTPException(404, "Сотрудник организации не найден")
    return member


async def ensure_owner_survives(db, member, next_role, next_active):
    if member.role == "client_owner" and member.active and (next_role != "client_owner" or not next_active):
        others = await db.scalar(select(func.count()).select_from(PortalUser).where(
            PortalUser.workspace_id == member.workspace_id, PortalUser.role == "client_owner",
            PortalUser.active.is_(True), PortalUser.id != member.id))
        if not others:
            raise HTTPException(409, "Нельзя заблокировать или разжаловать последнего владельца")


@router.get("")
async def list_team(db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    require_permission(actor, "manage_team")
    rows = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == actor.workspace_id)
                             .order_by(PortalUser.display_name, PortalUser.id))).all()
    return {"members": [member_payload(row) for row in rows], "viewer": member_payload(actor),
            "roles": ROLES, "sections": SECTION_CAPABILITIES,
            "defaults": {role: sorted(capabilities) for role, capabilities in ROLE_DEFAULTS.items()}}


@router.get("/{member_id}")
async def get_member(member_id: int, db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    require_permission(actor, "manage_team")
    return member_payload(await member_or_404(db, actor, member_id))


@router.post("", status_code=201)
async def invite_member(payload: Invite, request: Request, db: AsyncSession = Depends(get_db),
                        actor: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(actor, "manage_team")
    permissions = validate_delegation(actor, payload.role, payload.permissions)
    email = str(payload.email).lower()
    if await db.scalar(select(PortalUser.id).where(PortalUser.username == email)):
        raise HTTPException(409, "Этот email уже используется")
    from app.services import plans
    await plans.require_seat(db, actor.workspace_id)
    password = secrets.token_urlsafe(18)
    member = PortalUser(workspace_id=actor.workspace_id, username=email, display_name=payload.display_name.strip(),
                        phone=payload.phone, role=payload.role, permissions=permissions,
                        password_hash=await run_in_threadpool(hash_password, password), active=True,
                        must_change_password=True)
    db.add(member)
    await db.flush()
    # Keep project membership separate from the organization-level team UI.
    project = await db.scalar(select(Project).where(Project.workspace_id == actor.workspace_id)
                              .order_by(Project.is_default.desc(), Project.id).limit(1))
    if project:
        db.add(PortalProjectAccess(user_id=member.id, project_id=project.id))
    await db.commit()
    await db.refresh(member)
    return {**member_payload(member), "temporary_password": password,
            "delivery": "manual", "message": "Email не отправлен. Передайте временный пароль сотруднику безопасным каналом."}


@router.patch("/{member_id}")
async def edit_member(member_id: int, payload: MemberPatch, request: Request,
                      db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(actor, "manage_team")
    member = await member_or_404(db, actor, member_id)
    if member.role == "client_owner" and actor.role != "client_owner":
        raise HTTPException(403, "Изменить владельца может только владелец")
    role = payload.role or member.role
    if payload.role is not None or payload.permissions is not None:
        requested = payload.permissions if payload.permissions is not None else list(ROLE_DEFAULTS[role])
        delegated = validate_delegation(actor, role, requested)
    else:
        delegated = None
    next_active = payload.status != "blocked" if payload.status is not None else member.active
    await ensure_owner_survives(db, member, role, next_active)
    if delegated is not None:
        member.permissions = delegated
        member.role = role
    if payload.display_name is not None:
        member.display_name = payload.display_name.strip()
    if payload.email is not None:
        email = str(payload.email).lower()
        if email != member.username and await db.scalar(select(PortalUser.id).where(PortalUser.username == email)):
            raise HTTPException(409, "Этот email уже используется")
        member.username = email
    if "phone" in payload.model_fields_set:
        member.phone = payload.phone
    if payload.status is not None:
        member.active = next_active
        if not next_active:
            await db.execute(delete(PortalSession).where(PortalSession.user_id == member.id))
    await db.commit()
    return member_payload(member)


@router.delete("/{member_id}", status_code=204)
async def remove_member(member_id: int, request: Request, db: AsyncSession = Depends(get_db),
                        actor: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(actor, "manage_team")
    member = await member_or_404(db, actor, member_id)
    if member.role == "client_owner" and actor.role != "client_owner":
        raise HTTPException(403, "Изменить владельца может только владелец")
    await ensure_owner_survives(db, member, member.role, False)
    member.active = False
    await db.execute(delete(PortalSession).where(PortalSession.user_id == member.id))
    await db.commit()
