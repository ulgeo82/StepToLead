"""Ключи и модели ИИ (и сервисов лидгена), которые админ задаёт в портале, а не в .env.prod.

Хранятся в app_settings («integrations»); секреты — зашифрованы ENCRYPTION_SECRET и наружу не отдаются.
Значение из портала главнее .env; пустое поле в портале — берётся значение из .env.
"""
import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.system import AppSetting

KEY = "integrations"
# поле: (куда применять: "settings" | "env", секрет?, тип)
FIELDS = {
    "llm_provider": ("settings", False, str),
    "llm_api_key": ("settings", True, str),
    "llm_model": ("settings", False, str),
    "llm_base_url": ("settings", False, str),
    "yandex_folder_id": ("settings", False, str),
    "llm_model_chat": ("settings", False, str),
    "llm_model_summary": ("settings", False, str),
    "llm_model_calls": ("settings", False, str),
    "llm_model_campaigns": ("settings", False, str),
    "stt_provider": ("settings", False, str),
    "stt_api_key": ("settings", True, str),
    "stt_model": ("settings", False, str),
    "ai_daily_token_limit": ("settings", False, int),
    "ai_workspace_daily_token_limit": ("settings", False, int),
    "XMLSTOCK_USER": ("env", False, str),
    "XMLSTOCK_KEY": ("env", True, str),
    "XMLSTOCK_LIVE_URL": ("env", False, str),
    "DADATA_TOKEN": ("env", True, str),
}
_original: dict[str, object] = {}


def _current(name):
    target = FIELDS[name][0]
    return getattr(settings, name) if target == "settings" else os.environ.get(name, "")


def _set(name, value):
    target, _, kind = FIELDS[name]
    if name not in _original:
        _original[name] = _current(name)  # значение из .env — вернём, если в портале поле очистят
    if value in (None, ""):
        value = _original[name]
    elif kind is int:
        value = int(value)
    if target == "settings":
        setattr(settings, name, value)
    elif value:
        os.environ[name] = str(value)
    else:
        os.environ.pop(name, None)


async def stored(db: AsyncSession) -> dict:
    row = await db.get(AppSetting, KEY)
    return dict(row.value or {}) if row else {}


async def apply(db: AsyncSession) -> None:
    data = await stored(db)
    for name, (_, secret, _) in FIELDS.items():
        raw = data.get(name)
        try:
            value = decrypt_secret(raw) if secret and raw else raw
        except ValueError:
            value = None
        _set(name, value)


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 8 else "задан"


async def public(db: AsyncSession) -> dict:
    """Для формы: обычные поля — как есть, секреты — только «задан …abcd» и откуда взят."""
    data = await stored(db)
    out = {}
    for name, (_, secret, _) in FIELDS.items():
        from_portal = data.get(name) not in (None, "")
        effective = _current(name)
        entry = {"source": "portal" if from_portal else ("env" if effective not in (None, "", 0) else None)}
        if secret:
            entry["set"] = bool(effective)
            entry["hint"] = _mask(str(effective)) if effective else ""
        else:
            entry["value"] = data.get(name) if from_portal else ""
            entry["effective"] = effective
        out[name] = entry
    return out


async def save(db: AsyncSession, values: dict, clear: list[str]) -> None:
    """Секрет: пустое поле = не менять; в clear — удалить (вернуться к .env)."""
    row = await db.get(AppSetting, KEY)
    data = dict(row.value or {}) if row else {}
    for name, value in values.items():
        if name not in FIELDS:
            continue
        _, secret, kind = FIELDS[name]
        value = "" if value is None else str(value).strip()
        if secret:
            if value:
                data[name] = encrypt_secret(value)
            continue
        if kind is int and value:
            int(value)  # проверка: ValueError → 422
        data[name] = value
    for name in clear:
        if name in FIELDS:
            data.pop(name, None)
    if row:
        row.value = data
    else:
        db.add(AppSetting(key=KEY, value=data))
    await db.commit()
    await apply(db)
