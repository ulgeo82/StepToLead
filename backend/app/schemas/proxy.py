from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProxyBulkCreate(BaseModel):
    entries: str = Field(min_length=3, max_length=100_000)
    pool: str = Field(default="agency", pattern=r"^(agency|client)$")
    scheme: str = Field(default="auto", pattern=r"^(auto|socks5|http)$")   # тип для строк без схемы
    max_accounts: int | None = Field(default=None, ge=1, le=100)           # по умолчанию: аутрич 1, CRM-профили 3


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
    exit_ip: str | None = None
    latency_ms: int | None = None
    rotating: bool = False
    duplicate_of: int | None = None
    health: dict | None = None
    accounts_info: list[dict] = Field(default=[], serialization_alias="accounts")   # аккаунты аутрича на прокси

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ProxyBulkResult(BaseModel):
    imported: int
    skipped: int                      # дубли и ошибки вместе — как раньше
    duplicates: int = 0
    errors: list[str]
    batch_id: int | None = None       # проверка добавленных идёт в фоне: GET /proxies/checks/{batch_id}/progress
