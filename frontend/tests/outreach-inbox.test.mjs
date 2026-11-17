import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

function load(file, dependencies = {}, globals = {}) {
  const src = readFileSync(new URL(`../app/admin/outreach/inbox/${file}.ts`, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
  const context = { exports: {}, Headers, require: name => dependencies[name], ...globals };
  vm.runInNewContext(outputText, context);
  return context.exports;
}
const utils = load("inbox-utils");

test("входящие сохраняют старые email-ссылки и различают диалоги двух каналов", () => {
  assert.equal(utils.conversationKey(null, "5"), "email:5");
  assert.equal(utils.conversationKey("tg:5", "5"), "tg:5");
  assert.equal(utils.conversationKey("tg:1/lock", null), null);
  const items = [{ key: "email:5" }, { key: "tg:5" }];
  assert.equal(utils.nextConversation(items, "email:5", 1), "tg:5");
  assert.equal(utils.nextConversation(items, "tg:5", -1), "email:5");
});

test("подстановка шаблона однопроходная, истёкшая блокировка не показывается", () => {
  assert.equal(utils.renderReplyTemplate("{{ name }} · {{company}} · {{sender_name}}", {
    contact: { name: "{{company}}", company: "Завод" }, sender: { label: "Анна" },
  }), "{{company}} · Завод · Анна");
  const lock = { id: 2, name: "Анна", until: "2026-11-17T12:00:00Z" };
  assert.equal(utils.activeLock(lock, Date.parse(lock.until) - 1), lock);
  assert.equal(utils.activeLock(lock, Date.parse(lock.until)), null);
});

test("общий список запрашивается без пространства; ответ привязан к клиенту диалога", async () => {
  const calls = [];
  const { inboxRequest, inboxMutation } = load("inbox-api", {
    "@/lib/api": { api: async (...args) => { calls.push(args); } },
    "@/lib/space": { SPACE_HEADER: "X-Workspace-Id" },
  });
  await inboxRequest("/admin/outreach/inbox?all_clients=1", null);
  assert.equal(calls[0][1].headers.has("X-Workspace-Id"), false);
  assert.equal(calls[0][1].noSpace, true);
  await inboxMutation("/admin/outreach/inbox/tg%3A5/reply", 7, "POST", { text: "Ответ", template_id: 2 });
  assert.equal(calls[1][1].headers.get("X-Workspace-Id"), "7");
  assert.deepEqual(JSON.parse(calls[1][1].body), { text: "Ответ", template_id: 2 });
});

test("редактор с черновиком продлевает lock через 20 с и освобождает после закрытия", async () => {
  const callbacks = [], calls = [];
  let timer, cleared;
  const { useReplyLock } = load("reply-lock", {
    react: {
      useState: value => [value, () => {}],
      useRef: value => ({ current: value }),
      useCallback: callback => callback,
      useEffect: callback => callbacks.push(callback),
    },
    "./inbox-api": { inboxMutation: async (url, workspace, method = "POST") => {
      calls.push([url, workspace, method]);
      return { until: "2026-11-17T12:00:00Z" };
    } },
  }, {
    setInterval: (callback, delay) => { timer = callback; assert.equal(delay, 20_000); return 1; },
    clearInterval: id => { cleared = id; },
  });
  useReplyLock("/inbox/tg%3A2", 7, true);
  const unmount = callbacks[0]();
  const close = callbacks[1]();
  const tick = () => new Promise(resolve => setImmediate(resolve));
  await tick();
  await timer();
  unmount();
  close();
  await tick();
  assert.equal(cleared, 1);
  assert.deepEqual(calls, [
    ["/inbox/tg%3A2/lock", 7, "POST"],
    ["/inbox/tg%3A2/lock", 7, "POST"],
    ["/inbox/tg%3A2/lock", 7, "DELETE"],
  ]);
});
