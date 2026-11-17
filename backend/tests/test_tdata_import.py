"""TData connection retries are isolated and never contact Telegram."""
import unittest
import io
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.tdata_import import (
    TelegramConnectionError, _connect_with_retries, _find_tdata_folder, _safe_extract_zip,
    convert_tdata_archive, tdata_user_id,
)


class ArchiveTests(unittest.TestCase):
    def archive(self, filename, content=b"sample"):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(filename, content)
        return output.getvalue()

    def test_nested_tdata_and_offline_identity(self):
        content = self.archive("customer/export/nested/tdata/key_datas")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _safe_extract_zip(content, root)
            self.assertEqual(_find_tdata_folder(root), (root / "customer/export/nested/tdata").resolve())
        desktop = Mock(mainAccount=SimpleNamespace(UserId=12345))
        desktop.isLoaded.return_value = True
        with patch("app.services.tdata_import.TDesktop", return_value=desktop):
            self.assertEqual(tdata_user_id(content), 12345)
        desktop.ToTelethon.assert_not_called()

    def test_precise_errors(self):
        for content, message in (
            (b"broken", "Архив повреждён"),
            (self.archive("notes.txt"), "В архиве нет папки tdata"),
            (self.archive("nested/account.session"), "Формат .session/.json — пока не поддерживается"),
            (self.archive("nested/account.json"), "Формат .session/.json — пока не поддерживается"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                tdata_user_id(content)

    def test_damaged_member_is_reported(self):
        content = self.archive("tdata/key_data")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(zipfile.ZipFile, "open", side_effect=zipfile.BadZipFile("CRC")):
            with self.assertRaisesRegex(ValueError, "Архив повреждён"):
                _safe_extract_zip(content, Path(directory))


class FakeClient:
    def __init__(self, effects):
        self.connect = AsyncMock(side_effect=effects)
        self.disconnect = AsyncMock()


class ConnectionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversion_never_connects_without_proxy(self):
        with patch("app.services.tdata_import.TDesktop") as desktop:
            with self.assertRaisesRegex(ValueError, "без него аккаунт не подключаем"):
                await convert_tdata_archive(b"archive", None)
        desktop.assert_not_called()

    async def test_retries_transient_proxy_failure(self):
        client = FakeClient([ConnectionError("bad exit"), None])
        with patch("app.services.tdata_import.asyncio.sleep", new=AsyncMock()):
            await _connect_with_retries(client)

        self.assertEqual(client.connect.await_count, 2)
        client.disconnect.assert_awaited_once()

    async def test_reports_failure_after_all_attempts(self):
        client = FakeClient(ConnectionError("proxy refused"))
        with patch("app.services.tdata_import.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(TelegramConnectionError, "после 3 попыток"):
                await _connect_with_retries(client)

        self.assertEqual(client.connect.await_count, 3)
        self.assertEqual(client.disconnect.await_count, 3)


if __name__ == "__main__":
    unittest.main()
