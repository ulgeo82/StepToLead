"""Durable, sanitized campaign events shown in the portal execution journal."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import CampaignEvent


async def add_campaign_event(
    db: AsyncSession,
    campaign_id: int,
    level: str,
    event: str,
    message: str,
    *,
    account_id: int | None = None,
    username: str | None = None,
    details: dict | None = None,
    commit: bool = True,
) -> CampaignEvent:
    """Store an operational event, deliberately excluding secrets and message text."""
    row = CampaignEvent(
        campaign_id=campaign_id,
        level=level,
        event=event,
        message=message[:2000],
        account_id=account_id,
        username=username.lstrip("@")[:64] if username else None,
        details=details,
    )
    db.add(row)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return row
