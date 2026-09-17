from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass
class ParsedProxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


ALLOWED_SCHEMES = {"socks5", "socks4", "http"}


def parse_proxy_line(line: str) -> ParsedProxy:
    value = line.strip()
    if not value:
        raise ValueError("пустая строка")
    if "://" in value:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES or not parsed.hostname or not parsed.port:
            raise ValueError("некорректный URL прокси")
        return ParsedProxy(scheme, parsed.hostname, parsed.port, unquote(parsed.username) if parsed.username else None, unquote(parsed.password) if parsed.password else None)

    parts = value.split(":")
    if len(parts) not in {2, 4}:
        raise ValueError("ожидается host:port или host:port:user:password")
    try:
        port = int(parts[1])
    except ValueError as exc:
        raise ValueError("порт должен быть числом") from exc
    if not 1 <= port <= 65535 or not parts[0]:
        raise ValueError("некорректный host или port")
    return ParsedProxy("socks5", parts[0], port, parts[2] if len(parts) == 4 else None, parts[3] if len(parts) == 4 else None)

