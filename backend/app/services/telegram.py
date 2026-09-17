import io
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from opentele2.api import API
from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UsernameInvalidError,
    UsernameOccupiedError,
)
from telethon.sessions import StringSession

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.proxy import Proxy
from app.models.telegram_account import TelegramAccount


def ensure_session_storage_configured() -> None:
    """Не позволяет сохранять Telegram-сессии с демонстрационным ключом."""
    if settings.encryption_secret == "change-this-before-production":
        raise HTTPException(
            status_code=503,
            detail="Задайте уникальный ENCRYPTION_SECRET в файле .env",
        )


def telegram_api_credentials() -> tuple[int, str]:
    """Использует пользовательский API либо официальный API Telegram Desktop.

    Для импорта существующей tdata отдельная регистрация приложения на
    my.telegram.org не требуется: сессия продолжает работать как Telegram
    Desktop с официальными реквизитами, предоставляемыми OpenTele.
    """
    if bool(settings.telegram_api_id) != bool(settings.telegram_api_hash):
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_API_ID и TELEGRAM_API_HASH должны быть заданы вместе",
        )
    if settings.telegram_api_id and settings.telegram_api_hash:
        return settings.telegram_api_id, settings.telegram_api_hash
    return API.TelegramDesktop.api_id, API.TelegramDesktop.api_hash


def ensure_telegram_configured() -> None:
    ensure_session_storage_configured()
    telegram_api_credentials()


def proxy_config(proxy: Proxy | None) -> dict | None:
    if not proxy:
        return None
    return {
        "proxy_type": proxy.scheme,
        "addr": proxy.host,
        "port": proxy.port,
        "username": proxy.username,
        "password": decrypt_secret(proxy.password_encrypted),
        "rdns": True,
    }


def make_client(session_value: str | None = None, proxy: Proxy | None = None) -> TelegramClient:
    ensure_telegram_configured()
    api_id, api_hash = telegram_api_credentials()
    return TelegramClient(
        StringSession(session_value or ""),
        api_id,
        api_hash,
        proxy=proxy_config(proxy),
        device_model="Reachboard",
        system_version="1.0",
        app_version="0.1",
        auto_reconnect=False,
        request_retries=0,
        connection_retries=1,
        timeout=10,
        flood_sleep_threshold=0,
    )


async def request_login_code(account: TelegramAccount, proxy: Proxy | None) -> None:
    client = make_client(proxy=proxy)
    try:
        await client.connect()
        sent = await client.send_code_request(account.phone)
        account.pending_session_encrypted = encrypt_secret(client.session.save())
        account.phone_code_hash = sent.phone_code_hash
        account.status = "code_sent"
        account.last_error = None
    except FloodWaitError as exc:
        account.status = "flood_wait"
        account.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
        account.last_error = f"Telegram просит подождать {exc.seconds} сек."
        raise HTTPException(status_code=429, detail=account.last_error) from exc
    except Exception as exc:
        account.status = "error"
        account.last_error = str(exc)[:500]
        raise HTTPException(status_code=502, detail=f"Telegram не принял запрос: {exc}") from exc
    finally:
        await client.disconnect()


async def confirm_login(account: TelegramAccount, code: str | None, password: str | None, proxy: Proxy | None) -> bool:
    pending = decrypt_secret(account.pending_session_encrypted)
    if not pending:
        raise HTTPException(status_code=409, detail="Сначала запросите код подтверждения")
    client = make_client(pending, proxy)
    try:
        await client.connect()
        if account.requires_2fa:
            if not password:
                raise HTTPException(status_code=409, detail="Введите пароль двухэтапной аутентификации")
            await client.sign_in(password=password)
        else:
            if not code:
                raise HTTPException(status_code=422, detail="Введите код из Telegram")
            try:
                await client.sign_in(account.phone, code, phone_code_hash=account.phone_code_hash)
            except SessionPasswordNeededError:
                account.requires_2fa = True
                account.pending_session_encrypted = encrypt_secret(client.session.save())
                account.status = "password_required"
                return True
        account.session_encrypted = encrypt_secret(client.session.save())
        account.pending_session_encrypted = None
        account.phone_code_hash = None
        account.requires_2fa = False
        account.status = "online"
        account.last_error = None
        account.last_seen_at = datetime.now(timezone.utc)
        await sync_account_from_telegram(account, client)
        return False
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        account.last_error = "Неверный или просроченный код"
        raise HTTPException(status_code=422, detail=account.last_error) from exc
    except PasswordHashInvalidError as exc:
        account.last_error = "Неверный пароль 2FA"
        raise HTTPException(status_code=422, detail=account.last_error) from exc
    finally:
        await client.disconnect()


async def sync_account_from_telegram(account: TelegramAccount, client: TelegramClient | None = None) -> None:
    own_client = client is None
    if own_client:
        session_value = decrypt_secret(account.session_encrypted)
        if not session_value:
            raise HTTPException(status_code=409, detail="Аккаунт ещё не авторизован")
        client = make_client(session_value, account.proxy)
        await client.connect()
    assert client is not None
    try:
        me = await client.get_me()
        if not me:
            raise HTTPException(status_code=502, detail="Telegram не вернул данные аккаунта")
        full = await client(functions.users.GetFullUserRequest("me"))
        account.telegram_user_id = me.id
        account.first_name = me.first_name
        account.last_name = me.last_name
        account.username = me.username
        account.bio = full.full_user.about
        account.status = "online"
        account.last_error = None
        account.last_seen_at = datetime.now(timezone.utc)
        account.session_encrypted = encrypt_secret(client.session.save())
    finally:
        if own_client:
            await client.disconnect()


async def update_telegram_profile(
    account: TelegramAccount,
    first_name: str,
    last_name: str | None,
    bio: str | None,
    username: str | None,
    avatar: tuple[str, bytes] | None,
) -> None:
    session_value = decrypt_secret(account.session_encrypted)
    if not session_value:
        raise HTTPException(status_code=409, detail="Аккаунт ещё не авторизован")
    client = make_client(session_value, account.proxy)
    try:
        await client.connect()
        await client(functions.account.UpdateProfileRequest(first_name=first_name, last_name=last_name or "", about=bio or ""))
        normalized_username = (username or "").lstrip("@").strip()
        if normalized_username != (account.username or ""):
            await client(functions.account.UpdateUsernameRequest(normalized_username))
        if avatar:
            filename, content = avatar
            stream = io.BytesIO(content)
            stream.name = filename
            uploaded = await client.upload_file(stream)
            await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
            account.avatar_version += 1
        await sync_account_from_telegram(account, client)
    except UsernameOccupiedError as exc:
        raise HTTPException(status_code=409, detail="Этот username уже занят") from exc
    except UsernameInvalidError as exc:
        raise HTTPException(status_code=422, detail="Telegram отклонил формат username") from exc
    except FloodWaitError as exc:
        account.status = "flood_wait"
        account.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
        raise HTTPException(status_code=429, detail=f"Изменения временно ограничены. Подождите {exc.seconds} сек.") from exc
    finally:
        await client.disconnect()


async def check_username(account: TelegramAccount, username: str) -> bool:
    session_value = decrypt_secret(account.session_encrypted)
    if not session_value:
        raise HTTPException(status_code=409, detail="Аккаунт ещё не авторизован")
    client = make_client(session_value, account.proxy)
    try:
        await client.connect()
        return bool(await client(functions.account.CheckUsernameRequest(username.lstrip("@"))))
    except UsernameInvalidError as exc:
        raise HTTPException(status_code=422, detail="Username должен содержать минимум 5 латинских символов, цифр или подчёркиваний") from exc
    finally:
        await client.disconnect()


async def suggest_username(account: TelegramAccount) -> str:
    base = re.sub(r"[^a-z0-9]", "", f"{account.first_name or ''}{account.last_name or ''}".lower())
    roots = [base] if len(base) >= 4 else ["northpeak", "brightpath", "orbitline", "novaflow"]
    for _ in range(8):
        candidate = f"{secrets.choice(roots)[:20]}{secrets.randbelow(8999) + 1000}"
        if await check_username(account, candidate):
            return candidate
    raise HTTPException(status_code=409, detail="Не удалось найти свободный вариант — попробуйте ещё раз")
