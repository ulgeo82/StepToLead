import re
from pydantic import BaseModel, Field, model_validator


class AutomationSettings(BaseModel):
    system_prompt: str = Field(default="", max_length=30000)
    ai_model: str = Field(default="", max_length=120)
    ai_url: str = Field(default="", max_length=500)
    auto_reply: bool = True
    context_messages: int = Field(default=20, ge=1, le=100)
    forwarded_messages: int = Field(default=5, ge=1, le=50)
    positive_trigger: str = Field(default="Передаю ваш контакт менеджеру", max_length=500)
    negative_trigger: str = Field(default="Хорошо, больше писать не будем", max_length=500)
    positive_chat: str = Field(default="", max_length=100)
    negative_chat: str = Field(default="", max_length=100)
    partner_chat: str = Field(default="", max_length=100)
    fallback_enabled: bool = False
    fallback_text: str = Field(default="Извините, сейчас не могу ответить. Передам ваш вопрос коллеге.", max_length=4000)
    daily_limit: int = Field(default=3, ge=0, le=1000)
    first_message_max: int = Field(default=1000, ge=1, le=4096)
    timezone_offset: int = Field(default=4, ge=-12, le=14)
    restriction_hours: int = Field(default=24, ge=1, le=720)
    read_min: int = Field(default=5, ge=0, le=3600)
    read_max: int = Field(default=10, ge=0, le=3600)
    action_min: int = Field(default=60, ge=1, le=86400)
    action_max: int = Field(default=300, ge=1, le=86400)
    account_min: int = Field(default=300, ge=1, le=86400)
    account_max: int = Field(default=600, ge=1, le=86400)
    round_min: int = Field(default=300, ge=1, le=86400)
    round_max: int = Field(default=600, ge=1, le=86400)
    sleep_periods: str = Field(default="00:00-08:00", max_length=300)
    ignore_bots: bool = True
    ignore_without_username: bool = False
    blacklist: str = Field(default="SpamBot", max_length=5000)
    followup_enabled: bool = False
    followup_hours: int = Field(default=24, ge=1, le=720)
    followup_text: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_ranges(self):
        for name in ("read", "action", "account", "round"):
            if getattr(self, name + "_min") > getattr(self, name + "_max"):
                raise ValueError(f"{name}: минимум не должен превышать максимум")
        for period in self.sleep_periods.split(","):
            if period.strip() and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", period.strip()):
                raise ValueError("Периоды сна: ЧЧ:ММ-ЧЧ:ММ, через запятую")
        if self.ai_url and not self.ai_url.startswith("https://"):
            raise ValueError("Адрес ИИ должен начинаться с https://")
        if self.followup_enabled and not self.followup_text.strip():
            raise ValueError("Укажите текст follow-up")
        return self


class AutomationUpdate(BaseModel):
    settings: AutomationSettings
    account_ids: list[int] = Field(default_factory=list, max_length=50)
    api_key: str | None = Field(default=None, max_length=1000)


class BlockCreate(BaseModel):
    peer_id: int = Field(gt=0)
    username: str | None = Field(default=None, max_length=64)
