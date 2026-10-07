import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../components/admin-navigation.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const context = { exports: {} }; vm.runInNewContext(compiled, context);
const { adminNavigation, activeParents, filterNavigation, isActiveRoute, navigationNumbers } = context.exports;
const flatten = nodes => Array.from(nodes).flatMap(node => node.kind === "item" ? [node] : flatten(node.children));
const plain = value => JSON.parse(JSON.stringify(value));

test("все 13 действующих маршрутов сохранены", () => {
  assert.deepEqual(flatten(adminNavigation).filter(item => item.href).map(item => item.href).sort(), [
    "/admin", "/result", "/admin/advertising", "/admin/clients", "/admin/campaigns", "/admin/leads", "/admin/accounts", "/admin/parser", "/admin/proxies", "/admin/growth-leads", "/admin/monitor", "/admin/ai", "/admin/briefs",
  ].sort());
});
test("семь будущих пунктов disabled без маршрутов", () => {
  const future = flatten(adminNavigation).filter(item => item.disabled);
  assert.equal(future.length, 7); assert.ok(future.every(item => !item.href));
});
test("Telegram находится внутри Аутрича и Роста агентства", () => {
  assert.deepEqual(plain(activeParents(adminNavigation, "/admin/campaigns/42")), ["growth", "outreach", "telegram"]);
});
test("активный маршрут имеет точную границу, Обзор не активен на дочерних страницах", () => {
  assert.equal(isActiveRoute("/admin/clients/42", "/admin/clients"), true);
  assert.equal(isActiveRoute("/admin/clients-other", "/admin/clients"), false);
  assert.equal(isActiveRoute("/admin/clients", "/admin"), false);
});
test("поиск сохраняет родителей совпадения и не включает посторонние ссылки", () => {
  assert.deepEqual(flatten(filterNavigation(adminNavigation, "  АкКаУнТы ")).map(item => item.id), ["accounts"]);
  assert.deepEqual(plain(activeParents(filterNavigation(adminNavigation, "аккаунты"), "/admin/accounts")), ["growth", "outreach", "telegram"]);
});
test("поиск по папке показывает её дочерние разделы", () => {
  assert.deepEqual(flatten(filterNavigation(adminNavigation, "Telegram")).map(item => item.id), ["campaigns", "queue", "accounts", "parser", "proxies"]);
  assert.equal(filterNavigation(adminNavigation, "несуществующий раздел").length, 0);
});
test("номера двузначные, сбрасываются в группах и не зависят от поиска", () => {
  const nums = navigationNumbers(adminNavigation);
  assert.equal(nums.overview, "01"); assert.equal(nums.results, "01"); assert.equal(nums["growth-leads"], "01"); assert.equal(nums.monitor, "01");
  assert.ok(Object.values(nums).every(number => /^\d{2}$/.test(number)));
  assert.equal(nums.accounts, "10");
});
test("нет дублирующихся id", () => {
  const ids = [];
  const visit = nodes => nodes.forEach(node => { ids.push(node.id); if (node.kind !== "item") visit(node.children); });
  visit(adminNavigation); assert.equal(new Set(ids).size, ids.length);
});
