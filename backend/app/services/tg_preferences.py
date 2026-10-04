"""Personal Telegram choices only narrow project notification rules."""
import re
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def bounded_html(text: str) -> str:
    """Never cut an HTML entity/tag in half; long messages fall back to plain HTML."""
    if len(text) <= 3900:
        return text
    plain = html.unescape(re.sub(r"<[^>]*>", "", text))
    return html.escape(plain[:1800]) + "…"


def allowed(user, event: str, tz: str | None = None, at: datetime | None = None, *, check_quiet: bool = True) -> bool:
    state = user.telegram_state or {}
    if not user.active or state.get("notifications", {}).get(event, True) is False:
        return False
    quiet = state.get("quiet_hours") or {}
    if check_quiet and quiet.get("enabled"):
        try:
            local = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz or state.get("project_timezone") or "Europe/Moscow"))
            start, end = int(quiet.get("start", 22)), int(quiet.get("end", 9))
            if not (0 <= start < 24 and 0 <= end < 24):
                return True
            hour = local.hour
            if (start <= hour < end if start < end else hour >= start or hour < end):
                return False
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return False
    return True


def private_text(user, value: str) -> str:
    if (user.telegram_state or {}).get("show_phone", False):
        return value
    def redact(match):
        candidate = match.group(0)
        return "[телефон скрыт]" if len(re.sub(r"\D", "", candidate)) >= 10 else candidate
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{5,}\d(?!\w)", redact, value)


def lead_buttons(inbound_id=None, deal_id=None):
    from app.core.config import settings
    prefix = f"d:{deal_id}" if deal_id else f"i:{inbound_id}"
    if not (deal_id or inbound_id):
        return None
    url = f"{settings.frontend_origin.rstrip('/')}/crm" + (f"?deal={deal_id}" if deal_id else "?tab=inbound")
    return {"inline_keyboard": [
        [{"text": "Взять в работу", "callback_data": f"take:{prefix}"},
         {"text": "Нецелевая", "callback_data": f"quality:{prefix}"}],
        [{"text": "Перезвонить через 1 ч", "callback_data": f"call1:{prefix}"},
         {"text": "Перезвонить завтра", "callback_data": f"calltom:{prefix}"}],
        [{"text": "Открыть в портале", "url": url}]]}


def task_buttons(task_id, deal_id=None):
    from app.core.config import settings
    rows = [[{"text": "Выполнено", "callback_data": f"done:{task_id}"},
             {"text": "Перенести на завтра", "callback_data": f"tomorrow:{task_id}"}]]
    rows.append([{"text": "Открыть в портале", "url": settings.frontend_origin.rstrip('/') +
                  (f"/crm?deal={deal_id}" if deal_id else "/crm")}])
    return {"inline_keyboard": rows}
