import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function load(path, mocks = {}, storage) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
  } }).outputText;
  const context = { exports: {}, Error, URLSearchParams, localStorage: storage,
    require: name => mocks[name] ?? require(name) };
  vm.runInNewContext(compiled, context);
  return context.exports;
}
const utils = load("../app/admin/leadgen/lists/[id]/workspace-utils.ts");
const plain = value => JSON.parse(JSON.stringify(value));

test("функции всегда выполняются в порядке контракта, без повторов и чужих кодов", () => {
  assert.deepEqual(plain(utils.orderedSteps(["personalize", "email_check", "find_site", "email_check", "other"])),
    ["find_site", "email_check", "personalize"]);
  assert.equal(utils.stepOrder.length, 9);
});
test("таблица: 100 строк, точные f.* условия, без города по умолчанию", () => {
  const query = new URLSearchParams(utils.tableQuery({ page: 2, q: " мебель ", sort: "city", dir: "asc",
    filters: { city: "contains:тест", email_status: "in:valid,risky", phone: "notempty", domain: "empty", niche: "" } }));
  assert.equal(query.get("per_page"), "100");
  assert.equal(query.get("page"), "2");
  assert.equal(query.get("q"), "мебель");
  assert.equal(query.get("f.city"), "contains:тест");
  assert.equal(query.get("f.email_status"), "in:valid,risky");
  assert.equal(query.get("f.phone"), "notempty");
  assert.equal(query.has("f.niche"), false);
  const blank = utils.tableQuery({ page: 1, q: "", sort: "", dir: "asc", filters: {} });
  assert.doesNotMatch(blank, /city|region/);
});
test("вкладки: максимум восемь, текущая первой, без дублей и неверных id", () => {
  const tabs = Array.from({ length: 12 }, (_, id) => ({ id: id + 1, name: `База ${id + 1}` }));
  const output = plain(utils.rememberList(tabs, { id: 3, name: "Новое название" }));
  assert.equal(output.length, 8);
  assert.equal(output[0].name, "Новое название");
  assert.equal(output.filter(item => item.id === 3).length, 1);
  assert.deepEqual(plain(utils.recentLists([{ id: -1, name: "нет" }, null, { id: 2, name: 3 }])), []);
});
test("localStorage: повреждённый JSON, отказ доступа и квота не роняют экран", () => {
  const denied = load("../app/admin/leadgen/lists/[id]/workspace-utils.ts", {}, {
    getItem() { throw new Error("denied"); }, setItem() { throw new Error("quota"); },
  });
  assert.equal(denied.safeRead("key"), null);
  assert.doesNotThrow(() => denied.safeWrite("key", []));
  const corrupt = load("../app/admin/leadgen/lists/[id]/workspace-utils.ts", {}, { getItem: () => "[broken" });
  assert.equal(corrupt.safeRead("key"), null);
});
test("колонки: порядок и видимость сохраняются, исчезнувшие удаляются, новые добавляются", () => {
  const columns = ["display_name", "city", "personal_line", "inn"].map(key => ({ key, label: key, filter: "text" }));
  assert.deepEqual(plain(utils.columnPreferences(columns, [
    { key: "city", visible: false }, { key: "missing", visible: true }, { key: "city", visible: true },
  ])), [
    { key: "city", visible: false }, { key: "display_name", visible: true },
    { key: "personal_line", visible: false }, { key: "inn", visible: true },
  ]);
});
test("ИНН: 10 и 12 цифр, контрольные суммы; неправильные и короткие запрещены", () => {
  assert.equal(utils.validInn("7707083893"), true);
  assert.equal(utils.validInn("500100732259"), true);
  for (const value of ["7707083894", "500100732258", "0000000000", "000000000000", "abc", "123", "", " 7707083893"]) {
    assert.equal(utils.validInn(value), false);
  }
});
const row = { id: 1, display_name: "Компания", domain: "company.test", score: 7, stage: "new",
  score_reasons: [], needs_review: false, city: "", best_email: { value: "info@company.test", verify_status: "valid" },
  emails: 3, phone: null, signals: ["hiring"], messengers: ["telegram"], revenue_rub: 0, source: "import",
  added_at: null, duplicate_group: "g1" };
test("TSV: порядок видимых колонок, экранирование переносов и защита от формул", () => {
  const value = utils.tsv([{ ...row, display_name: "=CMD\tопасно\nстрока" }], [
    { key: "display_name", label: "Компания" }, { key: "email", label: "Почта" },
  ]);
  assert.equal(value, "Компания\tПочта\n'=CMD опасно строка\tinfo@company.test");
  assert.equal(utils.valueOf(row, "email_status"), "valid");
  assert.equal(utils.valueOf(row, "fit"), "");
});
test("таблица рендерит почту, статус, ещё N, дубли и фильтры; HTML компании безопасен", () => {
  const shared = { base: "/admin/leadgen", date: () => "—", money: value => `${value} ₽`,
    fitLabels: {}, stages: { new: "Новая" }, safeUrl: value => `https://${value}`,
    Fit: () => null, Score: ({ value }) => createElement("span", null, value) };
  const { WorkspaceTable } = load("../app/admin/leadgen/lists/[id]/workspace-table.tsx", {
    "@/lib/api": { api() {} }, "../../shared": shared,
    "../../email-status": { EmailStatus: ({ status }) => createElement("span", null, status) },
    "../../sources/source-types": { shortError: e => e.message }, "./workspace-utils": utils,
    "./workspace.module.css": { default: { duplicate: "duplicate", wrap: "wrap" } },
  });
  const html = renderToStaticMarkup(createElement(WorkspaceTable, {
    rows: [{ ...row, display_name: "<script>bad()</script>" }],
    columns: [{ key: "display_name", label: "Компания", filter: "text" }, { key: "email", label: "Почта", filter: "text" }],
    view: { page: 1, q: "", sort: "", dir: "asc", filters: {} }, selected: [], allSelected: false,
    labels: {}, wrap: true, duplicates: true, locked: false, toggle() {}, selectPage() {}, open() {}, changed() {}, setView() {},
  }));
  assert.match(html, /ещё 2/);
  assert.match(html, /info@company.test/);
  assert.match(html, /duplicate/);
  assert.match(html, /Не пусто/);
  assert.doesNotMatch(html, /<script>/);
});
test("ссылки списка ведут в рабочее место, диалоги переиспользуются обоими экранами", () => {
  const lists = readFileSync(new URL("../app/admin/leadgen/lists/page.tsx", import.meta.url), "utf8");
  assert.match(lists, /className=\{styles.companyButton\} href=\{`\/admin\/leadgen\/lists\/\$\{list.id\}`\}/);
  const companies = readFileSync(new URL("../app/admin/leadgen/companies/page.tsx", import.meta.url), "utf8");
  const workspace = readFileSync(new URL("../app/admin/leadgen/lists/[id]/page.tsx", import.meta.url), "utf8");
  assert.match(companies, /from "\.\.\/company-dialogs"/);
  assert.match(workspace, /from "\.\.\/\.\.\/company-dialogs"/);
  assert.doesNotMatch(companies, /function CompanyCard|function AddToListDialog/);
});
