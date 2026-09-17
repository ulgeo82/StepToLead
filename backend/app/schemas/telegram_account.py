from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TelegramConfigRead(BaseModel):
    configured: bool


class AuthRequestCode(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    proxy_id: int | None = None


class AuthConfirmCode(BaseModel):
    account_id: int
    code: str | None = Field(default=None, max_length=16)
    password: str | None = Field(default=None, max_length=256)


class AccountProxyUpdate(BaseModel):
    proxy_id: int | None = None


class TelegramAccountRead(BaseModel):
    id: int
    phone: str
    telegram_user_id: int | None
    first_name: str | None
    last_name: str | None
    username: str | None
    bio: str | None
    avatar_version: int
    status: str
    proxy_id: int | None
    requires_2fa: bool
    last_error: str | None
    flood_wait_until: datetime | None
    last_seen_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UsernameCheck(BaseModel):
    username: str = Field(min_length=5, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_]+$")


class UsernameAvailability(BaseModel):
    username: str
    available: bool


class AuthResult(BaseModel):
    account: TelegramAccountRead
    requires_2fa: bool = False


class TDataImportItem(BaseModel):
    filename: str
    status: Literal["imported", "skipped", "error"]
    account_id: int | None = None
    phone: str | None = None
    error: str | None = None


class TDataImportResult(BaseModel):
    imported: int
    skipped: int
    failed: int
    items: list[TDataImportItem]
