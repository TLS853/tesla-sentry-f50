const I18N = window.__I18N || {};
const LANGS = window.__LANGS || [['zh', '中文']];
export let LANG = (localStorage.getItem('tsf_lang') || '').trim();
if (!LANGS.some(l => l[0] === LANG)) {
  const nav = String((navigator.languages && navigator.languages[0]) || navigator.language || 'zh').toLowerCase();
  const hit = LANGS.find(l => nav === l[0] || nav.indexOf(l[0] + '-') === 0);
  LANG = hit ? hit[0] : 'en';
}
export function t(s) {
  if (!s || LANG === 'zh') return s;
  const m = I18N[LANG] || {};
  return m[s] || (I18N.en || {})[s] || s;
}
export function langName(code) {
  const l = LANGS.find(x => x[0] === code);
  return l ? l[1] : code;
}
const ZH_TXT = '__zh', ZH_TV = '__tv', ZH_ATTR = '__zha';
export function applyLang() {
  document.documentElement.lang = (LANG === 'zh') ? 'zh-CN' : LANG;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  while (w.nextNode()) nodes.push(w.currentNode);
  for (const n of nodes) {
    const p = n.parentNode;
    if (!p) continue;
    const tag = p.nodeName;
    if (tag === 'SCRIPT' || tag === 'STYLE') continue;
    const cur = n.nodeValue;
    if (n[ZH_TXT] !== undefined && cur !== n[ZH_TV] && cur !== n[ZH_TXT]) n[ZH_TXT] = cur;
    if (n[ZH_TXT] === undefined) n[ZH_TXT] = cur;
    const raw = n[ZH_TXT], key = (raw || '').trim();
    if (!key) continue;
    const val = t(key);
    n.nodeValue = (val === key) ? raw : raw.replace(key, val);
    n[ZH_TV] = n.nodeValue;
  }
  document.querySelectorAll('[placeholder],[title]').forEach(el => {
    ['placeholder', 'title'].forEach(a => {
      if (!el.hasAttribute(a)) return;
      el[ZH_ATTR] = el[ZH_ATTR] || {};
      if (el[ZH_ATTR][a] === undefined) el[ZH_ATTR][a] = el.getAttribute(a);
      const raw = el[ZH_ATTR][a], key = (raw || '').trim();
      if (!key) return;
      const val = t(key);
      el.setAttribute(a, val === key ? raw : raw.replace(key, val));
    });
  });
  const g = document.getElementById('lang-grid');
  if (g) renderLangGrid();
}
export function renderLangGrid() {
  const g = document.getElementById('lang-grid');
  if (!g) return;
  g.innerHTML = LANGS.map(([c, n]) =>
    '<button class="' + (c === LANG ? 'on' : '') + '" onclick="setLang(\'' + c + '\')">' +
    n + '<span class="code">' + c.toUpperCase() + '</span></button>').join('');
}
export function setLang(code) {
  if (!LANGS.some(l => l[0] === code)) return;
  LANG = code;
  localStorage.setItem('tsf_lang', code);
  applyLang();
  const th = localStorage.getItem('tsf_theme') || 'auto';
  const b = document.getElementById('theme');
  if (b) b.title = t('主题') + ': ' + t(th === 'auto' ? '自动' : th === 'light' ? '浅色' : '深色');
  window.dispatchEvent(new CustomEvent('tsf:language'));
}
