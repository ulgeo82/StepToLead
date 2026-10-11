from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProxyBulkCreate(BaseModel):
    entries: str = Field(min_length=3, max_length=100_000)
    pool: str = Field(default="agency", pattern=r"^(agency|client)$")


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
    pool: str = "agency"
    max_accounts: int = 3
    dedicated_workspace_id: int | None = None
    client_accounts: int = 0
    dedicated_workspace_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ProxyBulkResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
