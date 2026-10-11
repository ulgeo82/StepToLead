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
    now = datetime.now(timezone.utc)
    proxies = (await db.scalars(select(Proxy).where(Proxy.pool == "client"))).all()
    for proxy in proxies:
        checked = proxy.last_checked_at
        if checked and checked.replace(tzinfo=timezone.utc) > now - timedelta(minutes=5):
            continue
        try:
            await asyncio.wait_for(check(proxy), 12)
            proxy.status, proxy.last_error = "working", None
        except Exception:
            proxy.status, proxy.last_error = "error", "Нет связи через прокси"
        proxy.last_checked_at = now
        channels = (await db.scalars(select(MessagingChannel).where(
            MessagingChannel.kind == "telegram_user"))).all()
        for channel in channels:
            if (channel.config or {}).get("proxy_id") != proxy.id:
                continue
            if proxy.status == "error" and channel.status == "connected":
                channel.status, channel.last_error = "error", proxy.last_error
            elif proxy.status == "working" and channel.last_error == "Нет связи через прокси":
                channel.status, channel.last_error = "connected", None
    await db.commit()
