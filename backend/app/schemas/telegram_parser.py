from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TelegramParseCreate(BaseModel):
    account_id: int
    sources: list[str] = Field(min_length=1, max_length=50)
    collect_messages: bool = True
    collect_members: bool = True
    collect_comments: bool = True
    extended_profile: bool = False
    per_source_limit: int = Field(default=100, ge=1, le=5000)
    total_limit: int = Field(default=5000, ge=1, le=10000)
    activity_filter: Literal["all", "online", "recent"] = "all"
    max_offline_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("sources")
    @classmethod
    def clean_sources(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            value = value.strip()
            if value and value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            raise ValueError("Добавьте хотя бы одну ссылку на чат или канал")
        return cleaned

    @model_validator(mode="after")
    def validate_modes(self):
        if not (self.collect_messages or self.collect_members or self.collect_comments):
            raise ValueError("Выберите хотя бы один способ сбора")
        return self
