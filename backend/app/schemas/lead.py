from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.lead import LeadStatus


class LeadRead(BaseModel):
    id: int
    campaign_id: int
    username: str
    first_message: str
    status: LeadStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LeadStatusUpdate(BaseModel):
    status: LeadStatus


class LeadMessageUpdate(BaseModel):
    first_message: str = Field(min_length=1, max_length=4096)


class ImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
