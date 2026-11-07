"""Public getmatch offers API, validated against the supplied live sample."""
from urllib.parse import urljoin, urlsplit

from .base import Adapter, Context, SourceFormatError, Vacancy, has_word, parse_dt, strip_tags, to_int

ENDPOINT = "https://getmatch.ru/api/offers"


def parse_offer(row: dict) -> Vacancy:
    if not isinstance(row, dict):
        raise SourceFormatError()
    if (type(row.get("id")) is not int or row["id"] <= 0 or not isinstance(row.get("position"), str)
            or not row["position"].strip() or not isinstance(row.get("url"), str)
            or not isinstance(row.get("company"), dict)
            or not isinstance(row.get("skills_objects"), list)
            or not isinstance(row.get("location_requirements"), list)):
        raise SourceFormatError()
    for key in ("published_at", "offer_description", "description_html", "salary_currency"):
        if row.get(key) is not None and not isinstance(row[key], str):
            raise SourceFormatError()
    for key in ("salary_display_from", "salary_display_to"):
        if row.get(key) is not None:
            try:
                amount = float(row[key])
                if isinstance(row[key], bool) or not 0 <= amount < 1_000_000_000:
                    raise SourceFormatError()
            except (TypeError, ValueError, OverflowError) as error:
                raise SourceFormatError() from error
    company = row["company"]
    if not isinstance(company.get("name"), str):
        raise SourceFormatError()
    stack = []
    for skill in row["skills_objects"]:
        if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
            raise SourceFormatError()
        stack.append(skill["name"])
    locations = []
    formats = set()
    for location in row["location_requirements"]:
        if not isinstance(location, dict):
            raise SourceFormatError()
        city = location.get("city")
        if city is not None and not isinstance(city, str):
            raise SourceFormatError()
        if not location.get("exclude"):
            raw_format = location.get("format")
            work_format = raw_format if raw_format in ("remote", "office", "hybrid") else "office"
            if raw_format == "relocation_company":
                work_format = "relocation"
            formats.add(work_format)
            if city and city.strip():
                locations.append({"city": city.strip()})
    work_format = next((kind for kind in ("remote", "hybrid", "relocation") if kind in formats), "office")
    cities = list(dict.fromkeys(item["city"] for item in locations if item["city"] != "Весь мир"))
    description = strip_tags(row.get("offer_description") or row.get("description_html"))
    url = urljoin("https://getmatch.ru", row["url"])
    if urlsplit(url).hostname != "getmatch.ru" or urlsplit(url).scheme != "https":
        raise SourceFormatError()
    salary_from = None if row.get("salary_hidden") else to_int(row.get("salary_display_from"))
    salary_to = None if row.get("salary_hidden") else to_int(row.get("salary_display_to"))
    currency = row.get("salary_currency") or "RUR"
    currency = "RUR" if currency.upper() in {"RUB", "RUR"} else currency.upper()
    published = parse_dt(row.get("published_at"))
    if row.get("published_at") and published is None:
        raise SourceFormatError()
    employer = {"id": company.get("id")}
    if company.get("url"):
        if not isinstance(company["url"], str):
            raise SourceFormatError()
        employer_url = urljoin("https://getmatch.ru", company["url"])
        if urlsplit(employer_url).hostname != "getmatch.ru" or urlsplit(employer_url).scheme != "https":
            raise SourceFormatError()
        employer["url"] = employer_url
    return Vacancy(source="getmatch", ext_id=str(row["id"]), title=row["position"].strip(),
                   company=company["name"], city=", ".join(cities), remote="remote" in formats,
                   salary_from=salary_from, salary_to=salary_to, currency=currency, url=url,
                   snippet=description[:300], published=published, employer=employer,
                   extra={"stack": stack, "work_format": work_format, "text": description})


def parse_page(data, offset):
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list) or not isinstance(data.get("meta"), dict):
        raise SourceFormatError()
    meta = data["meta"]
    if (any(type(meta.get(key)) is not int for key in ("offset", "limit", "total"))
            or meta["offset"] != offset or meta["limit"] <= 0 or meta["total"] < 0):
        raise SourceFormatError()
    return data["offers"], meta


class GetmatchAdapter(Adapter):
    code = "getmatch"
    title = "getmatch"
    icon = "🎯"
    purpose = "IT-вакансии и стек технологий"

    async def fetch(self, f, since, settings, filter_settings, ctx: Context):
        if "getmatch" not in ctx.cache:
            collected = {}
            seen_ids = set()
            offset = 0
            ctx.cache["getmatch_skipped"] = 0
            async with ctx.http() as http:
                for _ in range(1000):
                    response = await http.get(ENDPOINT, params={"offset": offset, "limit": 100})
                    response.raise_for_status()
                    try:
                        rows, meta = parse_page(response.json(), offset)
                    except ValueError as error:
                        raise SourceFormatError() from error
                    new_ids = 0
                    invalid = 0
                    for row in rows:
                        try:
                            vacancy = parse_offer(row)
                        except (SourceFormatError, ValueError):
                            invalid += 1
                            continue
                        if vacancy.ext_id in seen_ids:
                            continue
                        new_ids += 1
                        seen_ids.add(vacancy.ext_id)
                        if row.get("is_active", True):
                            collected[vacancy.ext_id] = vacancy
                    ctx.cache["getmatch_skipped"] += invalid
                    if invalid * 2 > len(rows):
                        raise SourceFormatError("невалидны больше половины предложений страницы")
                    if rows and not new_ids:
                        raise SourceFormatError("повтор страницы предложений")
                    offset += len(rows)
                    if offset >= meta["total"]:
                        break
                    if not rows:
                        raise SourceFormatError("неполный список предложений")
                else:
                    raise SourceFormatError("слишком много страниц предложений")
            ctx.cache["getmatch"] = list(collected.values())
        result = []
        for vacancy in ctx.cache["getmatch"]:
            text = f"{vacancy.title} {vacancy.extra['text']} {' '.join(vacancy.extra['stack'])}"
            if f.get("keywords") and not has_word(text, f["keywords"]):
                continue
            if has_word(text, f.get("exclude") or []):
                continue
            if vacancy.published and vacancy.published < since:
                continue
            if f.get("remote_only") and not vacancy.remote:
                continue
            if f.get("regions") and (f.get("strict_city") or not vacancy.remote) and not has_word(
                    vacancy.city, f["regions"]):
                continue
            top = vacancy.salary_to or vacancy.salary_from
            if top is None and not f.get("allow_no_salary", True):
                continue
            if top is not None and vacancy.currency == "RUR" and top < int(f.get("salary_from") or 0):
                continue
            result.append(vacancy)
        return result


ADAPTER = GetmatchAdapter()
