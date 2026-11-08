"""Прогресс долгих операций для экрана: шаги с оценкой времени, ход внутри шага, проценты и сколько осталось.

Универсально: радар, поиск лидов, импорт — любая операция из шагов. Шаги одной группы идут одновременно
(например, площадки радара): группа длится столько, сколько самый долгий из них. Хранится в памяти процесса
(бэкенд — один процесс uvicorn); законченная операция видна ещё 10 минут, потом удаляется.

    t = progress.start("jobradar:run:5", "Проверка", [("hh", "hh.ru", 12, "sources"), ("match", "Отбор", 2)])
    t.begin("hh", total=3); t.tick("hh"); …; t.end("hh", note="найдено 40")
    t.finish("done", "Готово")      # или t.finish("error", "текст")
    progress.get("jobradar:run:5")  # -> снимок для экрана
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

KEEP_SECONDS = 600
CAP = 0.95          # шаг не доходит до 100% сам — только по факту окончания
CLOSED = ("done", "skipped", "error")


@dataclass
class Step:
    code: str
    name: str
    estimate: float
    group: str | None = None
    state: str = "pending"          # pending | running | done | skipped | error
    started: float | None = None
    finished: float | None = None
    last_tick: float | None = None
    done: int = 0                   # отметки «начал следующую единицу работы» (запрос, находку)
    total: int = 0
    note: str = ""

    def _per_item(self) -> float:
        """Секунд на единицу: по факту, когда отметок хотя бы две, иначе — из оценки."""
        if self.done >= 2 and self.started and self.last_tick and self.last_tick > self.started:
            return (self.last_tick - self.started) / (self.done - 1)
        return self.estimate / max(1, self.total)

    def fraction(self, now: float) -> float:
        if self.state in CLOSED:
            return 1.0
        if self.state != "running":
            return 0.0
        if self.total:
            partial = min(0.9, (now - (self.last_tick or self.started or now)) / max(0.3, self._per_item()))
            return min(CAP, (max(self.done, 1) - 1 + partial) / self.total)
        return min(CAP, (now - (self.started or now)) / max(self.estimate, 0.5))

    def left(self, now: float) -> float:
        """Сколько ещё займёт шаг. Идущий — по реальной скорости, если она уже видна."""
        if self.state in CLOSED:
            return 0.0
        if self.state == "pending":
            return self.estimate
        if self.total and self.done >= 2:
            remaining_items = max(0.0, self.total - self.done + 1 - min(0.9, (now - (self.last_tick or now))
                                                                       / max(0.3, self._per_item())))
            return remaining_items * self._per_item()
        overdue = now - (self.started or now) > self.estimate
        return max(2.0, self.estimate * 0.2) if overdue else self.estimate * (1 - self.fraction(now))


@dataclass
class Tracker:
    key: str
    title: str
    steps: list[Step]
    state: str = "queued"           # queued | running | done | error
    message: str = ""
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    started_wall: float = field(default_factory=time.time)

    def _find(self, code: str) -> Step | None:
        return next((s for s in self.steps if s.code == code), None)

    def run(self) -> None:
        """Из очереди — в работу (ждали, пока закончится другая операция)."""
        if self.state == "queued":
            self.state, self.started = "running", time.monotonic()

    def begin(self, code: str, *, total: int = 0, note: str = "") -> None:
        self.run()
        s = self._find(code)
        if s is None:
            s = Step(code, code, 1.0)
            self.steps.append(s)
        s.state, s.started, s.last_tick, s.total, s.done, s.note = "running", time.monotonic(), None, total, 0, note

    def tick(self, code: str, n: int = 1) -> None:
        s = self._find(code)
        if s is None or s.state != "running":
            return
        s.done += n
        s.last_tick = time.monotonic()
        if s.total and s.done > s.total:
            s.total = s.done   # страниц оказалось больше, чем думали

    def estimate(self, code: str, seconds: float, *, total: int | None = None) -> None:
        """Уточнить оценку шага, когда стало известно, сколько там работы (например, сколько находок)."""
        s = self._find(code)
        if s is None:
            return
        s.estimate = max(0.5, seconds)
        if total is not None:
            s.total = total

    def end(self, code: str, state: str = "done", note: str = "") -> None:
        s = self._find(code)
        if s is None:   # шага не было в плане (например, ИИ выключен) — нечего закрывать
            return
        s.state, s.finished = state, time.monotonic()
        if note:
            s.note = note

    def finish(self, state: str = "done", message: str = "") -> None:
        now = time.monotonic()
        for s in self.steps:
            if s.state in ("pending", "running"):
                s.state, s.finished = ("skipped" if s.state == "pending" else state), now
        self.state, self.message, self.finished = state, message, now

    def _units(self) -> list[list[Step]]:
        """Шаги, сложенные в последовательные блоки: группа (идут одновременно) — один блок."""
        out: list[list[Step]] = []
        index: dict[str, int] = {}
        for s in self.steps:
            if s.group and s.group in index:
                out[index[s.group]].append(s)
            else:
                if s.group:
                    index[s.group] = len(out)
                out.append([s])
        return out

    def snapshot(self) -> dict:
        now = time.monotonic()
        weight = done = left = 0.0
        for unit in self._units():
            w = max(s.estimate for s in unit)
            work = sum(s.estimate for s in unit) or 1.0
            weight += w
            done += w * sum(s.estimate * s.fraction(now) for s in unit) / work
            left += max(s.left(now) for s in unit)
        percent = 100 if self.state in ("done", "error") else 0 if self.state == "queued" \
            else min(99, int(100 * done / (weight or 1.0)))
        running = [s for s in self.steps if s.state == "running"]
        detail = " · ".join(s.name + (f": {min(max(s.done, 1), s.total)} из {s.total}" if s.total else "")
                            + (f" ({s.note})" if s.note else "") for s in running)
        return {
            "key": self.key, "title": self.title, "state": self.state, "message": self.message, "percent": percent,
            "elapsed": round((self.finished or now) - (self.started or self.created), 1),
            "waited": round((self.started or now) - self.created, 1),   # сколько ждали очереди
            "remaining": 0 if self.state in ("done", "error") else round(left),
            "estimate": round(weight), "current": detail, "started_at": self.started_wall,
            "steps": [{"code": s.code, "name": s.name, "state": s.state, "estimate": round(s.estimate, 1),
                       "parallel": bool(s.group),
                       "seconds": round((s.finished or now) - s.started, 1) if s.started else None,
                       "done": s.done, "total": s.total, "note": s.note} for s in self.steps],
        }


_items: dict[str, Tracker] = {}


def _cleanup() -> None:
    now = time.monotonic()
    for key in [k for k, t in _items.items() if t.finished and now - t.finished > KEEP_SECONDS]:
        _items.pop(key, None)


def start(key: str, title: str, steps: list[tuple], *, queued: bool = False) -> Tracker:
    """steps: (code, name, секунд) или (code, name, секунд, группа) — шаги одной группы идут одновременно."""
    _cleanup()
    t = Tracker(key, title, [Step(s[0], s[1], max(0.5, float(s[2])), s[3] if len(s) > 3 else None) for s in steps])
    if not queued:
        t.run()
    _items[key] = t
    return t


def get(key: str) -> dict | None:
    _cleanup()
    t = _items.get(key)
    return t.snapshot() if t else None


def tracker(key: str) -> Tracker | None:
    return _items.get(key)


def active(key: str) -> bool:
    t = _items.get(key)
    return bool(t and t.state in ("queued", "running"))
