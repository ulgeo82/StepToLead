"""Свой бот клиента: подключение по ссылке, меню только своей компании, отвязка только в том же боте."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router  # noqa: F401 register all models
from app.core.access import token_digest
from app.core.crypto import encrypt_secret
from app.db import Base
from app.models.marketing import ClientWorkspace, PortalUser, Project
from app.models.system import WorkspaceBot
from app.services import client_bots, notifications, tg_bot
from app.services.telegram_http import BotScope, bot_scope, current_token

TOKEN = "123456789:AAH" + "x" * 32


class ClientBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([ClientWorkspace(id=1, name="Кухни «Север»"), ClientWorkspace(id=2, name="Другая")])
            db.add_all([Project(id=1, workspace_id=1, name="Кухни"), Project(id=2, workspace_id=2, name="Чужой")])
            db.add_all([PortalUser(id=1, workspace_id=1, username="owner", display_name="Owner", password_hash="x",
                                   role="client_owner", active=True,
                                   telegram_link_code_hash=token_digest("code1"),
                                   telegram_link_expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
                        PortalUser(id=2, workspace_id=2, username="other", display_name="Other", password_hash="x",
                                   role="client_owner", active=True, telegram_chat_id="100",
                                   telegram_link_code_hash=token_digest("code2"),
                                   telegram_link_expires_at=datetime.now(timezone.utc) + timedelta(days=1))])
            db.add(WorkspaceBot(id=5, workspace_id=1, token_encrypted=encrypt_secret(TOKEN),
                                token_hash=client_bots.token_hash(TOKEN), bot_id="1", username="sever_bot"))
            await db.commit()
        self.api = AsyncMock(return_value={})
        self.patches = [patch.object(tg_bot, "telegram_api", self.api), patch.object(tg_bot, "flush_telegram"),
                        patch.object(notifications.settings, "telegram_bot_token", "agency-token")]
        for p in self.patches: p.start()
        self.scope = BotScope(token=TOKEN, workspace_id=1, bot_row_id=5, username="sever_bot")
        self.reset = bot_scope.set(self.scope)
        self.n = 0

    async def asyncTearDown(self):
        bot_scope.reset(self.reset)
        for p in reversed(self.patches): p.stop()
        await self.engine.dispose()

    async def send(self, text, chat=100):
        self.n += 1
        self.api.reset_mock()
        await tg_bot.process_update({"update_id": self.n, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"},
                                     "from": {"id": chat}, "text": text}}, self.sessions)
        return [c.args[1].get("text", "") for c in self.api.await_args_list]

    async def test_link_menu_and_isolation(self):
        self.assertEqual(current_token(), TOKEN)
        # Ссылка сотрудника другой компании в этом боте не срабатывает.
        self.assertIn("другого бота", (await self.send("/start code2"))[0])
        # Чужой сотрудник (подключён к боту агентства с тем же chat_id) меню не получает.
        self.assertIn("Подключите Telegram", (await self.send("/menu"))[0])
        self.assertIn("Telegram подключён", (await self.send("/start code1", chat=300))[0])
        async with self.sessions() as db:
            owner = await db.get(PortalUser, 1)
            self.assertEqual(owner.telegram_state["bot_id"], 5)
            self.assertEqual((await client_bots.scope_for_user(db, owner)).username, "sever_bot")
            self.assertIsNone(await client_bots.scope_for_user(db, await db.get(PortalUser, 2)))
        self.assertIn("Кухни «Север»", (await self.send("/menu", chat=300))[0])
        self.assertIn("Неизвестная команда", (await self.send("/admin", chat=300))[0])

    async def test_block_unlinks_only_this_bot(self):
        await self.send("/start code1", chat=100)  # owner компании 1 и other (агентство) с одним chat_id
        await tg_bot.unlink_blocked("100", self.sessions)
        async with self.sessions() as db:
            self.assertIsNone((await db.get(PortalUser, 1)).telegram_chat_id)
            self.assertEqual((await db.get(PortalUser, 2)).telegram_chat_id, "100")

    async def test_bad_token_rejected(self):
        async with self.sessions() as db:
            with self.assertRaises(client_bots.BotTokenError):
                await client_bots.connect(db, 1, "hello", "X")
            with self.assertRaises(client_bots.BotTokenError):
                await client_bots.connect(db, 2, TOKEN, "Другая")  # бот уже у другой компании


if __name__ == "__main__":
    unittest.main()
