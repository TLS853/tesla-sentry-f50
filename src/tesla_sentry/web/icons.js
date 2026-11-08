/* Small, local SVG icons. No font download or external icon service. */
const paths = {
  shield: '<path d="m12 3 8 3v6c0 4.4-4.7 7.7-8 9-3.3-1.3-8-4.6-8-9V6l8-3Z"/><path d="m8.5 12 2.3 2.3 4.8-5"/>',
  settings: '<path d="m9.8 3-.6 2.2-1.8 1-2.2-.6-2.1 3.6 1.6 1.6v2.3l-1.6 1.6 2.1 3.6 2.2-.6 1.8 1 .6 2.3h4.4l.6-2.3 1.8-1 2.2.6 2.1-3.6-1.6-1.6v-2.3l1.6-1.6-2.1-3.6-2.2.6-1.8-1-.6-2.2H9.8Z"/><circle cx="12" cy="12" r="3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/>',
  moon: '<path d="M20.5 13.2A8.5 8.5 0 0 1 10.8 3a8.6 8.6 0 1 0 9.7 10.2Z"/>',
  monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  video: '<rect x="3" y="5" width="13" height="14" rx="2.5"/><path d="m16 10 5-3v10l-5-3"/>',
  camera: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="m8 6 1.5-3h5L16 6"/><circle cx="12" cy="13" r="3.5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/>',
  key: '<circle cx="8" cy="8" r="5"/><path d="m11.5 11.5 9 9m-3-3 3-3m-6 0 3-3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a17 17 0 0 1 0 18 17 17 0 0 1 0-18Z"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18h1a2.4 2.4 0 0 0 1.8-4c-1-1.2-.2-3 1.4-3H18a3 3 0 0 0 3-3c0-4.4-4.2-8-9-8Z"/><path d="M7 11h.01M9 7h.01M14 6h.01M18 9h.01" stroke-width="3"/>',
  update: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1a8.3 8.3 0 0 1 13.3 3M4.6 14.9a8.3 8.3 0 0 0 13.3 3"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  arrowLeft: '<path d="m12 5-7 7 7 7M5 12h15"/>',
  arrowUpRight: '<path d="M6 18 18 6M6 6h12v12"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  play: '<path d="m8 4 12 8-12 8V4Z"/>',
  pause: '<path d="M8 5v14M16 5v14" stroke-width="3"/>',
  storage: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14h18M7 17h.01m4 0h.01"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18"/>',
  audio: '<path d="M9 18V5l11-2v13M9 8l11-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="17" cy="16" rx="3" ry="3"/>',
  car: '<path d="m5 7 2-4h10l2 4 2 3v8H3v-8l2-3Zm-2 4h18M6 14h2m8 0h2M5 18v3m14-3v3"/>',
  location: '<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.5"/>',
  bookmark: '<path d="M6 3h12v18l-6-4-6 4V3Z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="m3 3 18 18M10.5 5.1 12 5c6.5 0 10 7 10 7a21 21 0 0 1-3 4M6 6a24 24 0 0 0-4 6s3.5 7 10 7a12 12 0 0 0 5-1M10 10a3 3 0 0 0 4 4"/>',
  check: '<path d="m5 12 4 4L20 5"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>',
};

export function icon(name, className = '') {
  return '<svg class="icon ' + className.replace(/[^A-Za-z0-9 _-]/g, '') + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (paths[name] || paths.video) + '</svg>';
}

export function mountIcons() {
  document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
}
