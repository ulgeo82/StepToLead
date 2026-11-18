"""Один аккаунт — одно соединение с Telegram в момент времени (рассылка, прогрев, ответы, проверки).

Два одновременных подключения одной сессии Telegram видит как подозрительные и может сбросить ключ. Процесс
бэкенда один (uvicorn без воркеров), поэтому хватает множества в памяти.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_held: set[int] = set()


def try_take(account_id: int) -> bool:
    if account_id in _held:
        return False
    _held.add(account_id)
    return True


def release(account_id: int) -> None:
    _held.discard(account_id)


def busy(account_id: int) -> bool:
    return account_id in _held


@asynccontextmanager
async def hold(account_id: int, wait: float = 90.0):
    """Дождаться аккаунта (до wait секунд) и держать его на время работы."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while not try_take(account_id):
        if loop.time() > deadline:
            raise RuntimeError("Аккаунт сейчас занят другой задачей — попробуйте через минуту")
        await asyncio.sleep(0.5)
    try:
        yield
    finally:
        release(account_id)
