"""Проверка: каким параметром XMLStock Live отдаёт вторую страницу органики. Ключей не печатает.

Запуск на сервере (там заданы XMLSTOCK_*):
  docker compose --env-file .env.prod -f compose.prod.yml exec backend python scripts/xmlstock_page_check.py
Делает 3–5 запросов (≈ 0,1 ₽). Печатает по каждому кандидату: сколько доменов, сколько совпало с первой страницей.
Верный параметр — тот, где доменов ~10 и совпадений с первой страницей почти нет.
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.domains.leadgen.core.normalize import normalize_domain  # noqa: E402
from app.domains.leadgen.providers import XmlStockLiveProvider, parse_xmlstock_page  # noqa: E402

QUERY, LR = "кухни на заказ", 51


async def main():
    p = XmlStockLiveProvider()
    first = parse_xmlstock_page(await p.fetch_raw(QUERY, LR), QUERY)
    base = {normalize_domain(o.url) for o in first.organic}
    print(f"страница 1: доменов {len(base)}")
    for name in ("page", "p", "pages"):
        p.extra_params = {name: 1}
        try:
            page = parse_xmlstock_page(await p.fetch_raw(QUERY, LR), QUERY)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}=1: ошибка {type(exc).__name__}: {str(exc)[:80]}")
            continue
        got = {normalize_domain(o.url) for o in page.organic}
        print(f"{name}=1: доменов {len(got)}, совпало с первой страницей {len(got & base)}")


asyncio.run(main())
