"""Review fixes: polite AI answers are not turned into hand-offs; Telethon errors reach users in Russian."""
import unittest

from app.services import chat_automation
from app.services.telegram_profiles import error_text


class ChannelReviewFixes(unittest.TestCase):
    def test_polite_answer_without_commercial_claims_passes(self):
        kb = "Кухни на заказ. Замер бесплатный."
        text = "Здравствуйте! Подскажите, какой срок вам удобен для замера?"
        self.assertEqual(chat_automation.grounded(text, kb), text)
        self.assertEqual(chat_automation.grounded("Сделаем за 10 дней", kb), "Уточню у менеджера.")

    def test_error_texts_are_russian(self):
        self.assertEqual(error_text(ValueError("Could not find the input entity for PeerUser")),
                         "Не удалось найти этот чат в Telegram-профиле")
        self.assertEqual(error_text(ValueError("Нет связи через прокси")), "Нет связи через прокси")
        self.assertEqual(error_text(ConnectionError("reset")), "Нет связи через прокси")
        self.assertIn("попробуйте ещё раз", error_text(RuntimeError("boom")))
