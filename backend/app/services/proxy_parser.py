"""Разбор строки прокси. Поддерживаются форматы, в которых прокси продают магазины:

    scheme://login:pass@host:port       host:port:login:pass       login:pass@host:port
    host:port@login:pass                host:port                  login:pass:host:port (если host — IP/домен)

Схема без указания — `default_scheme` (по умолчанию socks5). Для пачки с типом «Авто» сервер сам пробует
SOCKS5, потом HTTP (services/proxy_health.py), а здесь только помечает, что схема не была указана.
"""
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass
class ParsedProxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    explicit_scheme: bool = True    # схема была в строке (socks5://…, http://…)


ALLOWED_SCHEMES = {"socks5", "socks4", "http", "https"}
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def _is_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOST.match(value)) and ("." in value or value == "localhost")


def _port(value: str) -> int | None:
    return int(value) if value.isdigit() and 1 <= int(value) <= 65535 else None


def skip_line(line: str) -> bool:
    """Пустые строки и комментарии (# …) пропускаем без ошибки."""
    value = line.strip()
    return not value or value.startswith("#")


def parse_proxy_line(line: str, default_scheme: str = "socks5") -> ParsedProxy:
    value = line.strip()
    if not value:
        raise ValueError("пустая строка")
    if "://" in value:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port
        except ValueError:
            port = None
        if scheme not in ALLOWED_SCHEMES or not parsed.hostname or not port:
            raise ValueError("некорректный URL прокси")
        return ParsedProxy("http" if scheme == "https" else scheme, parsed.hostname, port,
                           unquote(parsed.username) if parsed.username else None,
                           unquote(parsed.password) if parsed.password else None)

    scheme = default_scheme if default_scheme in ALLOWED_SCHEMES else "socks5"
    if "@" in value:
        left, right = value.rsplit("@", 1)
        a, b = left.split(":", 1) if ":" in left else (left, "")
        c, d = right.split(":", 1) if ":" in right else (right, "")
        if _is_host(c) and _port(d):                 # login:pass@host:port
            return ParsedProxy(scheme, c, _port(d), a or None, b or None, False)
        if _is_host(a) and _port(b):                 # host:port@login:pass
            return ParsedProxy(scheme, a, _port(b), c or None, d or None, False)
        raise ValueError("ожидается login:pass@host:port или host:port@login:pass")

    parts = value.split(":")
    if len(parts) == 2:
        if not _is_host(parts[0]) or not _port(parts[1]):
            raise ValueError("некорректный host или port")
        return ParsedProxy(scheme, parts[0], _port(parts[1]), explicit_scheme=False)
    if len(parts) == 4:
        if _is_host(parts[0]) and _port(parts[1]):   # host:port:login:pass
            return ParsedProxy(scheme, parts[0], _port(parts[1]), parts[2] or None, parts[3] or None, False)
        if _is_host(parts[2]) and _port(parts[3]):   # login:pass:host:port
            return ParsedProxy(scheme, parts[2], _port(parts[3]), parts[0] or None, parts[1] or None, False)
        raise ValueError("некорректный host или port")
    raise ValueError("ожидается host:port, host:port:login:pass, login:pass@host:port или scheme://…")
