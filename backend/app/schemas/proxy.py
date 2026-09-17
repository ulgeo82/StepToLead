from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProxyBulkCreate(BaseModel):
    entries: str = Field(min_length=3, max_length=100_000)


class ProxyRead(BaseModel):
    id: int
    scheme: str
    host: str
    port: int
    username: str | None
    label: str | None
    status: str
    last_error: str | None
    last_checked_at: datetime | None
    created_at: datetime
    assigned_accounts: int = 0

    model_config = ConfigDict(from_attributes=True)


class ProxyBulkResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]

