import asyncio
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

from opentele2.api import UseCurrentSession
from opentele2.td import TDesktop
from telethon import functions
from telethon.sessions import StringSession


MAX_ARCHIVE_SIZE = 100 * 1024 * 1024
MAX_UNPACKED_SIZE = 300 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 3_000
MAX_COMPRESSION_RATIO = 200
TELEGRAM_CONNECT_ATTEMPTS = 3
TELEGRAM_CONNECT_TIMEOUT = 12


class TelegramConnectionError(ValueError):
    """Соединение с Telegram не установилось после нескольких попыток."""


@dataclass
class ImportedTData:
    session: str
    telegram_user_id: int
    phone: str
    first_name: str | None
    last_name: str | None
    username: str | None
    bio: str | None


def _safe_extract_zip(content: bytes, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("Файл не является корректным ZIP-архивом") from exc

    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("В архиве слишком много файлов")
        total_size = sum(member.file_size for member in members)
        if total_size > MAX_UNPACKED_SIZE:
            raise ValueError("Распакованный архив превышает 300 МБ")

        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("В архиве обнаружен небезопасный путь")
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise ValueError("Архив не должен содержать символические ссылки")
            if member.flag_bits & 0x1:
                raise ValueError("ZIP с паролем пока не поддерживается")
            if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
                raise ValueError("Архив имеет подозрительно высокий коэффициент сжатия")

            target = destination.joinpath(*path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _find_tdata_folder(root: Path) -> Path:
    candidates = [
        path.parent
        for filename in ("key_data", "key_datas")
        for path in root.rglob(filename)
        if path.is_file()
    ]
    if not candidates:
        raise ValueError("В архиве не найдена папка tdata с файлом key_data/key_datas")
    unique = list(dict.fromkeys(path.resolve() for path in candidates))
    if len(unique) != 1:
        raise ValueError("В одном ZIP должен находиться tdata только одного аккаунта")
    return unique[0]


async def _connect_with_retries(client) -> None:
    """Переживает смену нерабочего exit-node у мобильных proxy-pool.

    Некоторые провайдеры выбирают новый выход на каждое TCP-соединение. Один
    выход может не пропускать MTProto, хотя следующий работает нормально.
    Поэтому одна неудачная попытка не должна браковать корректную TData.
    """
    last_error: BaseException | None = None
    for attempt in range(1, TELEGRAM_CONNECT_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(client.connect(), timeout=TELEGRAM_CONNECT_TIMEOUT)
            return
        except (TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            await client.disconnect()
            if attempt < TELEGRAM_CONNECT_ATTEMPTS:
                await asyncio.sleep(1)

    detail = str(last_error).strip() if last_error else "тайм-аут соединения"
    raise TelegramConnectionError(
        "TData прочитана, но прокси не смог стабильно подключиться к Telegram "
        f"после {TELEGRAM_CONNECT_ATTEMPTS} попыток. Проверьте или замените прокси. "
        f"Техническая причина: {detail or type(last_error).__name__}"
    ) from last_error


async def convert_tdata_archive(content: bytes, proxy: dict | None = None) -> ImportedTData:
    if not content:
        raise ValueError("Архив пуст")
    if len(content) > MAX_ARCHIVE_SIZE:
        raise ValueError("Размер ZIP не должен превышать 100 МБ")

    with tempfile.TemporaryDirectory(prefix="reachboard-tdata-") as temp_directory:
        root = Path(temp_directory)
        _safe_extract_zip(content, root)
        tdata_folder = _find_tdata_folder(root)
        desktop = TDesktop(str(tdata_folder))
        if not desktop.isLoaded():
            raise ValueError("Telegram Desktop не смог прочитать эту tdata-сессию")

        client = await desktop.ToTelethon(
            session=str(root / "converted.session"),
            flag=UseCurrentSession,
            proxy=proxy,
            timeout=10,
            connection_retries=1,
            request_retries=1,
            auto_reconnect=False,
        )
        try:
            await _connect_with_retries(client)
            if not await client.is_user_authorized():
                raise ValueError("Сессия tdata больше не авторизована в Telegram")
            me = await client.get_me()
            if not me:
                raise ValueError("Telegram не вернул данные аккаунта")
            full = await client(functions.users.GetFullUserRequest("me"))
            session = StringSession.save(client.session)
            if not session:
                raise ValueError("Не удалось преобразовать Telegram-сессию")
            phone = "+" + str(me.phone or "").lstrip("+")
            if phone == "+":
                raise ValueError("У Telegram-аккаунта не удалось определить номер телефона")
            return ImportedTData(
                session=session,
                telegram_user_id=me.id,
                phone=phone,
                first_name=me.first_name,
                last_name=me.last_name,
                username=me.username,
                bio=full.full_user.about,
            )
        finally:
            await client.disconnect()
