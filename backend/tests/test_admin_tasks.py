"""Общий список фоновых задач админки: операции видны с любой страницы, ведут туда, где их запустили;
предпросмотр сканера досчитывается сам и его результат можно забрать после возвращения. Без сети."""
import unittest

from app.services import progress
from test_jobradar_scanners import ORIGIN, ScannerBase


class AdminTasksTests(ScannerBase):
    def tearDown(self):
        progress._items.clear()
        super().tearDown()

    def test_preview_listed_with_link_and_result_kept(self):
        s = self.scanner(name="Видно везде", template="blank")
        self.c.patch(f"{self.b}/scanners/{s['id']}", headers=ORIGIN, json={"sources": ["trudvsem"]})
        pv = self.c.post(f"{self.b}/scanners/{s['id']}/preview", headers=ORIGIN, json={"days": 7, "queries": [
            {"name": "М", "keywords": ["маркетолог"]}]}).json()
        tasks = self.c.get("/api/admin/tasks").json()["items"]
        task = next(t for t in tasks if t["key"] == f"jobradar:preview:{s['id']}")
        self.assertEqual((task["state"], task["page"], task["run_id"]), ("done", "jobradar-preview", s["id"]))
        self.assertEqual(task["link"], f"/admin/jobradar/{s['id']}")
        last = self.c.get(f"{self.b}/scanners/{s['id']}/preview/last").json()
        self.assertEqual(last["state"], "done")
        self.assertEqual(last["result"]["matched"], pv["matched"])

    def test_running_task_and_unknown_key(self):
        t = progress.start("jobradar:run:999", "Проверка «Х»", [("a", "A", 5)])
        t.begin("a", total=4)
        progress.start("something-else", "Прочее", [("a", "A", 1)])
        items = {i["key"]: i for i in self.c.get("/api/admin/tasks").json()["items"]}
        self.assertEqual(items["jobradar:run:999"]["state"], "running")
        self.assertEqual(items["jobradar:run:999"]["page"], "jobradar-run")
        self.assertIsNone(items["something-else"]["link"])

    def test_leadgen_links(self):
        from app.domains.leadgen.models import LgList, LgSourceRun

        async def seed(db):
            lst = LgList(workspace_id=7, name="Кухни · октябрь")
            db.add(lst)
            await db.flush()
            runs = [LgSourceRun(workspace_id=7, source=src, status="running", params={}, stats={})
                    for src in (f"list:{lst.id}", "yandex_direct", "company_enrichment", "serp")]
            db.add_all(runs)
            await db.flush()
            return lst.id, [r.id for r in runs]
        list_id, ids = self.run_db(seed)
        for rid in ids:
            progress.start(f"leadgen:source-run:{rid}", "Функции по базе", [("a", "A", 5)])
        items = {i["run_id"]: i for i in self.c.get("/api/admin/tasks").json()["items"]}
        self.assertEqual(items[ids[0]]["link"], f"/admin/leadgen/lists/{list_id}")
        self.assertEqual(items[ids[0]]["title"], "Функции по базе · Кухни · октябрь")
        self.assertEqual(items[ids[1]]["link"], f"/admin/leadgen/search?run={ids[1]}")
        self.assertEqual(items[ids[2]]["page"], "contacts")
        self.assertEqual((items[ids[3]]["page"], items[ids[3]]["source"]), ("source", "serp"))

    def test_requires_admin(self):
        self.c.cookies.clear()
        self.assertIn(self.c.get("/api/admin/tasks").status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
