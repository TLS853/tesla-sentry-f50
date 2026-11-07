// Run with: node --experimental-vm-modules tests/frontend.mjs
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/tesla_sentry/web');
const values = new Map([['tsf_lang', 'en']]);
const themeButton = {};
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    innerHTML: '', textContent: '', value: '', checked: false, style: {}, dataset: {}, children: [],
    classList: { add() {}, remove() {}, toggle: () => false, contains: () => false },
    addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {},
    querySelectorAll: () => [], querySelector: () => null, getAttribute: () => '',
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
  });
  return elements.get(id);
}
const document = {
  documentElement: { dataset: {} }, body: {},
  getElementById: id => id === 'theme' ? themeButton : element(id),
  querySelectorAll: () => [],
  querySelector: () => null, addEventListener() {}, createElement: () => element(Symbol()),
  createTreeWalker: () => ({ nextNode: () => false }),
};
const context = vm.createContext({
  document, NodeFilter: { SHOW_TEXT: 4 },
  localStorage: { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) },
  navigator: { language: 'en' },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  CustomEvent: class { constructor(type) { this.type = type; } },
  dispatchEvent() {},
  addEventListener() {},
  location: { hostname: '127.0.0.1', search: '', protocol: 'http:', pathname: '/' },
  URLSearchParams, URL,
  setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
  requestAnimationFrame() {}, cancelAnimationFrame() {},
  fetch: async () => ({ ok: true, text: async () => '',
    json: async () => ({ SentryClips: [], SavedClips: [], RecentClips: [] }) }),
  __I18N: { en: { '主题': 'Theme', '路': 'cams' } }, __LANGS: [['zh', '中文'], ['en', 'English']],
  __CATEGORIES: { SentryClips: '哨兵告警', SavedClips: '手动保存', RecentClips: '行车记录' },
});
vm.runInContext('globalThis.window = globalThis', context);
const modules = {};
for (const name of ['app.js', 'i18n.js', 'theme.js', 'model.js']) {
  modules[name] = new vm.SourceTextModule(await fs.readFile(path.join(root, name), 'utf8'),
    { context, identifier: name });
}
await modules['app.js'].link(specifier => {
  const module = modules[specifier.replace('./', '')];
  assert.ok(module, `unknown module import: ${specifier}`);
  return module;
});
await modules['model.js'].evaluate();
const model = modules['model.js'].namespace;
const groups = model.groupVideos([
  { cam: 'back', f: '2026-11-07_12-00-00-back.mp4' },
  { cam: 'front', f: '2026-11-07_12-00-00-front.mp4' },
  { cam: 'front', f: '2026-11-07_12-01-00-front.mp4' },
]);
assert.equal(groups.length, 2);
assert.equal(groups[0].key, '2026-11-07_12-00-00');
assert.equal(groups[0].vids[0].cam, 'front');
assert.equal(groups[0].vids[1].cam, 'back');
await modules['theme.js'].evaluate();
assert.equal(document.documentElement.dataset.theme, 'light');
const i18n = modules['i18n.js'].namespace;
assert.equal(i18n.t('路'), 'cams');
i18n.setLang('zh');
assert.equal(i18n.t('路'), '路');
await modules['app.js'].evaluate();
await new Promise(resolve => setImmediate(resolve));
assert.equal(typeof context.openExport, 'function');
assert.equal(typeof context.setExpQuality, 'function');
assert.equal(context.curClips, undefined); // internal state is no longer a page-global variable
console.log('Frontend modules: syntax, imports, clip grouping, theme, language and startup checks passed');
