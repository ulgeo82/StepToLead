from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.domains.leadgen import space
from app.models import Campaign, Lead, LeadStatus
from app.services import tg_space
from app.schemas.lead import LeadMessageUpdate, LeadRead, LeadStatusUpdate

router = APIRouter(prefix="/leads", tags=["leads"], dependencies=[Depends(space.bind)])


async def _lead(db, lead_id):
    """Получатель — только из кампании выбранного клиента."""
    lead = await db.get(Lead, lead_id)
    campaign = await db.get(Campaign, lead.campaign_id) if lead else None
    if not lead or not campaign or not tg_space.owns(campaign.workspace_id):
        raise HTTPException(status_code=404, detail="Лид не найден")
    return lead


@router.get("", response_model=list[LeadRead])
async def list_leads(
    campaign_id: int | None = None,
    lead_status: LeadStatus | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = tg_space.scope(select(Lead).join(Campaign, Campaign.id == Lead.campaign_id), Campaign.workspace_id) \
        .order_by(Lead.created_at.desc())
    if campaign_id is not None:
        query = query.where(Lead.campaign_id == campaign_id)
    if lead_status is not None:
        query = query.where(Lead.status == lead_status)
    return list((await db.scalars(query)).all())


@router.get("/{lead_id}", response_model=LeadRead)
async def get_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    lead = await _lead(db, lead_id)
    return lead


@router.patch("/{lead_id}/status", response_model=LeadRead)
async def update_status(lead_id: int, payload: LeadStatusUpdate, db: AsyncSession = Depends(get_db)):
    lead = await _lead(db, lead_id)
    lead.status = payload.status
    await db.commit()
    await db.refresh(lead)
    return lead


@router.patch("/{lead_id}/message", response_model=LeadRead)
async def update_message(lead_id: int, payload: LeadMessageUpdate, db: AsyncSession = Depends(get_db)):
    lead = await _lead(db, lead_id)
    lead.first_message = payload.first_message.strip()
    await db.commit()
    await db.refresh(lead)
    return lead


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    lead = await _lead(db, lead_id)
    await db.delete(lead)
    await db.commit()
