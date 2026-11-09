import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const require = createRequire(import.meta.url);
function load(path, mocks = {}) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
  } }).outputText;
  const context = { exports: {}, Error, require: name => mocks[name] ?? require(name) };
  vm.runInNewContext(compiled, context);
  return context.exports;
}
const types = load("../app/admin/leadgen/sources/source-types.ts");
const plain = value => JSON.parse(JSON.stringify(value));
const aiUtils = load("../app/admin/leadgen/ai-query-utils.ts");
const fieldMocks = {
  "../leadgen.module.css": { default: {} },
  "../ai-query-utils": aiUtils,
  "../ai-query-picker": { AiQueryPicker: ({ purpose }) =>
    createElement("button", { type: "button", "data-purpose": purpose }, "✨ Подобрать ИИ") },
};

test("общий блок после поиска выбирает только chainable и не добавляет платный шаг сам", () => {
  const { AfterSearch, defaultChain } = load("../app/admin/leadgen/sources/after-search.tsx", {
    "./source-types": types, "../leadgen.module.css": { default: {} },
  });
  const sources = [
    { code: "find_site", kind: "enricher", title: "Найти сайт", chainable: true, chain_default: true,
      enabled: true, ready: true },
    { code: "email_by_inn", kind: "enricher", title: "Почта по ИНН", chainable: true,
      enabled: false, ready: false, ready_reason: "Нужен ключ" },
    { code: "serp", kind: "finder", title: "Поиск", chainable: false, chain_default: true },
  ];
  assert.deepEqual(plain(defaultChain(sources)), ["find_site"]);
  const html = renderToStaticMarkup(AfterSearch({ sources, selected: ["find_site"], changed() {} }));
  assert.match(html, /После поиска/);
  assert.match(html, /Нужен ключ/);
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.equal((html.match(/checked=""/g) || []).length, 1);
});

test("Директ и источник используют один компонент, база отправляет recheck, карточка получает прогресс", () => {
  const direct = readFileSync(new URL("../app/admin/leadgen/search/page.tsx", import.meta.url), "utf8");
  const source = readFileSync(new URL("../app/admin/leadgen/sources/source-run.tsx", import.meta.url), "utf8");
  const workspace = readFileSync(new URL("../app/admin/leadgen/lists/[id]/function-panel.tsx", import.meta.url), "utf8");
  for (const content of [direct, source]) assert.match(content, /<AfterSearch/);
  assert.doesNotMatch(direct, /Сразу собрать контакты с сайтов/);
  assert.match(direct, /TaskProgress/);
  assert.match(workspace, /recheck/);
  assert.match(workspace, /Перепроверить недавно проверенные/);
  const companies = readFileSync(new URL("../app/admin/leadgen/companies/page.tsx", import.meta.url), "utf8");
  assert.match(companies, /ContactProgress/);
  assert.match(companies, /run_id/);
});

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
  const { SourceFields } = load("../app/admin/leadgen/sources/source-fields.tsx", fieldMocks);
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

test("multi: несколько типизированных options, пустое обязательное поле и неизвестный вариант", () => {
  const schema = [{ key: "platforms", type: "multi", required: true,
    options: [{ value: "hh", label: "hh.ru" }, { value: 3, label: "Третья" }] }];
  assert.deepEqual(plain(types.normalize(schema, { platforms: ["hh", 3, "hh"] })), { platforms: ["hh", 3] });
  assert.equal(types.valid(schema, { platforms: ["hh", 3] }), true);
  assert.equal(types.valid(schema, { platforms: [] }), false);
  assert.equal(types.valid(schema, { platforms: ["unknown"] }), false);
  assert.equal(types.valid(schema, { platforms: "hh" }), false);
  const { SourceFields } = load("../app/admin/leadgen/sources/source-fields.tsx", fieldMocks);
  const html = renderToStaticMarkup(SourceFields({ schema, values: { platforms: ["hh", 3] },
    prefix: "multi", changed() {} }));
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.equal((html.match(/checked=""/g) || []).length, 2);
});

test("формы serp и hiring работают по схеме, без привязки к коду источника", () => {
  const serp = [{ key: "queries", type: "list", required: true, ai: { purpose: "search" } },
    { key: "region", type: "region", required: true },
    { key: "depth", type: "number", default: 2, min: 1, max: 5 }];
  assert.equal(types.valid(serp, { ...types.defaults(serp), queries: "запрос" }), false);
  for (const depth of [0, 6, "wrong"]) {
    assert.equal(types.valid(serp, { queries: ["запрос"], region: "Регион", depth }), false);
  }
  const hiring = [{ key: "titles", type: "list", ai: { purpose: "jobs" } },
    { key: "days", type: "number", min: 1, max: 30 }, { key: "hh_url", type: "text" }];
  const values = { titles: "маркетолог", days: 14, hh_url: "https://hh.ru/search/vacancy?text=test" };
  assert.equal(types.valid(hiring, values), true);
  assert.equal(types.normalize(hiring, values).hh_url, values.hh_url);
  const { SourceFields } = load("../app/admin/leadgen/sources/source-fields.tsx", fieldMocks);
  const html = renderToStaticMarkup(SourceFields({ schema: [...serp, ...hiring], values: {},
    prefix: "schema", changed() {} }));
  assert.match(html, /data-purpose="search"/);
  assert.match(html, /data-purpose="jobs"/);
  assert.match(html, /min="1" max="5"/);
});

test("предложения ИИ добавляются без дублей, включая регистр, пробелы и пустые строки", () => {
  assert.deepEqual(plain(aiUtils.mergeQueries(["Кухни на заказ", "  "],
    ["кухни  на заказ", "купить кухню", "КУПИТЬ КУХНЮ"])), ["Кухни на заказ", "купить кухню"]);
  assert.deepEqual(plain(aiUtils.aiQueryBody("direct", "  Ниша ", "", "")), { purpose: "direct", niche: "Ниша" });
  assert.deepEqual(plain(aiUtils.aiQueryBody("jobs", "Ниша", " Бриф ", " Регион ")),
    { purpose: "jobs", niche: "Ниша", brief: "Бриф", region: "Регион" });
});

test("оценка стоимости: null не превращается в 0 ₽, отсутствующий cost не показывается", () => {
  const { SourceCost } = load("../app/admin/leadgen/sources/source-cost.tsx");
  assert.equal(renderToStaticMarkup(SourceCost({ cost: undefined })), "");
  assert.doesNotMatch(renderToStaticMarkup(SourceCost({ cost: { requests: 40, rub: null } })), /₽/);
  const paid = renderToStaticMarkup(SourceCost({ cost: { requests: 40, rub: 1 } }));
  assert.match(paid, /Запросов к поиску: 40/);
  assert.match(paid, /≈ 1 ₽/);
  assert.match(renderToStaticMarkup(SourceCost({ cost: { requests: 0, rub: 0 } })), /≈ 0 ₽/);
});

test("значки почты различают valid/risky/invalid/не проверена", () => {
  const { EmailStatus } = load("../app/admin/leadgen/email-status.tsx", {
    "./leadgen.module.css": { default: {} },
  });
  for (const [status, expected] of [["valid", "✓ Проверена"], ["risky", "⚠ Есть риск"],
    ["invalid", "✕ Некорректная"], ["unknown", "Не проверена"]]) {
    assert.ok(renderToStaticMarkup(EmailStatus({ status })).includes(expected));
  }
});

test("итог цепочки: нулевые счётчики видны, причины без адресов", () => {
  const { ChainResult } = load("../app/admin/leadgen/sources/chain-result.tsx", {
    "../leadgen.module.css": { default: {} }, "./source-types": types,
  });
  const html = renderToStaticMarkup(ChainResult({ chain: { email_check: {
    done: 1, skipped: 2, failed: 0, valid: 0, invalid: 1, risky: 1,
    reasons: { "Нет сайта https://private.test/path": 2 },
  } }, names: { email_check: "Проверка почт" } }));
  assert.match(html, /Проверка почт/);
  assert.match(html, /Корректных почт: 0/);
  assert.doesNotMatch(html, /private\.test/);
});
