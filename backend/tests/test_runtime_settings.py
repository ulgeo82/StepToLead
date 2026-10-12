"""Ключи ИИ из портала: сохраняются зашифрованными, применяются без перезапуска, наружу не отдаются."""
import asyncio
import os
import tempfile
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.core.config import settings
from app.db import Base
from app.models.system import AppSetting
from app.services import ai, runtime_settings


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.saved = {k: getattr(settings, k) for k in ("llm_provider", "llm_api_key", "llm_model")}
        settings.llm_provider, settings.llm_api_key, settings.llm_model = "", "", ""
        runtime_settings._original.clear()

        async def init():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        asyncio.run(init())

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(settings, k, v)
        os.environ.pop("DADATA_TOKEN", None)
        runtime_settings._original.clear()
        asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def run_db(self, fn):
        async def go():
            async with self.sessions() as db:
                return await fn(db)
        return asyncio.run(go())

    def test_save_apply_and_mask(self):
        self.assertFalse(ai.configured())
        self.run_db(lambda db: runtime_settings.save(db, {"llm_provider": "anthropic", "llm_api_key": "sk-secret-123456",
                                                          "llm_model": "claude-sonnet-5-5", "DADATA_TOKEN": "tok-987654321"}, []))
        self.assertTrue(ai.configured())
        self.assertEqual(settings.llm_api_key, "sk-secret-123456")
        self.assertEqual(os.environ["DADATA_TOKEN"], "tok-987654321")
        row = self.run_db(lambda db: db.get(AppSetting, "integrations"))
        self.assertNotIn("sk-secret", str(row.value))  # в базе — только шифр
        public = self.run_db(runtime_settings.public)
        self.assertEqual(public["llm_api_key"], {"source": "portal", "set": True, "hint": "…3456"})
        self.assertNotIn("sk-secret", str(public))
        # пустой секрет при сохранении — ключ не меняется
        self.run_db(lambda db: runtime_settings.save(db, {"llm_api_key": "", "llm_model": "claude-opus-5-5"}, []))
        self.assertEqual(settings.llm_api_key, "sk-secret-123456")
        self.assertEqual(settings.llm_model, "claude-opus-5-5")
        # очистка — возвращаемся к значению из .env (здесь пусто)
        self.run_db(lambda db: runtime_settings.save(db, {}, ["llm_api_key"]))
        self.assertEqual(settings.llm_api_key, "")
        self.assertFalse(ai.configured())


if __name__ == "__main__":
    unittest.main()
