"""Commercial offers (КП) and invoices: totals, Russian amount in words and the printable HTML page.

The page is self-contained (inline CSS), opens by a secret link without login and prints to PDF from any browser.
"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape

KINDS = {"offer": "Коммерческое предложение", "invoice": "Счёт на оплату"}
VAT_MODES = {"none": "Без НДС", "0": "0%", "5": "5%", "7": "7%", "10": "10%", "20": "20%", "22": "22%"}
REQUISITE_KEYS = ("company", "inn", "kpp", "ogrn", "address", "bank", "bik", "account", "corr_account",
                  "director", "phone", "email", "site", "vat", "offer_intro", "offer_terms", "invoice_terms", "accent")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clean_items(items: list[dict]) -> list[dict]:
    result = []
    for row in items or []:
        name = " ".join(str(row.get("name") or "").split())[:300]
        if not name:
            continue
        qty = max(Decimal("0"), Decimal(str(row.get("qty") or 1)))
        price = max(Decimal("0"), money(row.get("price")))
        discount = min(Decimal("100"), max(Decimal("0"), Decimal(str(row.get("discount") or 0))))
        result.append({"name": name, "qty": float(qty), "unit": str(row.get("unit") or "шт.")[:16],
                       "price": float(price), "discount": float(discount)})
    return result[:100]


def line_sum(row: dict) -> Decimal:
    return money(Decimal(str(row["price"])) * Decimal(str(row["qty"])) * (Decimal(100) - Decimal(str(row.get("discount") or 0))) / 100)


def total(items: list[dict]) -> Decimal:
    return sum((line_sum(row) for row in items), Decimal("0.00"))


def vat_amount(amount: Decimal, mode: str | None) -> Decimal | None:
    if not mode or mode == "none" or mode not in VAT_MODES:
        return None
    rate = Decimal(mode)
    return money(amount * rate / (100 + rate))


# --------------------------------------------------------------------------- amount in words

_ONES = {"m": ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
         "f": ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]}
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_SCALES = [(("", "", ""), "m"), (("тысяча", "тысячи", "тысяч"), "f"), (("миллион", "миллиона", "миллионов"), "m"),
           (("миллиард", "миллиарда", "миллиардов"), "m")]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _triad(n: int, gender: str) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words += [_TENS[rest // 10], _ONES[gender][rest % 10]]
    return [w for w in words if w]


def amount_in_words(value) -> str:
    amount = money(value)
    rubles, kopecks = int(amount), int((amount - int(amount)) * 100)
    if rubles == 0:
        words = ["ноль"]
    else:
        words, n, scale = [], rubles, 0
        while n and scale < len(_SCALES):
            part = n % 1000
            if part:
                forms, gender = _SCALES[scale]
                chunk = _triad(part, gender)
                if scale:
                    chunk.append(plural(part, forms))
                words = chunk + words
            n //= 1000
            scale += 1
    text = " ".join(words)
    return f"{text[:1].upper()}{text[1:]} {plural(rubles, ('рубль', 'рубля', 'рублей'))} {kopecks:02d} {plural(kopecks, ('копейка', 'копейки', 'копеек'))}"


# --------------------------------------------------------------------------- printable page

def fmt(value) -> str:
    amount = money(value)
    whole = f"{int(amount):,}".replace(",", " ")
    cents = int((amount - int(amount)) * 100)
    return f"{whole},{cents:02d}"


def fmt_qty(value) -> str:
    q = Decimal(str(value))
    return f"{q.normalize():f}".replace(".", ",")


def date_text(value: datetime) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year} г."


def _p(text: str | None) -> str:
    return "<br>".join(escape(line) for line in (text or "").strip().splitlines())


def render(doc, seller: dict, buyer: dict, created: datetime, *, public: bool = False, deal_name: str = "") -> str:
    """Printable HTML of an offer or invoice."""
    s = {k: str(seller.get(k) or "").strip() for k in REQUISITE_KEYS}
    accent = s["accent"] if s["accent"].startswith("#") and len(s["accent"]) in {4, 7} else "#006BFD"
    items = doc.items or []
    amount = total(items)
    vat = vat_amount(amount, s["vat"])
    has_discount = any(row.get("discount") for row in items)
    is_invoice = doc.kind == "invoice"
    title = f"{KINDS[doc.kind]} № {doc.number} от {date_text(created)}"
    rows = "".join(
        f"<tr><td>{i}</td><td>{escape(row['name'])}</td><td class=n>{fmt_qty(row['qty'])}</td><td>{escape(row['unit'])}</td>"
        f"<td class=n>{fmt(row['price'])}</td>" + (f"<td class=n>{fmt_qty(row.get('discount') or 0)}%</td>" if has_discount else "")
        + f"<td class=n>{fmt(line_sum(row))}</td></tr>" for i, row in enumerate(items, 1))
    head = ("<th>№</th><th>Наименование</th><th>Кол-во</th><th>Ед.</th><th>Цена, ₽</th>"
            + ("<th>Скидка</th>" if has_discount else "") + "<th>Сумма, ₽</th>")
    seller_line = ", ".join(x for x in [s["company"], f"ИНН {s['inn']}" if s["inn"] else "", f"КПП {s['kpp']}" if s["kpp"] else "",
                                        s["address"], s["phone"]] if x)
    buyer_line = ", ".join(x for x in [buyer.get("company") or buyer.get("name"), buyer.get("inn") and f"ИНН {buyer['inn']}",
                                       buyer.get("phone"), buyer.get("email")] if x)
    bank = ""
    if is_invoice:
        bank = (f"<table class=bank><tr><td colspan=2 rowspan=2>{escape(s['bank'] or '—')}<br><small>Банк получателя</small></td>"
                f"<td>БИК</td><td>{escape(s['bik'])}</td></tr><tr><td>Сч. №</td><td>{escape(s['corr_account'])}</td></tr>"
                f"<tr><td>ИНН {escape(s['inn'])}</td><td>КПП {escape(s['kpp'])}</td><td rowspan=2>Сч. №</td><td rowspan=2>{escape(s['account'])}</td></tr>"
                f"<tr><td colspan=2>{escape(s['company'])}<br><small>Получатель</small></td></tr></table>")
    totals = f"<tr><td>Итого:</td><td>{fmt(amount)}</td></tr>"
    totals += f"<tr><td>В том числе НДС ({VAT_MODES[s['vat']]}):</td><td>{fmt(vat)}</td></tr>" if vat is not None else "<tr><td>Без НДС</td><td>—</td></tr>"
    totals += f"<tr class=grand><td>Всего к оплате:</td><td>{fmt(amount)} ₽</td></tr>"
    intro = _p(s["offer_intro"]) if not is_invoice else ""
    terms = _p(s["invoice_terms"] if is_invoice else s["offer_terms"])
    valid = f"<p class=muted>Предложение действительно до {date_text(doc.valid_until)}</p>" if doc.valid_until and not is_invoice else ""
    signature = (f"<div class=sign><span>Руководитель</span><i></i><span>{escape(s['director'])}</span></div>" if is_invoice
                 else f"<div class=contacts>{escape(s['company'])}" + "".join(f"<br>{escape(x)}" for x in (s['phone'], s['email'], s['site']) if x) + "</div>")
    toolbar = ("<div class=bar><button onclick='window.print()'>Скачать PDF / Печать</button></div>") if public else \
        "<div class=bar><button onclick='window.print()'>Печать / PDF</button></div>"
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content=noindex><title>{escape(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef2f7;font:14px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#14233c}}
.page{{max-width:820px;margin:24px auto;background:#fff;padding:48px 52px;border-radius:14px;box-shadow:0 10px 40px #0f21400f}}
.bar{{max-width:820px;margin:16px auto 0;display:flex;justify-content:flex-end}}
.bar button{{background:{accent};color:#fff;border:0;border-radius:10px;padding:10px 18px;font:600 14px inherit;cursor:pointer}}
header.top{{display:flex;justify-content:space-between;gap:24px;border-bottom:3px solid {accent};padding-bottom:16px;margin-bottom:22px}}
header.top b{{font-size:18px}}header.top small{{display:block;color:#5d6f8a}}
h1{{font-size:24px;margin:0 0 6px}}.muted{{color:#5d6f8a}}.lead{{margin:16px 0}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}th,td{{border:1px solid #d5deea;padding:7px 9px;text-align:left;vertical-align:top}}
th{{background:#f3f6fb;font-size:12px;color:#40536f}}td.n{{text-align:right;white-space:nowrap}}
table.bank td{{font-size:13px}}table.bank small{{color:#7a8ca5}}
table.totals{{width:auto;margin-left:auto}}table.totals td{{border:0;padding:3px 0 3px 24px;text-align:right}}
table.totals tr.grand td{{font-weight:700;font-size:16px;padding-top:8px}}
.words{{margin:4px 0 18px}}.terms{{margin-top:18px;color:#40536f;font-size:13px}}
.parties p{{margin:4px 0}}.sign{{display:flex;gap:16px;align-items:flex-end;margin-top:36px}}.sign i{{flex:0 0 200px;border-bottom:1px solid #14233c}}
.contacts{{margin-top:28px;color:#40536f}}
@media(max-width:640px){{.page{{margin:0;border-radius:0;padding:24px 16px}}header.top{{flex-direction:column}}th,td{{padding:5px;font-size:12px}}}}
@media print{{body{{background:#fff}}.bar{{display:none}}.page{{box-shadow:none;margin:0;max-width:none;padding:0}}}}
</style></head><body>{toolbar}<main class=page>
<header class=top><div><b>{escape(s['company'] or 'Компания')}</b><small>{escape(s['address'])}</small></div>
<div style="text-align:right"><small>{escape(s['phone'])}</small><small>{escape(s['email'])}</small><small>{escape(s['site'])}</small></div></header>
<h1>{escape(doc.title or title) if not is_invoice else escape(title)}</h1>
{f'<p class=muted>{escape(title)}</p>' if not is_invoice and doc.title else ''}
{bank}
<div class=parties>{f'<p><b>Поставщик:</b> {escape(seller_line)}</p>' if is_invoice else ''}
{f'<p><b>{"Покупатель" if is_invoice else "Для"}:</b> {escape(buyer_line)}</p>' if buyer_line else ''}
{f'<p><b>Основание:</b> {escape(deal_name)}</p>' if is_invoice and deal_name else ''}</div>
{f'<p class=lead>{intro}</p>' if intro else ''}
<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>
<table class=totals>{totals}</table>
<p class=words>Всего наименований {len(items)}, на сумму {fmt(amount)} ₽<br><b>{amount_in_words(amount)}</b></p>
{f'<p class=terms>{_p(doc.note)}</p>' if doc.note else ''}
{f'<p class=terms>{terms}</p>' if terms else ''}
{valid}{signature}</main></body></html>"""
