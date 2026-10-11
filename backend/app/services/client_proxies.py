"""Sticky SOCKS5 allocation for customer profiles; agency accounts remain separate."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.crypto import decrypt_secret
from app.models.messaging import MessagingChannel
from app.models.proxy import Proxy


async def usage(db):
    counts = {}
    channels = (await db.scalars(select(MessagingChannel).where(
        MessagingChannel.kind == "telegram_user"))).all()
    for channel in channels:
        proxy_id = (channel.config or {}).get("proxy_id")
        if proxy_id:
            counts[proxy_id] = counts.get(proxy_id, 0) + 1
    return counts


async def allocate(db, channel):
    # Lock the proxy rows before counting reservations (serialize competing allocations).
    proxies = (await db.scalars(select(Proxy).where(Proxy.pool == "client")
                               .order_by(Proxy.id).with_for_update())).all()
    current = (channel.config or {}).get("proxy_id")
    if current:
        return await db.get(Proxy, current)  # Never rotate an existing profile, including on failure.
    counts = await usage(db)
    dedicated = [p for p in proxies if p.dedicated_workspace_id == channel.workspace_id]
    candidates = dedicated or [p for p in proxies if p.dedicated_workspace_id is None]
    candidates = [p for p in candidates if p.scheme == "socks5" and p.status in {"working", "online", "ok"}
                  and counts.get(p.id, 0) < p.max_accounts]
    if not candidates:
        channel.status = "waiting_proxy"
        channel.last_error = "Нет свободных прокси для Telegram-профилей"
        return None
    proxy = min(candidates, key=lambda p: (counts.get(p.id, 0), p.id))
    channel.config = {**(channel.config or {}), "proxy_id": proxy.id}
    await db.flush()
    return proxy


async def check(proxy):
    from python_socks import ProxyType
    from python_socks.async_.asyncio import Proxy as SocksProxy
    transport = SocksProxy(proxy_type=ProxyType.SOCKS5, host=proxy.host, port=proxy.port,
                           username=proxy.username, password=decrypt_secret(proxy.password_encrypted), rdns=True)
    socket = await transport.connect(dest_host="149.154.167.50", dest_port=443, timeout=10)
    socket.close()


async def check_due(db):
    """Health-check due proxies concurrently, so a slow proxy never stalls message sending in the leader loop."""
    now = datetime.now(timezone.utc)
    proxies = (await db.scalars(select(Proxy).where(Proxy.pool == "client"))).all()
    due = [p for p in proxies if not (p.last_checked_at and p.last_checked_at.replace(tzinfo=timezone.utc)
                                     > now - timedelta(minutes=5))]
    if not due:
        return
    results = await asyncio.gather(*(asyncio.wait_for(check(p), 12) for p in due), return_exceptions=True)
    channels = (await db.scalars(select(MessagingChannel).where(MessagingChannel.kind == "telegram_user"))).all()
    for proxy, result in zip(due, results):
        if isinstance(result, BaseException):
            proxy.status, proxy.last_error = "error", "Нет связи через прокси"
        else:
            proxy.status, proxy.last_error = "working", None
        proxy.last_checked_at = now
        for channel in channels:
            if (channel.config or {}).get("proxy_id") != proxy.id:
                continue
            if proxy.status == "error" and channel.status == "connected":
                channel.status, channel.last_error = "error", proxy.last_error
            elif proxy.status == "working" and channel.last_error == "Нет связи через прокси":
                channel.status, channel.last_error = "connected", None
    await db.commit()


class ProxyInputError(ValueError):
    """Shown to the agency as is."""


async def checked_from_line(line: str):
    """Parse «host:port:login:password» (or socks5://…), require SOCKS5 and check it reaches Telegram right now."""
    from app.services.proxy_parser import parse_proxy_line
    try:
        item = parse_proxy_line(line)
    except ValueError as error:
        raise ProxyInputError(f"Не удалось разобрать прокси: {error}") from None
    if item.scheme != "socks5":
        raise ProxyInputError("Для Telegram-профилей нужен SOCKS5-прокси")
    from app.core.crypto import encrypt_secret
    probe = Proxy(scheme="socks5", host=item.host, port=item.port, username=item.username,
                  password_encrypted=encrypt_secret(item.password) if item.password else None)
    try:
        await asyncio.wait_for(check(probe), 12)
    except Exception:
        raise ProxyInputError("Прокси не пропускает к Telegram: проверьте адрес, порт, логин и пароль") from None
    return probe


async def add_for_workspace(db, workspace_id: int, line: str, max_accounts: int = 3) -> Proxy:
    probe = await checked_from_line(line)
    duplicate = await db.scalar(select(Proxy).where(Proxy.host == probe.host, Proxy.port == probe.port,
                                                    Proxy.username == probe.username))
    if duplicate:
        raise ProxyInputError("Этот прокси уже есть в портале")
    proxy = Proxy(scheme="socks5", host=probe.host, port=probe.port, username=probe.username,
                  password_encrypted=probe.password_encrypted, pool="client", max_accounts=max_accounts,
                  dedicated_workspace_id=workspace_id, status="working",
                  last_checked_at=datetime.now(timezone.utc))
    db.add(proxy)
    await db.flush()
    return proxy


async def replace(db, proxy: Proxy, line: str) -> int:
    """Swap the address of a proxy in place: its profiles keep their binding and reconnect through the new IP."""
    probe = await checked_from_line(line)
    duplicate = await db.scalar(select(Proxy).where(Proxy.id != proxy.id, Proxy.host == probe.host,
                                                    Proxy.port == probe.port, Proxy.username == probe.username))
    if duplicate:
        raise ProxyInputError("Этот прокси уже есть в портале")
    proxy.host, proxy.port, proxy.username = probe.host, probe.port, probe.username
    proxy.password_encrypted = probe.password_encrypted
    proxy.status, proxy.last_error, proxy.last_checked_at = "working", None, datetime.now(timezone.utc)
    moved = 0
    for channel in (await db.scalars(select(MessagingChannel).where(MessagingChannel.kind == "telegram_user"))).all():
        if (channel.config or {}).get("proxy_id") == proxy.id:
            moved += 1
            if channel.last_error == "Нет связи через прокси":
                channel.status, channel.last_error = "connected", None
    return moved


def signature(proxy: Proxy | None):
    """What a live connection depends on; the profile manager reconnects when it changes."""
    if proxy is None:
        return None
    return (proxy.id, proxy.host, proxy.port, proxy.username, proxy.password_encrypted)
