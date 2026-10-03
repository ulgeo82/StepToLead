"""Settings of the client care loop (meeting reminders, review requests, repeat sales)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import project_for
from app.api.routes.messaging import can_manage_channels
from app.core.access import check_origin, require_portal_user
from app.core.permissions import require_permission
from app.db import get_db
from app.models.marketing import PortalUser
from app.services import care

router = APIRouter(prefix="/crm", tags=["crm-care"])


class CareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminders: bool = False
    reminder_hours: int = Field(default=24, ge=2, le=72)
    reminder_text: str = Field(default=care.DEFAULTS["reminder_text"], min_length=10, max_length=600)
    review: bool = False
    review_days: int = Field(default=3, ge=1, le=60)
    review_url: str = Field(default="", max_length=400, pattern=r"^(https://\S{3,400})?$")
    review_text: str = Field(default=care.DEFAULTS["review_text"], min_length=10, max_length=600)
    repeat: bool = False
    repeat_days: int = Field(default=180, ge=7, le=1095)
    repeat_text: str = Field(default=care.DEFAULTS["repeat_text"], min_length=10, max_length=600)


async def view(db: AsyncSession, user: PortalUser, project) -> dict:
    return {"care": care.settings_for(project), "defaults": care.DEFAULTS, "stats": await care.stats(db, project.id),
            "can_manage": can_manage_channels(user)}


@router.get("/projects/{project_id}/care")
async def get_care(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    return await view(db, user, await project_for(db, user, project_id))


@router.put("/projects/{project_id}/care")
async def put_care(project_id: int, payload: CareIn, request: Request, db: AsyncSession = Depends(get_db),
                   user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настройки меняет руководитель или владелец")
    if payload.review and not payload.review_url:
        raise HTTPException(422, "Укажите ссылку, где оставить отзыв (Яндекс Карты, 2ГИС, Авито)")
    if payload.review and "{review_url}" not in payload.review_text:
        raise HTTPException(422, "В тексте просьбы об отзыве должна быть ссылка {review_url}")
    project.portal_state = {**(project.portal_state or {}), "care": {
        k: v.strip() if isinstance(v, str) else v for k, v in payload.model_dump().items()}}
    await db.commit()
    return await view(db, user, project)
