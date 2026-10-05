// Exercise the actual page click handlers and API helper; never contact a provider.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ts = require('typescript');

function load(relative, requireModule, globals = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(js, { exports, require: requireModule, setTimeout, clearTimeout, AbortController, DOMException, ...globals });
  return exports;
}

test('all four AI test buttons send JSON with the selected workspace', async () => {
  const calls = [];
  const fetch = async (url, options) => {
    if (url.includes('/test/')) {
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.deepEqual(JSON.parse(options.body), { workspace_id: 7 });
      calls.push(url);
    }
    return { ok: true, status: 200, json: async () => url.includes('/usage') ? { groups: [] } : { ok: true, answer: 'Проверка пройдена' } };
  };
  const api = load('lib/api.ts', () => {}, { fetch });
  let hook = 0;
  const functions = ['chat', 'summary', 'calls', 'campaigns'].map(feature => ({ feature, configured: true, key_set: true, model: 'test', provider: 'test' }));
  const overview = { functions, stt: { feature: 'stt', configured: false }, limits: {}, workspaces: [{ id: 7, name: 'Тест' }] };
  const element = (type, props) => ({ type, props });
  const page = load('app/admin/ai/page.tsx', name => {
    if (name === '@/lib/api') return api;
    if (name === 'react/jsx-runtime') return { jsx: element, jsxs: element, Fragment: 'fragment' };
    if (name === 'react') return { useEffect() {}, useState(initial) { const i = hook++; return [i === 0 ? overview : i === 3 ? '7' : initial, () => {}]; } };
    throw new Error(name);
  });
  const buttons = [];
  function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || !node.props) return;
    if (node.type === 'button' && !node.props.disabled) buttons.push(node);
    walk(node.props.children);
  }
  walk(page.default());
  assert.equal(buttons.length, 4);
  for (const button of buttons) await button.props.onClick();
  assert.deepEqual(calls, ['/api/admin/ai/test/chat', '/api/admin/ai/test/summary', '/api/admin/ai/test/calls', '/api/admin/ai/test/campaigns']);
});
