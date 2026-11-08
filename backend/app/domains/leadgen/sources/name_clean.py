"""Company presentation names; personal sole-proprietor names require a public site brand."""
from html.parser import HTMLParser

from app.domains.leadgen import service, site_enrich
from app.domains.leadgen.core.names import clean_company_name
from app.domains.leadgen.core.normalize import clean_company_name as strip_legal
from app.domains.leadgen.sources.site_tech import _robots
from .base import CompanySource, Enrichment


class BrandParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.brand, self.title, self.in_title = "", "", False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("property") or attrs.get("name") or "").lower() == "og:site_name":
            self.brand = attrs.get("content") or ""
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, text):
        if self.in_title:
            self.title += text


class NameCleanSource(CompanySource):
    code = "name_clean"
    title = "Чистка названий"
    icon = "🧹"
    purpose = "Название для писем без юридических форм; исходное название сохраняется"
    requires_site = False
    chain_default = True
    unit_seconds = 1
    concurrency = 50   # без сети — чистая функция

    async def enrich(self, company, settings, ctx):
        if (company.provenance.get("display_name") or {}).get("source") == "manual":
            return Enrichment(status="skipped", error="название исправлено вручную")
        raw = company.name or ""
        name, note = clean_company_name(raw)
        if note == "name_clean:ip" and company.domain:
            async with ctx.http(**{"User-Agent": site_enrich.USER_AGENT}) as http:
                root = f"https://{company.domain}"
                robots, down = await _robots(http, root)
                if not down and (robots is None or robots.can_fetch(site_enrich.USER_AGENT, root + "/")):
                    html, _ = await site_enrich._get_html(http, root + "/")
                    if html:
                        parser = BrandParser(html)
                        brand = parser.brand or parser.title
                        for separator in (" | ", " — ", " - "):
                            brand = brand.split(separator)[0]
                        brand = brand.strip()[:240]
                        personal = (strip_legal(raw) or "").casefold() == brand.casefold()
                        if not personal and brand.lower() not in {"главная", "главная страница", "официальный сайт", ""}:
                            cleaned, why = clean_company_name(brand)
                            if not why:
                                name, note = cleaned, None
        if note:
            reason = "ИП: нет подтверждённого бренда сайта" if note == "name_clean:ip" else "после чистки не осталось названия"
            return Enrichment(status="skipped", error=reason, review=note)
        if name == raw or not name:
            return Enrichment(status="skipped", error="название уже чистое", counters={"unchanged": 1})
        return Enrichment(profile={"raw": raw, "name": name}, counters={"cleaned": 1})

    async def persist(self, db, company, enrichment, ctx):
        if enrichment.status != "done":
            return enrichment
        if (company.provenance or {}).get("display_name", {}).get("source") == "manual":
            return Enrichment(status="skipped", error="название исправлено вручную")
        if not company.legal_name:
            company.legal_name = company.display_name
        raw = company.display_name
        company.display_name = enrichment.profile["name"]
        company.provenance = {**(company.provenance or {}), "display_name": {
            "source": self.code, "at": service.utcnow().isoformat(), "raw": raw,
        }}
        return enrichment


ADAPTER = NameCleanSource()
