"""Что значит ошибка Telegram и что с ней делать — одна таблица для рассылки, «Входящих», прогрева и сборщика.

kind:
- recipient — проблема у получателя: писать ему этим путём нельзя (закрыл личку, заблокировал нас, удалён).
  Сообщение точно НЕ ушло: лид — «не удалось», диалог — стоп, аккаунт работает дальше.
- limited — Telegram ограничил аккаунт (спам-блок, слишком много сообщений незнакомцам): пауза аккаунта.
- dead — аккаунт больше не может работать (сессия отозвана/вылетела, аккаунт удалён или забанен): аккаунт
  выключаем из рассылки, лид отдаём другому аккаунту.
- content — проблема в тексте (слишком длинный): лид — «не удалось», остальным пишем.
- None — неизвестно, ушло ли сообщение (обрыв связи, таймаут): автоматически НЕ повторяем, диалог — на проверку.
"""
from __future__ import annotations

from telethon import errors as e

TABLE = (
    ((e.UserPrivacyRestrictedError, e.UserNotMutualContactError), "recipient",
     "Получатель запретил писать ему незнакомцам"),
    ((e.UserIsBlockedError, e.YouBlockedUserError), "recipient", "Получатель заблокировал аккаунт (или аккаунт — его)"),
    ((e.InputUserDeactivatedError,), "recipient", "Аккаунт получателя удалён"),
    ((e.PeerIdInvalidError, e.ChatWriteForbiddenError), "recipient", "Писать этому получателю нельзя"),
    ((e.MessageTooLongError,), "content", "Сообщение длиннее, чем разрешает Telegram (4096 символов)"),
    ((e.PeerFloodError, e.UserBannedInChannelError, e.UserRestrictedError), "limited",
     "Telegram ограничил аккаунт: слишком много сообщений незнакомцам (спам-блок)"),
    ((e.AuthKeyUnregisteredError, e.SessionRevokedError, e.SessionExpiredError, e.AuthKeyDuplicatedError),
     "dead", "Сессия аккаунта больше не действует — переподключите аккаунт"),
    ((e.UserDeactivatedBanError, e.UserDeactivatedError), "dead", "Аккаунт заблокирован или удалён Telegram"),
)


def classify(exc: BaseException) -> tuple[str | None, str]:
    for types, kind, text in TABLE:
        if isinstance(exc, types):
            return kind, text
    name = type(exc).__name__
    if name in ("PrivacyPremiumRequiredError", "AllowPaymentRequiredError", "PaymentRequiredError"):
        return "recipient", "Получатель принимает сообщения только от Premium или за звёзды"
    return None, f"{name}: результат не подтверждён"


class AccountProblem(Exception):
    """Аккаунт нельзя использовать сейчас (limited) или совсем (dead)."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind, self.text = kind, text
