import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../app/admin/telegram-infra.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const context = { exports: {}, require: name => name.startsWith("@/") || name.endsWith(".css") ? {} : require(name) };
vm.runInNewContext(compiled, context);
const { isFreeProxy, proxyImportError, safeDiagnostic, toggleId } = context.exports;

test("для аккаунтов предлагаются свободные рабочие прокси аутрича с уникальным стабильным IP", () => {
  const working = {
    pool: "agency", status: "working", exit_ip: "192.0.2.1", max_accounts: 1, assigned_accounts: 0,
    rotating: false, duplicate_of: null, dedicated_workspace_id: null, health: { level: "ok" },
  };
  assert.equal(isFreeProxy(working), true);
  for (const other of [
    { pool: "client" }, { rotating: true }, { duplicate_of: 1 }, { assigned_accounts: 1 },
    { exit_ip: null }, { health: { level: "dead" } }, { status: "untested" }, { dedicated_workspace_id: 9 },
  ]) assert.equal(isFreeProxy({ ...working, ...other }), false);
});

test("ошибки импорта не отражают присланные реквизиты", () => {
  assert.equal(proxyImportError("Строка 3: некорректный host или port", 0),
    "Строка 3: некорректный host или port.");
  assert.equal(proxyImportError("Строка 7: socks5://login:example@host:1080", 0),
    "Строка 7: не удалось добавить прокси.");
  assert.equal(safeDiagnostic("Сбой socks5://login:example@host:1080"), "Сбой [адрес скрыт]");
  assert.equal(safeDiagnostic("Сбой host:1080:login:example"), "Сбой [реквизиты скрыты]");
});

test("массовый выбор не дублирует отмеченные ID", () => {
  assert.deepEqual(Array.from(toggleId([4, 2], 4)), [2]);
  assert.deepEqual(Array.from(toggleId([4, 2], 3)), [4, 2, 3]);
});
