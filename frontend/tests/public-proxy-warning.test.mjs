import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../app/admin/proxies/public-proxy-warning.ts", import.meta.url), "utf8");
const context = { exports: {}, URL };
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText, context);
const { hasPossiblyPublicProxy } = context.exports;

test("предупреждает об одном прокси без реквизитов и пачке портов одного хоста", () => {
  assert.equal(hasPossiblyPublicProxy("192.0.2.1:1080"), true);
  assert.equal(hasPossiblyPublicProxy("proxy.example:8001\nPROXY.example:8002\nproxy.example:8003"), true);
  assert.equal(hasPossiblyPublicProxy("socks5://[2001:db8::1]:1080"), true);
  assert.equal(hasPossiblyPublicProxy("http://proxy.example:80"), true);
  assert.equal(hasPossiblyPublicProxy("proxy.example:1080::"), true);
  assert.equal(hasPossiblyPublicProxy("proxy.example:1080@login:sample\r\nproxy.example:1081"), true);
});

test("авторизованные форматы, комментарии и некорректные строки не дают ложного предупреждения", () => {
  assert.equal(hasPossiblyPublicProxy([
    "proxy.example:1080:login:sample", "login:sample@proxy.example:1081",
    "proxy.example:1082@login:sample", "socks5://login:sample@proxy.example:1083",
    "# proxy.example:1084", "", "proxy.example:65536", "http://proxy.example:no-port", "ожидается адрес",
  ].join("\n")), false);
});

test("список сохранённых прокси использует только наличие логина", () => {
  assert.equal(hasPossiblyPublicProxy([]), false);
  assert.equal(hasPossiblyPublicProxy([{ username: "login" }]), false);
  assert.equal(hasPossiblyPublicProxy([{ username: "login" }, { username: null }]), true);
});
