from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Campaign, Lead
from app.schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from app.schemas.lead import ImportResult
from app.services.importer import parse_leads_file

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("", response_model=list[CampaignRead])
async def list_campaigns(db: AsyncSession = Depends(get_db)):
    query = (
        select(Campaign, func.count(Lead.id).label("leads_count"))
        .outerjoin(Lead)
        .group_by(Campaign.id)
        .order_by(Campaign.created_at.desc())
    )
    rows = (await db.execute(query)).all()
    return [CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count}) for campaign, count in rows]


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_campaign(payload: CampaignCreate, db: AsyncSession = Depends(get_db)):
    campaign = Campaign(**payload.model_dump())
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    count = await db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id))
    return CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count or 0})


@router.patch("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(campaign_id: int, payload: CampaignUpdate, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(campaign, field, value)
    await db.commit()
    await db.refresh(campaign)
    count = await db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id))
    return CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count or 0})


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    await db.delete(campaign)
    await db.commit()


@router.post("/{campaign_id}/import", response_model=ImportResult)
async def import_leads(campaign_id: int, file: UploadFile, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    try:
        parsed, errors = parse_leads_file(file.filename or "", await file.read())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = set((await db.scalars(select(Lead.username).where(Lead.campaign_id == campaign_id))).all())
    imported = 0
    skipped = len(errors)
    for item in parsed:
        if item.username in existing:
            skipped += 1
            continue
        db.add(Lead(campaign_id=campaign_id, username=item.username, first_message=item.first_message))
        existing.add(item.username)
        imported += 1
    await db.commit()
    return ImportResult(imported=imported, skipped=skipped, errors=errors[:20])
