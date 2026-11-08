import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);
function load(path, mocks = {}) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const context = { exports: {}, Error, require: name => mocks[name] ?? require(name) };
  vm.runInNewContext(compiled, context);
  return context.exports;
}
const types = load("../app/admin/leadgen/sources/source-types.ts");
const plain = value => JSON.parse(JSON.stringify(value));

test("город, регион и секрет не подставляются из схемы по умолчанию", () => {
  assert.deepEqual(plain(types.defaults([
    { key: "city", type: "text", default: "Город" },
    { key: "location", type: "region", default: "Регион" },
    { key: "token", type: "secret", default: "not-a-real-key" },
    { key: "limit", type: "number", default: 30 },
  ])), { limit: 30 });
});

test("нормализация: список построчно, ноль и false сохраняются, объекты секретов не отправляются", () => {
  const schema = [
    { key: "items", type: "list" }, { key: "limit", type: "number" },
    { key: "active", type: "bool" }, { key: "token", type: "secret" },
  ];
  assert.deepEqual(plain(types.normalize(schema, {
    items: " один\n\n два ", limit: "0", active: false, token: { set: true }, unknown: "skip",
  })), { items: ["один", "два"], limit: 0, active: false });
  assert.deepEqual(plain(types.normalize(schema, { token: "" })), { token: "" });
  assert.deepEqual(plain(types.normalize(schema, {})), {});
});

test("проверка обязательных полей и границ оценки", () => {
  const schema = [{ key: "region", type: "region", required: true },
    { key: "limit", type: "number", min: 1, max: 500 }];
  assert.equal(types.valid(schema, { region: " ", limit: 1 }), false);
  assert.equal(types.valid(schema, { region: "Регион", limit: 501 }), false);
  assert.equal(types.valid(schema, { region: "Регион", limit: 500 }), true);
});

test("секрет из ответа сервера никогда не рендерится", () => {
  const { SourceFields } = load("../app/admin/leadgen/sources/source-fields.tsx", {
    "../leadgen.module.css": { default: {} },
  });
  const props = { schema: [{ key: "token", label: "Ключ", type: "secret" }],
    values: {}, prefix: "test", changed() {} };
  const masked = renderToStaticMarkup(SourceFields({ ...props, saved: { token: { set: true } } }));
  assert.match(masked, /Ключ сохранён/);
  assert.match(masked, /type="password"/);
  const unexpected = renderToStaticMarkup(SourceFields({ ...props, saved: { token: "not-a-real-key" } }));
  assert.doesNotMatch(unexpected, /not-a-real-key/);
});

test("тех-профиль различает нулевую скорость и отсутствие проверки, неизвестные коды видны", () => {
  const { CompanyTech } = load("../app/admin/leadgen/company-tech.tsx", {
    "./leadgen.module.css": { default: {} }, "./shared": { date: value => value },
  });
  const tech = { checked_at: "2026-11-08", platform: "future_platform", analytics: [], calltracking: [],
    chat: [], crm_forms: [], quiz: false, privacy_policy: false, footer_year: null, pagespeed_mobile: 0 };
  const html = renderToStaticMarkup(CompanyTech({ tech }));
  assert.match(html, /0\/100/);
  assert.match(html, /future_platform/);
  assert.match(renderToStaticMarkup(CompanyTech({ tech: { ...tech, pagespeed_mobile: null } })), /Не проверяли/);
  assert.match(renderToStaticMarkup(CompanyTech({ tech: null })), /ещё не проверяли/);
});

test("ошибки короткие, без адресов; англоязычные ошибки заменяются русским сообщением", () => {
  assert.doesNotMatch(types.shortError(new Error("Ошибка https://example.test/path?token=private")), /example|private/);
  assert.doesNotMatch(types.shortError(new Error("Сбой api.example.test")), /api\.example/);
  assert.match(types.shortError(new Error("connection refused")), /Не удалось/);
});
