"""Тех-профиль сайта (enricher): Метрика, GA, коллтрекинг, чат, CRM-формы, платформа, год в подвале, скорость.

Одна страница (главная) + robots.txt; скорость мобильной версии — Google PageSpeed Insights, только если в настройках
задан ключ. Защиту и капчи не обходим: не открылся сайт — пишем причину и идём дальше.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from app.domains.leadgen import site_enrich
from app.domains.leadgen.core.tech_profile import TRACKING_TITLES, profile
from app.domains.leadgen.sources.base import CompanySource, CompanyView, Context, Enrichment, SignalIn, short_error

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


class SiteTechSource(CompanySource):
    code = "site_tech"
    kind = "enricher"
    title = "Тех-профиль сайта"
    icon = "🛠"
    purpose = "Метрика, коллтрекинг, чат, CRM, платформа и свежесть сайта — для сигналов и текста письма"
    needs_key = False
    unit_seconds = 2.5          # главная + robots (+ PageSpeed — добавляется в оценке, см. unit_cost)
    concurrency = 4             # разные сайты — параллельно; к одному сайту — один запрос за раз
    fresh_days = 30
    settings_schema = [
        {"key": "pagespeed_key", "label": "Ключ Google PageSpeed Insights", "type": "secret", "default": None,
         "hint": "Бесплатно в Google Cloud Console → PageSpeed Insights API. Без ключа скорость не проверяем."},
        {"key": "pagespeed", "label": "Проверять скорость мобильной версии", "type": "bool", "default": True},
        {"key": "slow_score", "label": "Медленный сайт: оценка PageSpeed ниже", "type": "number", "default": 50},
        {"key": "outdated_years", "label": "Устаревший сайт: год в подвале старше, лет", "type": "number",
         "default": 2},
    ]
    signals_info = [
        {"kind": "no_analytics", "label": "Нет Метрики на сайте", "weight": 2,
         "hint": "Не видят, откуда заявки — прямо наша боль"},
        {"kind": "no_calltracking", "label": "Нет коллтрекинга", "weight": 1,
         "hint": "Звонки с рекламы не размечены"},
        {"kind": "has_calltracking", "label": "Есть коллтрекинг", "weight": 0,
         "hint": "Продвинутый покупатель — другой текст письма"},
        {"kind": "slow_site", "label": "Медленный сайт на телефоне", "weight": 1, "hint": "PageSpeed ниже порога"},
        {"kind": "outdated_site", "label": "Сайт давно не обновлялся", "weight": 1, "hint": "Год в подвале старше порога"},
    ]
    variables = [   # key — имя переменной в письме {{key}}, field — поле профиля (lg_enrichments.result)
        {"key": "site_platform", "field": "site_platform", "label": "Платформа сайта (Тильда, WordPress…)"},
        {"key": "has_metrika", "field": "has_metrika_text", "label": "Есть Метрика: да / нет"},
        {"key": "calltracking", "field": "calltracking_text", "label": "Коллтрекинг (Calltouch…) или пусто"},
        {"key": "footer_year", "field": "footer_year", "label": "Год в подвале сайта"},
        {"key": "mobile_speed", "field": "mobile_speed", "label": "Скорость на телефоне 0–100"},
    ]

    def unit_cost(self, settings: dict) -> float:
        """Секунд на компанию без опыта: PageSpeed добавляет ~15 с."""
        return self.unit_seconds + (15.0 if self._pagespeed_on(settings) else 0.0)

    @staticmethod
    def _pagespeed_on(settings: dict) -> bool:
        return bool(settings.get("pagespeed", True) and settings.get("pagespeed_key"))

    async def enrich(self, company: CompanyView, settings: dict, ctx: Context) -> Enrichment:
        if not company.domain:
            return Enrichment(status="skipped", error="нет сайта")
        now_year = datetime.now(timezone.utc).year
        html, root, blocked, error = None, "", False, None
        async with ctx.http(**{"User-Agent": site_enrich.USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as http:
            for scheme in ("https", "http"):
                root = f"{scheme}://{company.domain}"
                robots = await site_enrich._robots(http, root)
                if robots is not None and not robots.can_fetch(site_enrich.USER_AGENT, root + "/"):
                    blocked = True
                    break
                html, err = await site_enrich._get_html(http, root + "/")
                if html is not None:
                    break
                error = err
        if blocked:
            return Enrichment(status="skipped", error="robots.txt запрещает обход")
        if html is None:
            return Enrichment(status="failed", error=_site_error(error))

        data = await asyncio.to_thread(profile, html, now_year)
        data["checked_url"] = root + "/"
        signals: list[SignalIn] = []
        if not data["has_metrika"]:   # без Метрики не видят, откуда заявки с Директа (GA этого не заменяет)
            signals.append(SignalIn("no_analytics", {"has_ga": data["has_ga"], "gtm": data["has_gtm"]}))
        if data["calltracking"]:
            signals.append(SignalIn("has_calltracking", {"services": data["calltracking"]}))
        else:
            signals.append(SignalIn("no_calltracking", {}))
        years = int(settings.get("outdated_years") or 2)
        if data["footer_year"] and now_year - data["footer_year"] >= years:
            signals.append(SignalIn("outdated_site", {"year": data["footer_year"]}))

        data["mobile_speed"] = None
        if self._pagespeed_on(settings):
            score, perr = await self._pagespeed(root + "/", settings["pagespeed_key"], ctx)
            data["mobile_speed"], data["pagespeed_error"] = score, perr
            if score is not None and score < int(settings.get("slow_score") or 50):
                signals.append(SignalIn("slow_site", {"score": score}))

        # Переменные письма — готовыми строками.
        data["site_platform"] = data["platform_title"] or ""
        data["has_metrika_text"] = "да" if data["has_metrika"] else "нет"
        data["calltracking_text"] = ", ".join(TRACKING_TITLES.get(c, c) for c in data["calltracking"])
        return Enrichment(status="done", profile=data, signals=signals)

    async def _pagespeed(self, url: str, key: str, ctx: Context) -> tuple[int | None, str | None]:
        params = {"url": url, "strategy": "mobile", "category": "performance", "key": key}
        try:
            async with ctx.http() as http:
                http.timeout = httpx.Timeout(60, connect=10)   # Lighthouse считает долго
                resp = await http.get(PAGESPEED_URL, params=params)
                resp.raise_for_status()
                score = resp.json()["lighthouseResult"]["categories"]["performance"]["score"]
            return (round(float(score) * 100) if score is not None else None), None
        except Exception as exc:  # noqa: BLE001 — скорость необязательна, профиль сохраняем без неё
            return None, "PageSpeed: " + short_error(exc)

    async def healthcheck(self, settings: dict, ctx: Context) -> tuple[bool, str]:
        if not settings.get("pagespeed_key"):
            return True, "работает; скорость не проверяем — нет ключа PageSpeed"
        score, err = await self._pagespeed("https://steptolead.ru/", settings["pagespeed_key"], ctx)
        return (False, err) if err else (True, f"работает; PageSpeed отвечает (steptolead.ru: {score})")


def _site_error(err: str | None) -> str:
    if not err:
        return "сайт не открылся"
    if err.startswith("HTTP "):
        code = int(err[5:]) if err[5:].isdigit() else 0
        return f"сайт ответил ошибкой {code}" if code else "сайт ответил ошибкой"
    if err.startswith("not html"):
        return "на главной не html-страница"
    if "Timeout" in err:
        return "сайт не ответил вовремя"
    return "сайт не открылся"


ADAPTER = SiteTechSource()
