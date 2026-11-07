import { t } from './i18n.js';

const THEME_ICONS = { auto: '🌗', light: '☀️', dark: '🌙' };
const THEME_NEXT = { auto: 'light', light: 'dark', dark: 'auto' };
export function applyTheme(mode) {
  let eff = mode;
  if (mode === 'auto') {
    eff = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.dataset.theme = eff;
  const b = document.getElementById('theme');
  if (b) { b.textContent = THEME_ICONS[mode] || '🌗'; b.title = t('主题') + ': ' + t(mode === 'auto' ? '自动' : mode === 'light' ? '浅色' : '深色'); }
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
