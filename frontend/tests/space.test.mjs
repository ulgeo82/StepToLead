import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../lib/space.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;

function load(win) {
  const context = { exports: {}, URLSearchParams, ...(win ? { window: win } : {}) };
  vm.runInNewContext(compiled, context);
  return context.exports;
}
function memory(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: (key) => (data.has(key) ? data.get(key) : null), setItem: (key, value) => data.set(key, String(value)) };
}
const browser = (pathname, search = "", session = {}, local = {}) =>
  ({ location: { pathname, search }, sessionStorage: memory(session), localStorage: memory(local) });

test("клиент из ссылки главнее выбора вкладки, а тот — последнего выбора в браузере", () => {
  const { pickSpace } = load();
  assert.equal(pickSpace("?touch=5&ws=8", "3", "4"), 8);
  assert.equal(pickSpace("?touch=5", "3", "4"), 3);
  assert.equal(pickSpace("", null, "4"), 4);
  assert.equal(pickSpace("", null, null), null);
  assert.equal(pickSpace("?ws=abc", "0", null), null);
  assert.equal(pickSpace("?ws=-2", null, "1.5"), null);
});

test("пространство клиента — только в лидогенерации и аутриче и только для их API", () => {
  const { isSpacePage, isSpaceApi } = load();
  assert.equal(isSpacePage("/admin/leadgen/lists/5"), true);
  assert.equal(isSpacePage("/admin/outreach/inbox"), true);
  assert.equal(isSpacePage("/admin/jobradar"), false);
  assert.equal(isSpacePage("/admin/leadgenius"), false);
  assert.equal(isSpaceApi("/admin/leadgen/companies?q=1"), true);
  assert.equal(isSpaceApi("/admin/tasks"), false);
  assert.equal(isSpacePage("/admin/campaigns/7"), true);
  assert.equal(isSpacePage("/admin/accounts"), true);
  assert.equal(isSpaceApi("/telegram-accounts/health-summary"), true);
  assert.equal(isSpaceApi("/proxies?pool=agency"), true);
  assert.equal(isSpaceApi("/admin/outreach/inbox"), true);
  assert.equal(isSpaceApi("/telegram-blocklist"), false);
});

test("запрос уходит в пространстве выбранного клиента; радар вакансий остаётся у агентства", () => {
  const onLists = load(browser("/admin/leadgen/lists", "", { "stl:space": "8" }));
  assert.equal(onLists.spaceForRequest("/admin/leadgen/lists"), 8);
  assert.equal(onLists.spaceForRequest("/admin/tasks"), null);
  const onRadar = load(browser("/admin/jobradar", "", { "stl:space": "8" }));
  assert.equal(onRadar.spaceForRequest("/admin/leadgen/clients"), null);
  assert.equal(load().spaceForRequest("/admin/leadgen/lists"), null); // на сервере окна нет
});

test("ссылка с ?ws= переключает вкладку на клиента и запоминает его", () => {
  const win = browser("/admin/outreach/inbox", "?touch=5&ws=8", { "stl:space": "3" });
  const space = load(win);
  assert.equal(space.currentSpace(), 8);
  assert.equal(win.sessionStorage.getItem("stl:space"), "8");
  win.location.search = "";
  assert.equal(space.currentSpace(), 8);
});

test("вкладки не путают клиентов: агентство, выбранное во вкладке, не затирается выбором в другой", () => {
  const fresh = browser("/admin/leadgen/lists", "", {}, { "stl:space": "8" });
  assert.equal(load(fresh).currentSpace(), 8); // новая вкладка берёт последний выбор
  const agency = browser("/admin/leadgen/lists", "", { "stl:space": "0" }, { "stl:space": "8" });
  assert.equal(load(agency).currentSpace(), null);
  const win = browser("/admin/leadgen/lists");
  const space = load(win);
  space.chooseSpace(8);
  assert.equal(space.currentSpace(), 8);
  space.chooseSpace(null);
  assert.equal(space.currentSpace(), null);
  assert.equal(win.localStorage.getItem("stl:space"), "0");
});

test("скачивание файла и смена клиента", () => {
  const { spaceUrl, sectionRoot } = load();
  assert.equal(spaceUrl("/api/admin/leadgen/exports/3/download", 8), "/api/admin/leadgen/exports/3/download?ws=8");
  assert.equal(spaceUrl("/api/admin/leadgen/companies/export?q=a", 8), "/api/admin/leadgen/companies/export?q=a&ws=8");
  assert.equal(spaceUrl("/api/admin/leadgen/exports/3/download", null), "/api/admin/leadgen/exports/3/download");
  assert.equal(sectionRoot("/admin/leadgen/lists/5"), "/admin/leadgen/lists");
  assert.equal(sectionRoot("/admin/outreach/campaigns/12"), "/admin/outreach/campaigns");
  assert.equal(sectionRoot("/admin/campaigns/12"), "/admin/campaigns");
  assert.equal(sectionRoot("/admin/leadgen"), "/admin/leadgen");
  assert.equal(sectionRoot("/admin/clients/4"), "/admin/clients/4");
});
