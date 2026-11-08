"""Поиск почт на сайте: адреса в скриптах, защита Cloudflare, «[at]»-запись, мусор из виджетов, страница контактов
без ссылки, общие ящики не считаются личными. Без сети."""
import asyncio
import unittest

import httpx

from app.domains.leadgen import site_enrich
from app.domains.leadgen.core.site_extract import extract, good_email


def cf(email: str, key: int = 0x2A) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


class ExtractEmailTests(unittest.TestCase):
    def emails(self, html):
        return sorted(extract(html, "https://firma.example/").emails)

    def test_email_inside_script_data(self):
        html = ('<html><body><div id="app"></div><script>window.__NUXT__={contacts:{email:"b2b@firma.example",'
                'dsn:"https://0123456789abcdef0123@o1.ingest.sentry.io/1"}}</script></body></html>')
        self.assertEqual(self.emails(html), ["b2b@firma.example"])

    def test_cloudflare_protected(self):
        html = (f'<a href="/cdn-cgi/l/email-protection#{cf("zakaz@firma.example")}">[email&#160;protected]</a>'
                f'<span class="__cf_email__" data-cfemail="{cf("info@firma.example", 0x55)}"></span>')
        self.assertEqual(self.emails(html), ["info@firma.example", "zakaz@firma.example"])

    def test_obfuscated_at_and_dot(self):
        html = "<p>Пишите: sales [at] firma [dot] example или office (собака) firma.example</p>"
        self.assertEqual(self.emails(html), ["office@firma.example", "sales@firma.example"])

    def test_junk_filtered(self):
        for bad in ("0@unknown.email", "abc@sentry.io", "user@example.com", "logo@2x.png", "x@wixpress.com", "noreply@firma.example",
                    "0123456789abcdef0123@firma.example"):
            self.assertFalse(good_email(bad), bad)
        self.assertTrue(good_email("tender@firma.example"))


class PersonalTests(unittest.TestCase):
    def test_departments_are_not_personal(self):
        for e in ("tender@marya.ru", "shatura@shatura.com", "site@okna-veka.ru", "corpsales@shatura.com",
                  "b2b@divan.ru", "magazin@marya.ru", "director@marya.ru"):
            self.assertFalse(site_enrich._looks_personal(e, e.split("@")[1]), e)

    def test_people_are_personal(self):
        for e in ("ivan.petrov@marya.ru", "a.ivanova@marya.ru", "petrov@marya.ru", "kovalenko@marya.ru",
                  "petrov_a@marya.ru", "olga@marya.ru", "i.v.semin@marya.ru", "semin.i.v@marya.ru"):
            self.assertTrue(site_enrich._looks_personal(e, "marya.ru"), e)

    def test_other_domains(self):
        for e in ("s.yakovlev@teremopt.ru", "ivan.petrov@mail.ru"):            # имя — личный, где бы ни лежал
            self.assertTrue(site_enrich._looks_personal(e, "terem.ru"), e)
        for e in ("dentalia@yandex.ru", "dom0717@mail.ru", "kuhni.ru@mail.ru", "pes-bani@yandex.ru", "hr@terem.ru"):
            self.assertFalse(site_enrich._looks_personal(e, "terem.ru"), e)


class ContactPageProbeTests(unittest.TestCase):
    def crawl(self, pages):
        log = []

        def handler(request):
            log.append(request.url.path)
            body = pages.get(request.url.path)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})
        result = asyncio.run(site_enrich.crawl("firma.example", transport=httpx.MockTransport(handler), delay=0))
        return result, log

    def test_unlinked_contacts_page_is_probed(self):
        result, log = self.crawl({"/": "<html><body>Кухни на заказ</body></html>",
                                  "/kontakty": "<html><body>corp@firma.example</body></html>"})
        self.assertEqual(sorted(result.facts.emails), ["corp@firma.example"])
        self.assertNotIn("/about", log)            # нашли — дальше не перебираем
        self.assertEqual(result.errors, {})        # 404 на догадках — не ошибка сайта

    def test_contacts_page_probed_even_with_email_on_home(self):
        result, log = self.crawl({"/": "<html><body>info@firma.example</body></html>",
                                  "/contacts": "<html><body>corpsales@firma.example</body></html>"})
        self.assertEqual(sorted(result.facts.emails), ["corpsales@firma.example", "info@firma.example"])
        self.assertNotIn("/kontakty", log)

    def test_no_probe_when_contacts_linked(self):
        _, log = self.crawl({"/": '<html><body><a href="/kontakty">Контакты</a></body></html>',
                             "/kontakty": "<html><body>info@firma.example</body></html>"})
        self.assertNotIn("/contacts", log)


if __name__ == "__main__":
    unittest.main()
