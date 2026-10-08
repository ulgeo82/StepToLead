import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";
const source = readFileSync(new URL("../app/admin/outreach/email/types.ts", import.meta.url), "utf8");
const context={exports:{}};
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,context);
const {fieldForError,emptyStep,emptyWindow}=context.exports;
test("ошибки 422 привязаны к шагу и полю",()=>{
  assert.equal(fieldForError("Шаг 2: у первого письма в ветке нужна тема"),"steps.1.subject");
  assert.equal(fieldForError("Шаг 1: у письма нужен текст"),"steps.0.body");
  assert.equal(fieldForError("Шаг 3: пауза от 0 до 60 дней"),"steps.2.delay_days");
  assert.equal(fieldForError("smtp_port: неверное значение"),"smtp_port");
  assert.equal(fieldForError("steps · 0 · body: неверное значение"),"steps.0.body");
  assert.equal(fieldForError("Неизвестный часовой пояс"),"window");
});
test("черновики независимы, окно по умолчанию понедельник–пятница",()=>{
  const first=emptyStep(),second=emptyStep();first.body="тест";assert.equal(second.body,"");
  assert.deepEqual(JSON.parse(JSON.stringify(emptyWindow().weekdays)),[0,1,2,3,4]);
});
