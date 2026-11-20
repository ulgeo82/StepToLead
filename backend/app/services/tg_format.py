"""Разметка ответа из «Входящих» → текст + сущности Telegram (как в самом Telegram Desktop).

**жирный**  __курсив__  ++подчёркнутый++  ~~зачёркнутый~~  ||спойлер||  `код`  ```блок кода```
[текст](https://ссылка)  строки, начинающиеся с «> », — цитата.
Вложенность не поддерживается (как и в быстрой разметке Telegram). Смещения — в UTF-16, как требует Telegram.
"""
from __future__ import annotations

import re

from telethon.tl import types

INLINE = re.compile(
    r"```(?:[a-zA-Z0-9_+-]*\n)?(?P<pre>[\s\S]+?)```"
    r"|`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<b>[^*\n][\s\S]*?)\*\*"
    r"|__(?P<i>[^_\n][\s\S]*?)__"
    r"|\+\+(?P<u>[^+\n][\s\S]*?)\+\+"
    r"|~~(?P<s>[^~\n][\s\S]*?)~~"
    r"|\|\|(?P<sp>[^|\n][\s\S]*?)\|\|"
    r"|\[(?P<lt>[^\]\n]+)\]\((?P<url>https?://[^\s)]+)\)")
KINDS = {"b": types.MessageEntityBold, "i": types.MessageEntityItalic, "u": types.MessageEntityUnderline,
         "s": types.MessageEntityStrike, "sp": types.MessageEntitySpoiler, "code": types.MessageEntityCode}


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse(source: str) -> tuple[str, list]:
    out, entities, pos = [], [], 0

    def emit(text: str) -> int:
        offset = u16("".join(out))
        out.append(text)
        return offset

    for m in INLINE.finditer(source):
        emit(source[pos:m.start()])
        kind = next(k for k, v in m.groupdict().items() if v is not None and k != "url")
        inner = m.group(kind)
        offset = emit(inner)
        length = u16(inner)
        if kind == "pre":
            entities.append(types.MessageEntityPre(offset, length, language=""))
        elif kind == "lt":
            entities.append(types.MessageEntityTextUrl(offset, length, url=m.group("url")))
        else:
            entities.append(KINDS[kind](offset, length))
        pos = m.end()
    emit(source[pos:])
    text = "".join(out)
    # Цитаты: подряд идущие строки «> …» — одна цитата; сам «> » из текста убираем.
    lines, result, quotes, i = text.split("\n"), [], [], 0
    shift = []   # (позиция в исходном тексте UTF-16, на сколько сдвинуть сущности после неё)
    cur = 0
    for line in lines:
        if line.startswith("> "):
            shift.append((cur, 2))
            line_out = line[2:]
            start = u16("\n".join(result) + ("\n" if result else ""))
            result.append(line_out)
            quotes.append((start, u16(line_out)))
        else:
            result.append(line)
        cur += u16(line) + 1
    if shift:
        def moved(off: int) -> int:
            return off - sum(n for at, n in shift if at < off or at == off)
        for e in entities:
            end = e.offset + e.length
            e.offset, e.length = moved(e.offset), max(0, moved(end) - moved(e.offset))
    text = "\n".join(result)
    merged = []
    for start, length in quotes:   # соседние строки цитаты — одна сущность
        if merged and merged[-1][0] + merged[-1][1] + 1 == start:
            merged[-1] = (merged[-1][0], merged[-1][1] + 1 + length)
        else:
            merged.append((start, length))
    entities += [types.MessageEntityBlockquote(s, n) for s, n in merged if n]
    return text, entities
