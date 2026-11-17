"""Здоровье прокси: доходит ли до Telegram, какой внешний IP видит Telegram, задержка, история проверок за сутки.

Общий для обоих пулов (аутрич агентства и CRM-профили клиентов). Проверка без Telegram-сессий: TCP-соединение
через прокси до дата-центра Telegram + запрос внешнего IP. Пароли прокси не попадают ни в ответы, ни в логи.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.core.crypto import decrypt_secret

log = logging.getLogger(__name__)

TELEGRAM_DC = ("149.154.167.50", 443)
IP_ECHO = ("api.ipify.org", 80)
TIMEOUT = 10
KEEP_SECONDS = 24 * 3600
DEAD_AFTER = 3            # столько неудачных проверок подряд — прокси мёртв
SLOW_MS = 2000
TELEGRAM_SCHEMES = ("socks5", "http")


class ProbeError(RuntimeError):
    pass


def _transport(proxy, scheme: str | None = None):
    from python_socks import ProxyType
    from python_socks.async_.asyncio import Proxy as SocksProxy
    kind = {"http": ProxyType.HTTP, "socks4": ProxyType.SOCKS4}.get(scheme or proxy.scheme, ProxyType.SOCKS5)
    return SocksProxy(proxy_type=kind, host=proxy.host, port=proxy.port, username=proxy.username,
                      password=decrypt_secret(proxy.password_encrypted), rdns=True)


async def _exit_ip(proxy, scheme: str | None) -> str | None:
    try:
        sock = await _transport(proxy, scheme).connect(dest_host=IP_ECHO[0], dest_port=IP_ECHO[1], timeout=TIMEOUT)
    except Exception:  # noqa: BLE001 — внешний IP необязателен: прокси может не пускать на 80-й порт
        return None
    try:
        reader, writer = await asyncio.open_connection(sock=sock)
        writer.write(f"GET /?format=text HTTP/1.1\r\nHost: {IP_ECHO[0]}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), TIMEOUT)
        writer.close()
        body = raw.split(b"\r\n\r\n", 1)[-1].decode(errors="ignore").strip().splitlines()
        value = (body[-1] if body else "").strip()
        return value[:64] if value and all(ch.isalnum() or ch in ".:" for ch in value) else None
    except Exception:  # noqa: BLE001
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        return None


async def probe(proxy, scheme: str | None = None) -> dict:
    """{"latency_ms", "exit_ip"} или ProbeError. scheme — попробовать другой тип (для «Авто»)."""
    started = time.monotonic()
    try:
        sock = await _transport(proxy, scheme).connect(dest_host=TELEGRAM_DC[0], dest_port=TELEGRAM_DC[1],
                                                       timeout=TIMEOUT)
        sock.close()
    except Exception as exc:  # noqa: BLE001
        raise ProbeError(_reason(exc)) from None
    latency = int((time.monotonic() - started) * 1000)
    return {"latency_ms": latency, "exit_ip": await _exit_ip(proxy, scheme)}


def _reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "auth" in text or "407" in text or "credential" in text:
        return "Прокси не принял логин или пароль"
    if "timeout" in text or "timed out" in text:
        return "Прокси не ответил вовремя"
    if "refused" in text:
        return "Прокси отклонил соединение"
    return "Нет связи с Telegram через прокси"


def record(proxy, info: dict | None, error: str | None = None, *, now: float | None = None) -> bool:
    """Записать итог проверки. Возвращает True, если прокси только что стал мёртвым (для уведомления)."""
    now = now or time.time()
    health = dict(proxy.health or {})
    checks = [c for c in health.get("checks") or [] if isinstance(c, list) and len(c) == 2 and c[0] > now - KEEP_SECONDS]
    ok = info is not None
    checks.append([int(now), 1 if ok else 0])
    was_dead = int(health.get("fails_in_row") or 0) >= DEAD_AFTER
    health["fails_in_row"] = 0 if ok else int(health.get("fails_in_row") or 0) + 1
    if ok:
        proxy.latency_ms = info.get("latency_ms")
        ip = info.get("exit_ip")
        if ip:
            ips = [x for x in health.get("ips") or [] if x != ip][-4:]
            health["ips"] = ips + [ip]
            proxy.exit_ip = ip
    health["checks"] = checks[-400:]
    proxy.health = health
    proxy.status, proxy.last_error = ("working", None) if ok else ("error", error or "Нет связи через прокси")
    proxy.last_checked_at = datetime.fromtimestamp(now, timezone.utc)
    return not was_dead and health["fails_in_row"] >= DEAD_AFTER


def view(proxy, duplicate_of: int | None = None) -> dict:
    """Здоровье для экрана: балл 0–100, уровень и причины по-русски."""
    health = proxy.health or {}
    checks = health.get("checks") or []
    fails = int(health.get("fails_in_row") or 0)
    uptime = round(100 * sum(c[1] for c in checks) / len(checks)) if checks else None
    ips = health.get("ips") or []
    rotating = len(set(ips[-3:])) > 1
    reasons: list[str] = []
    if proxy.status == "untested" or not checks:
        return {"score": None, "level": "unknown", "uptime_24h": None, "fails_in_row": 0,
                "reasons": ["Ещё не проверен"], "rotating": False}
    score = uptime if uptime is not None else 0
    if fails >= DEAD_AFTER:
        reasons.append(f"Не отвечает {fails} проверки подряд")
        score = 0
    elif fails:
        reasons.append("Последняя проверка не прошла")
        score -= 15
    if proxy.latency_ms and proxy.latency_ms > SLOW_MS:
        reasons.append(f"Медленный: {proxy.latency_ms} мс")
        score -= 15
    if rotating:
        reasons.append("Внешний IP меняется — для аккаунтов не годится")
        score -= 30
    if duplicate_of:
        reasons.append(f"Тот же внешний IP, что у прокси №{duplicate_of}")
        score -= 20
    if uptime is not None and uptime < 90 and fails < DEAD_AFTER:
        reasons.append(f"Доступен {uptime}% времени за сутки")
    score = max(0, min(100, int(score)))
    level = "dead" if fails >= DEAD_AFTER else "warn" if reasons else "ok"
    return {"score": score, "level": level, "uptime_24h": uptime, "fails_in_row": fails, "reasons": reasons,
            "rotating": rotating}


def duplicates(proxies) -> dict[int, int]:
    """id прокси → id первого прокси с тем же внешним IP."""
    first: dict[str, int] = {}
    out: dict[int, int] = {}
    for p in sorted(proxies, key=lambda x: x.id):
        if not p.exit_ip:
            continue
        if p.exit_ip in first:
            out[p.id] = first[p.exit_ip]
        else:
            first[p.exit_ip] = p.id
    return out


async def check_one(proxy, *, auto_scheme: bool = False) -> bool:
    """Проверить и записать. auto_scheme: схема не была указана — пробуем SOCKS5, потом HTTP и сохраняем рабочую."""
    tried = [proxy.scheme] + ([s for s in TELEGRAM_SCHEMES if s != proxy.scheme] if auto_scheme else [])
    error = None
    for scheme in tried:
        try:
            info = await asyncio.wait_for(probe(proxy, scheme), TIMEOUT * 2 + 5)
        except (ProbeError, asyncio.TimeoutError) as exc:
            error = str(exc) or "Прокси не ответил вовремя"
            continue
        proxy.scheme = scheme
        return record(proxy, info)
    return record(proxy, None, error)
