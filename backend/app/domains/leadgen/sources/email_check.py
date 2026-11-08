"""Email checks via DNS only. No SMTP connections and no delivery/bounce mutations."""
import asyncio
import re
from datetime import timedelta

import dns.exception
import dns.resolver

from app.domains.leadgen import service
from .base import CompanySource, Enrichment

DISPOSABLE = {"mailinator.com", "yopmail.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
              "trashmail.com", "getnada.com", "dispostable.com", "temp-mail.org"}
TYPOS = {"gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmal.com": "gmail.com",
         "yandex.rru": "yandex.ru", "yandx.ru": "yandex.ru", "mail.ry": "mail.ru", "mail.r": "mail.ru"}
FREE = {"gmail.com", "mail.ru", "yandex.ru", "ya.ru", "bk.ru", "list.ru", "inbox.ru", "rambler.ru",
        "outlook.com", "hotmail.com", "icloud.com"}
resolver_factory = dns.resolver.Resolver   # tests replace this; never resolve real names in tests


def dns_mail(domain):
    resolver = resolver_factory()
    resolver.lifetime = 5
    for kind in ("MX", "A"):
        try:
            answer = resolver.resolve(domain, kind)
            if kind == "MX" and any(str(getattr(item, "exchange", "")).strip() == "." for item in answer):
                return False  # RFC 7505 null MX explicitly refuses mail; do not fall back to A.
            if answer:
                return True
        except dns.resolver.NXDOMAIN:
            return False
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.DNSException:
            return None
    return False


async def check_email(value, ctx):
    email = (value or "").strip().lower()
    if "@" in email:
        local, host = email.rsplit("@", 1)
        try:
            email = local + "@" + host.encode("idna").decode("ascii")
        except UnicodeError:
            return "invalid", {"reason": "неверный домен"}
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                                          r"@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", email):
        return "invalid", {"reason": "неверный синтаксис"}
    local, host = email.rsplit("@", 1)
    if len(local) > 64:
        return "invalid", {"reason": "неверный синтаксис"}
    if host in DISPOSABLE:
        return "invalid", {"reason": "одноразовый домен"}
    if host in TYPOS:
        return "risky", {"reason": "возможная опечатка", "suggestion": f"{local}@{TYPOS[host]}"}
    cache = ctx.cache.setdefault("email_dns", {})
    if host not in cache:
        cache[host] = await asyncio.to_thread(dns_mail, host)
    if cache[host] is False:
        return "invalid", {"reason": "у домена нет почты"}
    if cache[host] is None:
        return "risky", {"reason": "DNS временно недоступен"}
    if host in FREE:
        return "risky", {"reason": "общий почтовый сервис вместо корпоративного домена"}
    return "valid", {"reason": "синтаксис и DNS в порядке"}


class EmailCheckSource(CompanySource):
    code = "email_check"
    title = "Проверка почт"
    icon = "✉️"
    purpose = "Синтаксис, DNS, одноразовые домены и опечатки — без SMTP-проб"
    requires_site = False
    chain_default = True
    unit_seconds = 3
    concurrency = 4
    settings_schema = [{"key": "force", "type": "bool", "label": "Перепроверить почты за последние 30 дней",
                        "default": False}]

    async def enrich(self, company, settings, ctx):
        now = service.utcnow()
        counters = {"valid": 0, "invalid": 0, "risky": 0}
        updates, details = [], []
        for contact in company.contacts:
            if contact["kind"] != "email" or contact.get("is_personal"):
                continue
            verified = service._aware(contact.get("verified_at"))
            if verified and now - verified < timedelta(days=30) and not settings.get("force"):
                continue
            status, payload = await check_email(contact["value"], ctx)
            counters[status] += 1
            updates.append({"id": contact["id"], "verify_status": status, "verified_at": now})
            details.append({"contact_id": contact["id"], "status": status, "payload": payload})
        return Enrichment(status="done" if updates else "skipped", error=None if updates else "нет почт для проверки",
                          counters=counters, contact_updates=updates, profile={**counters, "contacts": details})


ADAPTER = EmailCheckSource()
