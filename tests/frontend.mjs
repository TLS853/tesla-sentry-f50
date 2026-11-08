// Run with: node --experimental-vm-modules tests/frontend.mjs
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/tesla_sentry/web');
const values = new Map([['tsf_lang', 'en']]);
const themeButton = { setAttribute() {} };
const elements = new Map();
const startupTimers = [], startupRequests = [];
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
  setTimeout(fn) { startupTimers.push(fn); }, clearTimeout() {}, setInterval() {}, clearInterval() {},
  requestAnimationFrame() {}, cancelAnimationFrame() {},
  fetch: async url => {
    startupRequests.push(url);
    return { ok: true, text: async () => '', json: async () => ({ SentryClips: [], SavedClips: [], RecentClips: [] }) };
  },
  __I18N: { en: { '主题': 'Theme', '路': 'cams' } }, __LANGS: [['zh', '中文'], ['en', 'English']],
  __CATEGORIES: { SentryClips: '哨兵告警', SavedClips: '手动保存', RecentClips: '行车记录' },
});
vm.runInContext('globalThis.window = globalThis', context);
const modules = {};
for (const name of ['app.js', 'i18n.js', 'theme.js', 'model.js', 'icons.js']) {
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
for (const callback of startupTimers.splice(0)) callback();
await new Promise(resolve => setImmediate(resolve));
assert.equal(startupRequests.filter(url => url.startsWith('/api/ota/')).length, 0);
assert.equal(typeof context.openExport, 'function');
assert.equal(typeof context.setExpQuality, 'function');
assert.equal(context.curClips, undefined); // internal state is no longer a page-global variable
const qqSettings = {
  provider: 'qq', enabled: true, qq_app_id: '12345', qq_has_secret: true,
  qq_openid: 'A'.repeat(32), qq_target_type: 'user', media: 'text', ready: true,
};
const savedBodies = [];
context.fetch = async (url, options) => {
  if (url === '/api/notify/save') savedBodies.push(JSON.parse(options.body));
  return { ok: true, json: async () => url === '/api/notify/save' ? { ok: true } : qqSettings };
};
context.stab('notify');
await new Promise(resolve => setImmediate(resolve));
assert.equal(element('nt-provider').value, 'qq');
assert.equal(element('nt-qq-fields').style.display, '');
assert.equal(element('nt-feishu-fields').style.display, 'none');
assert.equal(element('nt-qq-secret').value, '');
assert.equal(element('nt-qq-secret').dataset.has, '1');
assert.notEqual(element('nt-qq-openid').value, qqSettings.qq_openid);
context.ntToggleEye();
assert.equal(element('nt-qq-openid').value, qqSettings.qq_openid);
context.ntToggleEye();
await context.ntSave();
assert.equal(savedBodies.length, 1);
assert.equal(savedBodies[0].provider, 'qq');
assert.equal(savedBodies[0].qq_openid, qqSettings.qq_openid); // save original, not display mask
assert.equal(savedBodies[0].qq_app_id, qqSettings.qq_app_id);
assert.equal(savedBodies[0].qq_app_secret, ''); // retains server-side secret
element('nt-provider').value = 'feishu';
context.ntProviderUI();
assert.equal(element('nt-qq-fields').style.display, 'none');
assert.equal(element('nt-feishu-fields').style.display, '');
assert.equal(typeof context.wrapDetach, 'function');
const updateRequests = [];
context.fetch = async (url, options) => {
  updateRequests.push({ url, options });
  return { ok: true, json: async () => url === '/api/ota/status'
    ? { ok: true, current: '2.9.0', mode: 'manual_zip', rollback_ready: true, busy: false }
    : { ok: false, msg: 'test: package rejected' } };
};
context.stab('update');
await new Promise(resolve => setImmediate(resolve));
assert.equal(updateRequests.length, 1);
assert.equal(updateRequests[0].url, '/api/ota/status');
assert.equal(element('upd-rb').style.display, '');
element('upd-url').value = 'https://updates.example/release.zip?X-Amz-Signature=test-signature';
element('upd-sha').value = 'a'.repeat(64);
await context.otaApply();
assert.equal(updateRequests.length, 2);
assert.equal(updateRequests[1].url, '/api/ota/apply');
assert.deepEqual(JSON.parse(updateRequests[1].options.body), {
  url: 'https://updates.example/release.zip?X-Amz-Signature=test-signature', sha256: 'a'.repeat(64),
});
assert.equal(element('upd-go').disabled, false); // retry remains available on failure
element('upd-url').value = 'file:///etc/passwd';
await context.otaApply();
assert.equal(updateRequests.length, 2); // invalid schemes never reach the update endpoint
console.log('Frontend modules: startup without update checks, manual ZIP form, QQ settings and media checks passed');
