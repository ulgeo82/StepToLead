"""Read only public channel previews. All channel addresses are normalized before HTTP."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .base import Adapter, Context, Vacancy, has_word, looks_remote, parse_dt, short_error

CLOSED = "закрытый канал, читать нельзя"
CITIES = ["Москва", "Санкт-Петербург", "Самара", "Казань", "Тольятти", "Нижний Новгород", "Уфа",
          "Екатеринбург", "Новосибирск", "Краснодар", "Ростов-на-Дону", "Воронеж", "Пермь", "Омск",
          "Красноярск", "Челябинск", "Волгоград", "Саратов", "Санкт Петербург"]


def normalize_channel(text: str) -> tuple[str | None, str | None]:
    text = (text or "").strip()
    if text.startswith("@"):
        text = text[1:]
    elif "/" in text or ":" in text:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"t.me", "www.t.me"}:
            return None, "не похоже на канал"
        parts = parsed.path.strip("/").split("/")
        if parts[0].startswith("+") or parts[0].lower() == "joinchat":
            return None, CLOSED
        if parts[0] == "s":
            parts = parts[1:]
        if len(parts) != 1:
            return None, "не похоже на канал"
        text = parts[0]
    if text.startswith("+") or text.lower() == "joinchat":
        return None, CLOSED
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", text):
        return None, "не похоже на канал"
    return text.lower(), None


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag == "br":
            return "\n"
        value = "".join(c.text() if isinstance(c, Node) else c for c in self.children)
        return value + ("\n" if self.tag in {"p", "div", "li"} else "")

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class ChannelHTML(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


def message_date(node: Node) -> datetime | None:
    """Prefer the post permalink date; video duration is also rendered as <time>."""
    date_links = [n for n in node.walk() if n.tag == "a" and n.has_class("tgme_widget_message_date")]
    preferred = [n for link in date_links for n in link.walk()]
    for candidate in [*preferred, *node.walk()]:
        if candidate.tag != "time" or candidate.has_class("message_video_duration"):
            continue
        value = candidate.attrs.get("datetime")
        if value:
            published = parse_dt(value)
            if published is not None:
                return published
    return None


def parse_channel(content: str, channel: str) -> tuple[str | None, list[Vacancy]]:
    root = ChannelHTML(content).root
    title_node = next((n for n in root.walk() if n.has_class("tgme_channel_info_header_title")), None)
    title = title_node.text().strip() if title_node else None
    result = {}
    for node in root.walk():
        if not node.has_class("tgme_widget_message"):
            continue
        match = re.fullmatch(re.escape(channel) + r"/(\d+)", node.attrs.get("data-post", ""), re.I)
        if not match:
            continue
        text_node = next((n for n in node.walk() if n.has_class("tgme_widget_message_text")), None)
        text = text_node.text().strip() if text_node else ""
        if not text:
            continue
        published = message_date(node)
        post_id = int(match[1])
        result[post_id] = Vacancy(
            source="telegram", ext_id=f"{channel}/{post_id}", title=text.splitlines()[0][:300],
            company="", city=has_word(text, CITIES) or "", snippet=text[:300],
            salary_known=False, remote=looks_remote(text),
            url=f"https://t.me/{channel}/{post_id}", published=published,
            extra={"channel": channel, "text": text},
        )
    return title, [result[key] for key in sorted(result)]


async def read_channel(channel: str, ctx: Context):
    key = f"telegram:{channel}"
    if key not in ctx.cache:
        try:
            async with ctx.http(use_proxy=True) as http:
                response = await http.get(f"https://t.me/s/{channel}")
                response.raise_for_status()
            if not response.text.strip():
                ctx.cache[key] = (None, [], "пустой ответ канала")
            else:
                title, items = parse_channel(response.text, channel)
                ctx.cache[key] = (title, items, None if items else "публичные посты недоступны")
        except (httpx.HTTPError, ValueError) as error:
            ctx.cache[key] = (None, [], short_error(error))
    return ctx.cache[key]


async def check_channel(name: str, ctx: Context) -> dict:
    channel, error = normalize_channel(name)
    if error:
        return {"ok": False, "status": "closed" if error == CLOSED else "unavailable",
                "message": error, "title": None, "preview": []}
    title, items, error = await read_channel(channel, ctx)
    return {"ok": error is None, "status": "unavailable" if error else "ok",
            "message": error or "канал доступен", "title": title, "preview": items[-3:]}


async def enrich(vacancy: Vacancy, ctx: Context):
    from dataclasses import replace
    vacancy = replace(vacancy, extra=dict(vacancy.extra))
    if ctx.ai_extract is None:
        return vacancy
    try:
        extracted = await ctx.ai_extract(vacancy.extra["text"])
    except Exception:
        return vacancy  # A provider outage does not lose posts or stop other channels.
    if not isinstance(extracted, dict):
        return vacancy
    for key in ("title", "company", "city"):
        if isinstance(extracted.get(key), str) and extracted[key].strip():
            setattr(vacancy, key, extracted[key].strip()[:300])
    if isinstance(extracted.get("remote"), bool):
        vacancy.remote = extracted["remote"]
    for key in ("salary_from", "salary_to"):
        number = extracted.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and 0 < number < 1_000_000_000:
            setattr(vacancy, key, int(number))
    vacancy.salary_known = vacancy.salary_from is not None or vacancy.salary_to is not None
    return vacancy


class TelegramChannelsAdapter(Adapter):
    code = "telegram"
    title = "Telegram-каналы"
    icon = "✈️"
    purpose = "Вакансии из публичных каналов"
    settings_schema = [{"key": "proxy_id", "label": "Прокси для Telegram", "type": "proxy", "default": None}]
    filter_schema = [{"key": "channels", "label": "Каналы", "type": "list", "default": []}]

    unit_seconds = 4.0   # страница канала через прокси + пауза 2,5 с

    def units(self, f, filter_settings, settings) -> int:
        return max(1, len((filter_settings or {}).get("channels") or []))

    async def healthcheck(self, settings, ctx):
        names = list(ctx.channel_state)
        # A source check has no filter/channel state. Probe Telegram's own public channel;
        # it is never returned by fetch or imported as a vacancy.
        result = await check_channel(names[0] if names else "telegram", ctx)
        message = "Telegram отвечает; каналы проверяются отдельно" if result["ok"] and not names else result["message"]
        return result["ok"], message

    async def fetch(self, f, since, settings, filter_settings, ctx: Context):
        result = []
        seen = set()
        channels = filter_settings.get("channels") or []
        for name in channels[:30]:
            channel, error = normalize_channel(name)
            if error:
                ctx.channel_result[str(name)] = {"status": "closed" if error == CLOSED else "unavailable",
                    "last_post_id": None, "last_post_at": None, "found": 0, "error": error}
                continue
            if channel in seen:
                continue
            if seen:
                await ctx.sleep(2.5)
            seen.add(channel)
            title, items, error = await read_channel(channel, ctx)
            previous = ctx.channel_state.get(channel)
            cutoff = datetime.now(timezone.utc) - timedelta(days=ctx.first_run_days)
            fresh = [v for v in items if int(v.ext_id.rsplit("/", 1)[1]) > previous] if previous is not None else [
                v for v in items if v.published and v.published >= cutoff]
            newest = items[-1] if items else None
            last_id = int(newest.ext_id.rsplit("/", 1)[1]) if newest else previous
            ctx.channel_result[channel] = {
                "status": "unavailable" if error else "ok", "last_post_id": max(previous or 0, last_id or 0) or None,
                "last_post_at": newest.published.isoformat() if newest and newest.published else None,
                "found": len(fresh), "error": error,
            }
            for item in fresh:
                text = item.extra["text"]
                if f.get("keywords") and not has_word(text, f["keywords"]):
                    continue
                if has_word(text, f.get("exclude") or []):
                    continue
                vacancy = await enrich(item, ctx)
                if f.get("remote_only") and not vacancy.remote:
                    continue
                if not vacancy.city:
                    vacancy.city = has_word(text, f.get("regions") or []) or ""
                if f.get("regions") and (f.get("strict_city") or not vacancy.remote) and not has_word(
                        vacancy.city, f["regions"]):
                    continue
                result.append(vacancy)
        return result


ADAPTER = TelegramChannelsAdapter()
