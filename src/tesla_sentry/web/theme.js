import { t } from './i18n.js';
import { icon } from './icons.js';

const THEME_ICONS = { auto: 'monitor', light: 'sun', dark: 'moon' };
const THEME_NEXT = { auto: 'light', light: 'dark', dark: 'auto' };
export function applyTheme(mode) {
  if (!['auto', 'light', 'dark'].includes(mode)) mode = 'auto';
  let eff = mode;
  if (mode === 'auto') {
    eff = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.dataset.theme = eff;
  const color = document.querySelector('meta[name="theme-color"]');
  if (color) color.setAttribute('content', eff === 'dark' ? '#151618' : '#ffffff');
  const b = document.getElementById('theme');
  if (b) {
    b.innerHTML = icon(THEME_ICONS[mode] || 'monitor');
    b.title = t('主题') + ': ' + t(mode === 'auto' ? '自动' : mode === 'light' ? '浅色' : '深色');
    b.setAttribute('aria-label', b.title);
  }
}
export function cycleTheme() {
  const cur = localStorage.getItem('tsf_theme') || 'auto';
  const nxt = THEME_NEXT[cur] || 'auto';
  localStorage.setItem('tsf_theme', nxt);
  applyTheme(nxt);
}
applyTheme(localStorage.getItem('tsf_theme') || 'auto');
if (window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('tsf_theme') || 'auto') === 'auto') applyTheme('auto');
});
