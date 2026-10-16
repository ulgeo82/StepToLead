"""Elapsed business time. Naive stored timestamps are interpreted as UTC, as in reports."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def validate_schedule(schedule):
    ZoneInfo(schedule["timezone"])
    days = schedule["days"]
    if not isinstance(days, list) or any(type(d) is not int or d not in range(1, 8) for d in days):
        raise ValueError("Дни недели — числа от 1 до 7")
    for item in (schedule["start"], schedule["end"]):
        if not isinstance(item, str) or len(item) != 5 or time.fromisoformat(item).strftime("%H:%M") != item:
            raise ValueError("Укажите время в формате ЧЧ:ММ")
    if schedule["start"] >= schedule["end"]:
        raise ValueError("Конец рабочего дня должен быть позже начала")
    return schedule


def business_minutes(start: datetime, end: datetime, schedule: dict | None) -> float:
    first = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
    last = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
    if last <= first:
        return 0.0
    if schedule is None:
        return (last - first).total_seconds() / 60
    validate_schedule(schedule)
    zone = ZoneInfo(schedule["timezone"])
    current, final = first.astimezone(zone).date(), last.astimezone(zone).date()
    opening, closing = time.fromisoformat(schedule["start"]), time.fromisoformat(schedule["end"])
    seconds = 0.0
    while current <= final:
        if current.isoweekday() in schedule["days"]:
            left = datetime.combine(current, opening, zone).astimezone(timezone.utc)
            right = datetime.combine(current, closing, zone).astimezone(timezone.utc)
            seconds += max(0.0, (min(last, right) - max(first, left)).total_seconds())
        current += timedelta(days=1)
    return seconds / 60
