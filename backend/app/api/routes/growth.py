import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.access import rate_limit
from app.core.access import require_admin
from app.db import get_db
from app.models.access import GrowthCalculation
from app.services.business_economics import EconomicsInput, EconomicsResult, calculateBusinessEconomics

public_router = APIRouter(prefix="/public/growth", tags=["public-growth"])
admin_router = APIRouter(prefix="/admin/growth", tags=["admin-growth"], dependencies=[Depends(require_admin)])


class GrowthContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: EconomicsInput
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    telegram: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    consent: bool

    @field_validator("name", "phone", "telegram", "email", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_contact(self):
        if not self.consent:
            raise ValueError("Нужно согласие на обработку заявки")
        if not (self.phone or self.telegram or self.email):
            raise ValueError("Укажите телефон, Telegram или email — достаточно одного способа связи")
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise ValueError("Проверьте email")
        if self.telegram and not re.fullmatch(r"@?[a-zA-Z][a-zA-Z0-9_]{4,31}", self.telegram):
            raise ValueError("Укажите Telegram в формате @username")
        if self.phone and not 7 <= len(re.sub(r"\D", "", self.phone)) <= 15:
            raise ValueError("Проверьте телефон")
        return self


@public_router.post("/calculate", response_model=EconomicsResult)
async def calculate(payload: EconomicsInput):
    return calculateBusinessEconomics(payload)


@public_router.post("/leads", status_code=201)
async def save_lead(payload: GrowthContact, request: Request, db: AsyncSession = Depends(get_db)):
    await rate_limit(request, "growth-lead", 10, 3600)
    results = calculateBusinessEconomics(payload.inputs)
    record = GrowthCalculation(name=payload.name, phone=payload.phone or None, telegram=payload.telegram or None,
                               email=payload.email or None, inputs=payload.inputs.model_dump(),
                               results=results.model_dump(), status=results.status)
    db.add(record)
    await db.commit()
    return {"id": record.id, "message": "Расчёт и контакты сохранены. Спасибо!"}


@admin_router.get("/leads")
async def list_growth_leads(offset: int = 0, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(GrowthCalculation).order_by(GrowthCalculation.created_at.desc()).offset(max(0, offset)).limit(50))).all()
    return [{"id": row.id, "name": row.name, "phone": row.phone, "telegram": row.telegram, "email": row.email,
             "inputs": row.inputs, "results": row.results, "status": row.status, "created_at": row.created_at} for row in rows]
