import unittest
from datetime import datetime, timedelta, timezone

from telethon.tl.types import User, UserStatusOffline, UserStatusOnline, UserStatusRecently

from app.schemas.telegram_parser import TelegramParseCreate
from app.services.telegram_parser import activity_allowed, normalize_source, source_link


class TelegramParserPolicyTests(unittest.TestCase):
    def test_source_normalization(self):
        self.assertEqual(normalize_source("https://t.me/example_chat/"), "example_chat")
        self.assertEqual(normalize_source("@example_chat"), "example_chat")
        self.assertEqual(normalize_source("https://t.me/+invite-code"), "https://t.me/+invite-code")
        self.assertEqual(source_link("@example_chat"), "https://t.me/example_chat")

    def test_create_requires_collection_mode(self):
        with self.assertRaises(ValueError):
            TelegramParseCreate(account_id=1, sources=["@chat"], collect_messages=False, collect_members=False, collect_comments=False)

    def test_activity_filters(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(activity_allowed(User(id=1, status=UserStatusOnline(expires=now)), "online", None, now))
        self.assertTrue(activity_allowed(User(id=2, status=UserStatusRecently(by_me=False)), "recent", 7, now))
        old = User(id=3, status=UserStatusOffline(was_online=now - timedelta(days=10)))
        self.assertFalse(activity_allowed(old, "all", 7, now))
        self.assertTrue(activity_allowed(old, "all", 30, now))
