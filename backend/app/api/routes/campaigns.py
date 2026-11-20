from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Campaign, Lead
from app.schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from app.schemas.lead import ImportResult
from app.services.importer import parse_leads_file
from app.domains.leadgen import space
from app.services import tg_space

# Кампании Telegram-аутрича — в пространстве выбранного клиента (этап B, docs/OUTREACH_INBOX.md).
router = APIRouter(prefix="/campaigns", tags=["campaigns"], dependencies=[Depends(space.bind)])


@router.get("", response_model=list[CampaignRead])
async def list_campaigns(db: AsyncSession = Depends(get_db)):
    query = (
        select(Campaign, func.count(Lead.id).label("leads_count"))
        .outerjoin(Lead)
        .group_by(Campaign.id)
        .order_by(Campaign.created_at.desc())
    )
    query = tg_space.scope(query, Campaign.workspace_id)
    rows = (await db.execute(query)).all()
    return [CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count}) for campaign, count in rows]


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_campaign(payload: CampaignCreate, db: AsyncSession = Depends(get_db)):
    campaign = Campaign(**payload.model_dump(), workspace_id=tg_space.current())
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    campaign = tg_space.require(await db.get(Campaign, campaign_id), "Кампания не найдена")
    count = await db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id))
    return CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count or 0})


@router.patch("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(campaign_id: int, payload: CampaignUpdate, db: AsyncSession = Depends(get_db)):
    campaign = tg_space.require(await db.get(Campaign, campaign_id), "Кампания не найдена")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(campaign, field, value)
    await db.commit()
    await db.refresh(campaign)
    count = await db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id))
    return CampaignRead.model_validate(campaign).model_copy(update={"leads_count": count or 0})


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    campaign = tg_space.require(await db.get(Campaign, campaign_id), "Кампания не найдена")
    await db.delete(campaign)
    await db.commit()


@router.post("/{campaign_id}/import", response_model=ImportResult)
async def import_leads(campaign_id: int, file: UploadFile, db: AsyncSession = Depends(get_db)):
    campaign = tg_space.require(await db.get(Campaign, campaign_id), "Кампания не найдена")
    try:
        parsed, errors = parse_leads_file(file.filename or "", await file.read())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = set((await db.scalars(select(Lead.username).where(Lead.campaign_id == campaign_id))).all())
    # Одному человеку компания пишет один раз: дубли из других гипотез этой же компании — пропускаем.
    from app.models.automation import GlobalBlock
    company = set((await db.scalars(select(Lead.username).join(Campaign, Campaign.id == Lead.campaign_id).where(
        Lead.campaign_id != campaign_id,
        tg_space.belongs(Campaign.workspace_id, campaign.workspace_id or tg_space.home())))).all())
    blocked_names = {(u or "").lower().lstrip("@") for u in (await db.scalars(select(GlobalBlock.username))).all()}
    imported = 0
    skipped = len(errors)
    in_company = blocked = 0
    for item in parsed:
        if item.username in existing:
            skipped += 1
            continue
        if item.username in company:
            in_company += 1
            continue
        if item.username in blocked_names:
            blocked += 1
            continue
        db.add(Lead(campaign_id=campaign_id, username=item.username, first_message=item.first_message))
        existing.add(item.username)
        imported += 1
    await db.commit()
    return ImportResult(imported=imported, skipped=skipped, errors=errors[:20], already_in_company=in_company, blocked=blocked)
