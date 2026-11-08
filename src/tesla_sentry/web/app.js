import { LANG, t, langName, applyLang, renderLangGrid, setLang } from './i18n.js';
import { applyTheme, cycleTheme } from './theme.js';
import { CAM_NAME, camSort, clipKeyOf, groupVideos } from './model.js';
import { icon, mountIcons } from './icons.js';

mountIcons();

let data = {}, cur = null, syncTimer = null, currentCat = 'SentryClips', lastSig = '', reasonFilter = 'all', dateFilter = 'all';
let curClips = [], curClipIx = 0, TEL = null;

/* ---------- 多语言 ----------
   字典以中文原文为 key; 静态文案由 applyLang() 遍历 DOM 替换, 动态文案在生成时用 t() 包一层。
   切回中文时从节点上保存的原文还原, 不会叠加翻译。 */
/* ---------- 主题: 自动(跟系统) / 浅色 / 深色, 手动循环切换并记住 ---------- */
/* ---------- 清晰度选择 ----------
   内网(100.96.x/192.168.x)默认原画; 走公网/穿透访问时默认 360p 省 5G 上行带宽 */
const Q_LIST = [['orig', '原画'], ['720p', '720p'], ['480p', '480p'], ['360p', '360p']];
const isLanNet = /^(100\.96\.|192\.168\.|10\.|127\.)/.test(location.hostname);
let curQ = localStorage.getItem('tsfq') || (isLanNet ? 'orig' : '360p');
if (!Q_LIST.some(x => x[0] === curQ)) curQ = 'orig';
function qUrl(u) { return curQ === 'orig' ? u : u + '?q=' + curQ; }
function renderQSel() {
  const curLabel = (Q_LIST.find(x => x[0] === curQ) || Q_LIST[0])[1];
  document.getElementById('qsel').innerHTML =
    `<button class="qbtn ${curQ !== 'orig' ? 'on' : ''}" onclick="toggleQ(event)">${curLabel} ⌃</button>` +
    '<div class="qmenu">' + Q_LIST.map(([k, label]) =>
      `<button class="${k === curQ ? 'on' : ''}" onclick="setQuality('${k}')">${label}</button>`).join('') + '</div>';
}
function toggleQ(e) {
  if (e) e.stopPropagation();
  document.getElementById('qsel').classList.toggle('open');
}
function closeQ() {
  ['qsel', 'expq'].forEach(id => { const el = document.getElementById(id); if (el) el.classList.remove('open'); });
}
document.addEventListener('click', closeQ);
function setQuality(q) {
  closeQ();
  if (q === curQ) return;
  curQ = q; localStorage.setItem('tsfq', q);
  renderQSel();
  if (!cur) return;
  // 记住各路播放进度与播放状态, 换源后续播
  const v0el = document.querySelector('#vgrid video');
  const wasPlaying = v0el ? !v0el.paused : false;
  const times = [...document.querySelectorAll('#vgrid video')].map(v => v.currentTime || 0);
  const hint = document.getElementById('qhint');
  if (q !== 'orig') {
    hint.textContent = '正在按 ' + q + ' 转码所选片段 (首次约需 20~60 秒/路, 转好一次后秒开)…';
    hint.style.display = 'block';
    clearTimeout(setQuality._ht);
    setQuality._ht = setTimeout(() => { hint.style.display = 'none'; }, 90000);
  } else {
    hint.style.display = 'none';
  }
  renderClip();
  const vids = [...document.querySelectorAll('#vgrid video')];
  vids.forEach((v, i) => {
    const seek = () => { try { if (times[i]) v.currentTime = times[i]; } catch (e) {} };
    if (v.readyState >= 1) seek();
    else v.addEventListener('loadedmetadata', seek, { once: true });
  });
  if (wasPlaying) tryPlay();
  // 第一路能播就撤提示
  const v0 = vids[0];
  if (v0) v0.addEventListener('canplay', () => { hint.style.display = 'none'; }, { once: true });
}
const CATS_NAME = window.__CATEGORIES;
/* event.json 里的 reason → 中文名 (reason 是自由文本, 新值靠前缀匹配兜底) */
const REASON_LABEL = {
  sentry_aware_object_detection: '哨兵警觉',
  sentry_aware_door: '车门拉动',
  sentry_aware_handle: '车门拉动',
  sentry_aware_accel: '遭遇袭击',
  sentry_panic: '遭遇袭击',
  sentry_trigger: '哨兵模式触发',
  user_interaction_dashcam_saved: '手动保存',
  user_interaction_dashcam_menu: '行车记录仪菜单保存',
  user_interaction_dashcam_launcher_action_tapped: '点击行车记录仪图标',
  user_interaction_honk: '按喇叭保存'
};
function reasonLabel(r) {
  if (!r) return '';
  if (REASON_LABEL[r]) return t(REASON_LABEL[r]);
  for (const k in REASON_LABEL) if (r.indexOf(k) === 0) return t(REASON_LABEL[k]);
  return r;
}
const EMPTY_HTML = document.getElementById('empty').innerHTML;
/* Model 3/Y (HW3) 只有 4 路: front / back / left_repeater / right_repeater
   Model S/X 与 HW4 车型才有 6 路 (含 left_pillar / right_pillar)
   页面按每个事件实际的文件数自适应, 不写死路数 */
function camLabel(c) { return t(CAM_NAME[c] || c || '画面'); }
function fmtFull(n) {
  const m = (n || '').match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})/);
  return m ? `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}:00` : n;
}
function camShort(c) {
  const m = {front:'前',back:'后',left_repeater:'左',right_repeater:'右',left_pillar:'左',right_pillar:'右'};
  return m[c] || (camLabel(c) || '画').slice(0, 1);
}
function toggleDetail() {
  const r = document.getElementById('ic-rows'), b = document.getElementById('ic-fold');
  const open = r.classList.toggle('open');
  b.textContent = open ? '收起详细数据 ⌃' : '展开详细数据 ⌄';
  layoutPlayer();
}
function fmtName(n) {
  const m = (n || '').match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})/);
  return m ? m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : n;
}
function curVids() { return (curClips[curClipIx] || { vids: [] }).vids; }
function switchClip(i) {
  if (!curClips[i]) return;
  curClipIx = i;
  renderClip();
}
function gridDims(n) {
  if (n <= 1) return [1, 1];
  if (n === 2) return [2, 1];
  if (n <= 4) return [2, 2];
  if (n <= 6) return window.innerWidth <= 700 ? [2, 3] : [3, 2];
  const c = window.innerWidth <= 700 ? 2 : 3;
  return [c, Math.ceil(n / c)];
}

function tabContent(category, count) {
  const symbols = { SentryClips: 'shield', SavedClips: 'bookmark', RecentClips: 'video' };
  return icon(symbols[category] || 'video') + `<span class="tl">${t(CATS_NAME[category] || category)}</span><span class="tn">${count}</span>`;
}
async function load() {
  data = await (await fetch('/api/events')).json();
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  const liveOpen = document.getElementById('livep').classList.contains('open');
  for (const c of Object.keys(data)) {
    const b = document.createElement('button');
    b.className = 'tab' + (!liveOpen && c === currentCat ? ' active' : '');
    b.innerHTML = tabContent(c, data[c].length);
    b.onclick = () => { document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); b.classList.add('active'); currentCat = c; reasonFilter = 'all'; dateFilter = 'all'; closeLive(); render(c); };
    tabs.appendChild(b);
  }
  /* 第 4 个页签: 实时预览 (与分类页签同级) */
  const lv = document.createElement('button');
  lv.className = 'tab' + (liveOpen ? ' active' : '');
  lv.id = 'tab-live';
  lv.innerHTML = icon('camera') + '<span class="tl">' + t('最新画面') + '</span>';
  lv.onclick = () => { document.querySelectorAll('.tab').forEach(x => x.classList.remove('active')); lv.classList.add('active'); openLive(); };
  tabs.appendChild(lv);
  document.getElementById('stat').textContent = `${t('归档')} ${Object.values(data).reduce((a, v) => a + v.length, 0)} ${t('条')}`;
  lastSig = sigOf(data);
  renderQSel();
  render(currentCat);
  openDeepLink();
}

/* 支持 ?ev=SentryClips/2026-11-04_12-30-00 直接打开某个事件 */
function openDeepLink() {
  const q = new URLSearchParams(location.search);
  /* ?date=2026-11-03 只看某一天 */
  const qd = q.get('date');
  if (/^\d{4}-\d{2}-\d{2}$/.test(qd || '')) { dateFilter = qd; render(currentCat); }
  /* ?cat=SavedClips 直接切到某个分类 */
  const qc = q.get('cat');
  if (qc && Object.keys(data).includes(qc)) {
    const tabs = document.getElementById('tabs');
    [...tabs.children].forEach(b => b.classList.remove('active'));
    const idx = Object.keys(data).indexOf(qc);
    if (tabs.children[idx]) tabs.children[idx].classList.add('active');
    currentCat = qc; reasonFilter = 'all'; render(qc);
  }
  const m = q.get('ev');
  if (!m) return;
  const keys = Object.keys(data);
  for (const c of keys) {
    const ev = (data[c] || []).find(e => e.name === m || (c + '/' + e.name) === m);
    if (!ev) continue;
    const tabs = document.getElementById('tabs');
    [...tabs.children].forEach(b => b.classList.remove('active'));
    const idx = keys.indexOf(c);
    if (tabs.children[idx]) tabs.children[idx].classList.add('active');
    currentCat = c; reasonFilter = 'all'; render(c); openPlayer(ev);
    const cam = new URLSearchParams(location.search).get('cam');
    if (cam) {
      const cell = document.querySelector(`#vgrid .vcell[data-cam="${cam}"]`);
      if (cell) focusCell(cell);
    }
    if (q.get('exp') === '1') setTimeout(openExport, 600);
    return;
  }
}

function sigOf(d) {
  return Object.keys(d).map(c => c + ':' + d[c].map(e => e.name + '/' + e.videos.length).join(',')).join('|');
}

/* 实时同步状态 + 新片段自动出现 */
async function refreshLive() {
  let live = null;
  try { live = await (await fetch('/api/live')).json(); } catch (e) { return; }
  const dot = document.getElementById('livedot'), txt = document.getElementById('livetxt');
  if (live.running) {
    dot.style.background = 'var(--positive)';
    if (live.pending > 0) txt.textContent = `${t('实时同步中')} · ${live.pending} ${t('待确认')}`;
    else if (live.ago !== null && live.ago !== undefined) txt.textContent = `${t('实时同步中')} · ${live.ago}s`;
    else txt.textContent = t('实时同步中');
  } else {
    dot.style.background = 'var(--muted2)';
    txt.textContent = t('同步服务未运行');
  }
  // 车载U盘存储条 (镜像未挂载时回落显示归档盘)
  const sw = document.getElementById('storwrap');
  if (live.disk && live.disk.total > 0) {
    const GB = 1073741824, d = live.disk;
    sw.style.display = '';
    document.getElementById('storlab').textContent = t(d.src === 'usb' ? '车载U盘存储' : '盒子归档存储');
    document.getElementById('storval').textContent =
      (d.used / GB).toFixed(1) + 'GB / ' + (d.total / GB).toFixed(1) + 'GB';
    const pct = Math.min(100, d.used / d.total * 100);
    const fill = document.getElementById('storfill');
    fill.style.width = pct.toFixed(1) + '%';
    fill.style.background = pct >= 90 ? 'var(--accent)' : 'var(--muted2)';
  } else {
    sw.style.display = 'none';
  }
  // 事件有变化时无缝重绘 (不打断播放器)
  try {
    const d = await (await fetch('/api/events')).json();
    const s = sigOf(d);
    if (s !== lastSig) {
      lastSig = s; data = d;
      const tabs = document.getElementById('tabs');
      [...tabs.children].forEach((b, i) => {
        const c = Object.keys(d)[i];
        if (c) b.innerHTML = tabContent(c, d[c].length);
      });
      document.getElementById('stat').textContent = `${t('归档')} ${Object.values(d).reduce((a, v) => a + v.length, 0)} ${t('条')}`;
      if (!document.getElementById('player').classList.contains('open')) render(currentCat);
    }
  } catch (e) {}
}
refreshLive();
setInterval(refreshLive, 10000);

/* 缩略图: 服务端抽帧的静态图, 原生懒加载; 不再用 <video> 预览 (会把设备和解码器一起拖死) */
function reasonOf(ev) {
  return (ev.meta && ev.meta.reason) || 'unknown';
}
/* 事件目录名以 YYYY-MM-DD_HH-MM-SS 开头, 取前 10 位作为日期 */
/* 哨兵事件: reason 以 sentry_ 开头; camera 字段只有哨兵触发才有意义 */
function isSentryEv(md) {
  return String((md || {}).reason || '').indexOf('sentry_') === 0;
}
/* event.json 的 camera 编号 -> 片段文件后缀 (社区公认映射: 0前 3左前 4右前 5左B 6右B 7后) */
const CAM_IDX = {0:'front', 3:'left_pillar', 4:'right_pillar', 5:'left_repeater', 6:'right_repeater', 7:'back'};
/* 片段组 key (YYYY-MM-DD_HH-MM-SS) -> 墙钟毫秒; 车机片段名与 event.json timestamp 同一时基, 直接比较 */
function clipEpoch(k) {
  const m = String(k || '').match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() : 0;
}
/* 哨兵封面: 触发摄像头 + 触发那一帧; 找不到对应片段/摄像头返回 null 回落默认封面 */
function sentryThumbSrc(ev, groups) {
  const md = ev.meta || {};
  const camKey = CAM_IDX[+md.camera];
  const m = String(md.timestamp || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!camKey || !m) return null;
  const tg = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
  if (!tg) return null;
  for (const g of groups) {                       // groups 已按片段开始时间升序
    const s = clipEpoch(g.key);
    if (!s || tg < s || tg > s + 70e3) continue;  // 触发时刻落在这 1 分钟片段内
    const v = g.vids.find(x => x.cam === camKey);
    if (!v) return null;                          // 触发摄像头该分钟没录到, 回落
    return v.url.replace('/media/', '/thumb/') + '?t=' + Math.max(0, Math.round((tg - s) / 1000));
  }
  return null;
}
function dateOf(ev) {
  const m = String(ev.name || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[1] + '-' + m[2] + '-' + m[3] : '';
}
/* 官方App哨兵分类: 警觉 / 车门拉动 / 遭遇袭击 (reason 前缀映射, 未知 sentry_ 值归入警觉) */
function reasonTab(r) {
  if (r.indexOf('sentry_panic') === 0 || r.indexOf('sentry_aware_accel') === 0) return 'panic';
  if (r.indexOf('sentry_aware_door') === 0 || r.indexOf('sentry_aware_handle') === 0) return 'door';
  return 'aware';
}
const SENTRY_TABS = [['all', '全部'], ['aware', '哨兵警觉'], ['door', '车门拉动'], ['panic', '遭遇袭击']];
/* 筛选匹配: 哨兵分类按官方三分类的 key 匹配, 其他分类按原始 reason 精确匹配 */
function matchReason(cat, ev) {
  if (reasonFilter === 'all') return true;
  if (cat === 'SentryClips') return reasonTab(reasonOf(ev)) === reasonFilter;
  return reasonOf(ev) === reasonFilter;
}
/* 两行筛选: 触发原因 / 日期, 互为主次 (计数按对方的筛选结果算) */
function renderFilters(cat) {
  const evs = data[cat] || [];
  const rbox = document.getElementById('filters'), dbox = document.getElementById('dfilters');
  const byDate = dateFilter === 'all' ? evs : evs.filter(ev => dateOf(ev) === dateFilter);
  const byReason = reasonFilter === 'all' ? evs : evs.filter(ev => matchReason(cat, ev));

  // 原因筛选在新分类里可能不存在, 自动回落避免"空列表卡死"; 日期允许选到没有事件的那天(会显示空态提示)
  if (reasonFilter !== 'all' && !evs.some(ev => matchReason(cat, ev))) reasonFilter = 'all';

  if (cat === 'SentryClips') {
    // 官方App样式: 固定分类行常驻, 计数为 0 也显示 (和车机/官方App一致)
    const cnt = { all: byDate.length };
    byDate.forEach(ev => { const t = reasonTab(reasonOf(ev)); cnt[t] = (cnt[t] || 0) + 1; });
    rbox.className = 'filters show';
    rbox.innerHTML = SENTRY_TABS.map(([k, label]) =>
      `<button class="chip${k === reasonFilter ? ' on' : ''}" onclick="setReason('${k}')">${t(label)}<span class="n">${cnt[k] || 0}</span></button>`
    ).join('');
  } else {
    const rcnt = {};
    byDate.forEach(ev => { const r = reasonOf(ev); rcnt[r] = (rcnt[r] || 0) + 1; });
    const rkeys = Object.keys(rcnt).sort((a, b) => rcnt[b] - rcnt[a]);
    if (rkeys.length < 2) { rbox.className = 'filters'; rbox.innerHTML = ''; }
    else {
      rbox.className = 'filters show';
      const items = [['all', t('全部'), byDate.length]].concat(
        rkeys.map(k => [k, reasonLabel(k) !== k ? reasonLabel(k) : t('未标注原因'), rcnt[k]]));
      rbox.innerHTML = '<span class="flab">' + t('原因') + '</span>' + items.map(([k, label, n]) =>
        `<button class="chip${k === reasonFilter ? ' on' : ''}" onclick="setReason('${k}')">${label}<span class="n">${n}</span></button>`
      ).join('');
    }
  }

  const dcnt = {};
  byReason.forEach(ev => { const d = dateOf(ev); if (d) dcnt[d] = (dcnt[d] || 0) + 1; });
  if (dateFilter !== 'all' && !/^\d{4}-\d{2}-\d{2}$/.test(dateFilter)) dateFilter = 'all';
  const alldays = [...new Set(evs.map(dateOf).filter(Boolean))].sort().reverse();
  let days = alldays.filter(d => dcnt[d] > 0 || d === dateFilter);
  // 自选日期可能当天没有事件, 仍然把选中的那天显示出来, 方便点回「全部日期」
  if (dateFilter !== 'all' && !days.includes(dateFilter)) days = [dateFilter].concat(days);
  if (!alldays.length && dateFilter === 'all') { dbox.className = 'filters'; dbox.innerHTML = ''; return; }
  dbox.className = 'filters show';
  const items = [['all', t('全部日期'), byReason.length]].concat(
    days.map(d => [d, d.slice(5).replace('-', '/'), dcnt[d] || 0]));
  dbox.innerHTML = '<span class="flab">' + t('日期') + '</span>' + items.map(([k, label, n]) =>
    `<button class="chip d${k === dateFilter ? ' on' : ''}" onclick="setDate('${k}')">${label}<span class="n">${n}</span></button>`
  ).join('') + '<button class="chip pick" onclick="pickDate()" title="' + t('选择日期…') + '">' + icon('calendar') + t('选择日期…') + '</button>';
}
/* 原生日期选择器: 分类里只有一天时也能翻到别的日期 */
function pickDate() {
  const inp = document.getElementById('dpick');
  if (!inp) return;
  const t = new Date();
  inp.max = t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' +
    String(t.getDate()).padStart(2, '0');
  inp.value = dateFilter !== 'all' ? dateFilter : '';
  try { if (inp.showPicker) { inp.showPicker(); return; } } catch (e) {}
  inp.click();
}
function setReason(k) {
  reasonFilter = k;
  render(currentCat);
}
function setDate(k) {
  dateFilter = k;
  render(currentCat);
}
function clearFilters() {
  reasonFilter = 'all'; dateFilter = 'all';
  render(currentCat);
}
/* 缩略图失败多半是临时的 (磁盘满/片段刚同步完还没落稳), 自动重试两次再显示占位图 */
function thumbErr(img) {
  const n = +(img.dataset.r || 0);
  if (n < 2) {
    img.dataset.r = n + 1;
    setTimeout(() => { img.src = img.src.split('?')[0] + '?r=' + (n + 1); }, n ? 8000 : 2500);
    return;
  }
  img.parentNode.innerHTML = '<div class="ph">' + icon('video') + '<span>' + t('暂无缩略图') + '</span></div>';
}
/* 日期分组小标题: 今天/昨天/M月D日 */
function dheadLabel(d) {
  const now = new Date(), today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
  const y = new Date(now.getTime() - 864e5), yd = y.getFullYear() + '-' + String(y.getMonth() + 1).padStart(2, '0') + '-' + String(y.getDate()).padStart(2, '0');
  const m = d.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return d;
  if (d === today) return t('今天');
  if (d === yd) return t('昨天');
  if (LANG === 'zh') return (+m[2]) + '月' + (+m[3]) + '日';
  try { return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(LANG, { month: 'short', day: 'numeric' }); }
  catch (e) { return d.slice(5); }
}
function render(cat) {
  const g = document.getElementById('grid'), e = document.getElementById('empty');
  g.innerHTML = '';
  renderFilters(cat);
  let evs = data[cat] || [];
  evs = evs.filter(ev => matchReason(cat, ev));
  if (dateFilter !== 'all') evs = evs.filter(ev => dateOf(ev) === dateFilter);
  document.getElementById('category-title').textContent = t(CATS_NAME[cat] || cat);
  document.getElementById('result-count').textContent = evs.length + ' ' + t('条事件');
  document.getElementById('clear-filters').hidden = reasonFilter === 'all' && dateFilter === 'all';
  e.style.display = evs.length ? 'none' : 'block';
  if (!evs.length) {
    e.innerHTML = (reasonFilter !== 'all' || dateFilter !== 'all')
      ? '<div class="empty-symbol">' + icon('search') + '</div><h3>' + t('该筛选下暂无事件') + '</h3><p>' + t('换个日期或点「全部」查看本分类所有事件') + '</p>'
      : EMPTY_HTML;
  }
  let prevDay = null;
  for (const ev of evs) {
    const day = dateOf(ev);
    if (day && day !== prevDay) {           // 日期分组小标题 (像官方App的"下午 14:00-17:59 · 3条")
      const n = evs.filter(x => dateOf(x) === day).length;
      const h = document.createElement('div'); h.className = 'dhead';
      h.innerHTML = '<b>' + dheadLabel(day) + '</b><span class="n">' + n + ' ' + t('条') + '</span>';
      g.appendChild(h);
      prevDay = day;
    }
    const card = document.createElement('article'); card.className = 'card';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    const groups = groupVideos(ev.videos);
    const vids = camSort(groups.length ? groups[groups.length - 1].vids : []);
    let th;
    if (vids.length) {
      /* 哨兵事件: 用触发摄像头在触发那一帧当封面; 其他/回落: 前摄像头第1秒 */
      const src = sentryThumbSrc(ev, groups) ||
        (vids.find(v => v.cam === 'front') || vids[0]).url.replace('/media/', '/thumb/');
      th = `<div class="thumb"><img loading="lazy" decoding="async" src="${esc(src)}" alt="${t('事件缩略图')}" onerror="thumbErr(this)"><span class="play-spot">${icon('play')}</span><span class="cnum">${icon('video')}${vids.length} ${t('路')}</span></div>`;
    } else {
      th = '<div class="thumb"><div class="ph">' + icon('video') + '<span>' + t('暂无缩略图') + '</span></div></div>';
    }
    const mm = ev.meta || {};
    /* 标题: 触发原因·触发摄像头 (哨兵), 否则分类名 */
    let title = t(CATS_NAME[cat] || cat);
    if (mm.reason) {
      title = reasonLabel(mm.reason) || mm.reason;
      if (isSentryEv(mm) && mm.camera !== undefined && mm.camera !== null) {
        const ck = CAM_IDX[+mm.camera];
        if (ck) title += ' · ' + camLabel(ck);
      }
    }
    const tm = (ev.name.match(/_(\d{2}-\d{2})/) || [,''])[1].replace('-', ':');
    const tmTxt = tm ? tm : fmtName(ev.name);
    const locationText = [mm.city, mm.street].filter(Boolean).join(' · ') || t('位置未知');
    const dayTxt = day ? day.slice(5).replace('-', '/') : '';
    const mb = (Number(ev.size) || 0) / 1048576;
    const szTxt = mb >= 1 ? mb.toFixed(1) + ' MB' : (mb * 1024).toFixed(0) + ' KB';
    card.innerHTML = th + `<div class="meta"><div class="event-topline"><time class="event-time">${esc(tmTxt)}</time><span>${esc(dayTxt)}</span><span class="event-arrow">${icon('arrowUpRight')}</span></div><h3 class="event-title" title="${esc(title)}">${esc(title)}</h3><div class="event-location">${icon('location')}<span>${esc(locationText)}</span></div><div class="event-footer"><span>${vids.length} ${t('路画面')} · ${szTxt}</span><span class="view-event">${t('查看事件')}${icon('chevron')}</span></div></div>`;
    card.setAttribute('aria-label', tmTxt + ' ' + title + ' ' + locationText);
    card.onclick = () => openPlayer(ev);
    card.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPlayer(ev); } };
    g.appendChild(card);
  }
  applyLang();
}

function layoutPlayer() {
  const vg = document.getElementById('vgrid');
  if (vg.querySelector('.vcell.focused')) return;  // 单路放大时保持满屏
  const n = vg.children.length;
  if (!n) return;
  const d = gridDims(n);
  vg.style.gridTemplateColumns = `repeat(${d[0]},1fr)`;
  // 显式像素行高: 不依赖 CSS aspect-ratio (老版 iOS Safari 不支持时格子会塌陷/视频层溢出遮挡)
  const w = vg.clientWidth || 0;
  if (w > 0) {
    const gap = 3;
    const cw = (w - gap * (d[0] - 1)) / d[0];
    vg.style.gridAutoRows = Math.round(cw * 9 / 16) + 'px';
  }
}
/* 点击任意一路: 单路铺满; 再点一次(或 Esc)恢复多路 */
function focusCell(cell) {
  const vg = document.getElementById('vgrid');
  const cells = [...vg.children];
  const wasFocused = cell.classList.contains('focused');
  cells.forEach(c => { c.classList.remove('focused'); c.style.display = ''; });
  if (wasFocused) { layoutPlayer(); return; }
  cell.classList.add('focused');
  cells.forEach(c => { if (c !== cell) c.style.display = 'none'; });
  vg.style.gridTemplateColumns = '1fr';
  vg.style.gridTemplateRows = '1fr';
}
function openPlayer(ev) {
  cur = ev;
  curClips = groupVideos(ev.videos);
  curClipIx = curClips.length ? curClips.length - 1 : 0;
  renderClip();
}
function renderClip() {
  const ev = cur;
  const clip = curClips[curClipIx] || { key: '', vids: [] };
  const vg = document.getElementById('vgrid'); vg.innerHTML = '';
  camSort(clip.vids).forEach(v => {
    const cell = document.createElement('div'); cell.className = 'vcell';
    cell.dataset.cam = v.cam || '';
    const vid = document.createElement('video');
    vid.src = qUrl(v.url); vid.muted = true; vid.defaultMuted = true; vid.preload = 'auto';
    /* 每格加载状态: 转码/缓冲时显示提示, 避免黑屏让人以为坏了 */
    const cl = document.createElement('div'); cl.className = 'cload';
    cl.innerHTML = '<span class="spin"></span><span>缓冲中…</span>';
    const clDone = () => { cl.style.display = 'none'; };
    vid.addEventListener('loadeddata', clDone);
    vid.addEventListener('playing', clDone);
    vid.addEventListener('error', () => {
      cl.className = 'cload err';
      cl.innerHTML = '<span>' + t('加载失败, 点其他片段再试') + '</span>';
    });
    vid.addEventListener('waiting', () => { cl.style.display = ''; });
    /* 低清晰度首次播放需要后台转码, 缓冲时给出提示 */
    vid.addEventListener('waiting', () => {
      if (curQ !== 'orig') {
        const h = document.getElementById('qhint');
        h.textContent = t('正在缓冲 (首次播放新片段需后台转码十几秒, 转好一次后秒开)…');
        h.style.display = 'block';
      }
    });
    vid.addEventListener('canplay', () => { document.getElementById('qhint').style.display = 'none'; });
    vid.setAttribute('muted', ''); vid.setAttribute('playsinline', '');
    vid.setAttribute('webkit-playsinline', ''); vid.setAttribute('x5-playsinline', '');
    const lb = document.createElement('span'); lb.className = 'clabel'; lb.textContent = camShort(v.cam);
    cell.appendChild(vid); cell.appendChild(cl); cell.appendChild(lb);
    cell.onclick = () => focusCell(cell);
    vg.appendChild(cell);
  });
  document.getElementById('ic-time').textContent = fmtFull(ev.name);
  const pt = document.getElementById('ptitle');
  if (pt) pt.textContent = t(CATS_NAME[currentCat] || currentCat) + ' · ' + fmtFull(ev.name);
  document.getElementById('ic-cat').textContent = t(CATS_NAME[currentCat] || currentCat);
  const md0 = ev.meta || {};
  document.getElementById('ic-big').textContent =
    (md0.reason && (reasonLabel(md0.reason) || md0.reason)) || CATS_NAME[currentCat] || currentCat;
  const mb = (ev.size || 0) / 1048576;
  document.getElementById('ic-sub').innerHTML = clip.vids.length + ' 路画面 · <span id="ic-dur">--:--</span>' + (mb >= 0.05 ? ' · ' + (mb >= 1 ? mb.toFixed(1) + ' MB' : Math.round(mb * 1024) + ' KB') : '');
  const cb = document.getElementById('clipbar');
  if (curClips.length > 1) {
    cb.classList.add('show');
    cb.innerHTML = '<span class="ick" style="min-width:auto;align-self:center">片段</span>' +
      curClips.map((c, i) => {
        const t = (c.key.split('_')[1] || c.key).replace(/-/g, ':');
        return `<button class="${i === curClipIx ? 'on' : ''}" onclick="switchClip(${i})">${t}</button>`;
      }).join('');
  } else {
    cb.classList.remove('show');
    cb.innerHTML = '';
  }
  const md = ev.meta || {};
  let rh = '';
  rh += '<div data-k="time"><span class="ick">时间</span>' + (md.timestamp ? md.timestamp.replace('T', ' ') : fmtFull(ev.name)) + '</div>';
  if (md.city) rh += '<div data-k="city"><span class="ick">城市</span>' + md.city + '</div>';
  if (md.street) rh += '<div data-k="street"><span class="ick">位置</span>' + md.street + '</div>';
  if (md.est_lat && md.est_lon) rh += '<div data-k="coord"><span class="ick">坐标</span>' + md.est_lat + ', ' + md.est_lon + '</div>';
  if (md.reason) rh += '<div><span class="ick">触发原因</span>' + (reasonLabel(md.reason) || md.reason) + '</div>';
  /* camera 字段只有哨兵事件有意义; 手动保存等也会带 camera:"0" */
  if (md.camera && isSentryEv(md)) { const cn = CAM_IDX[+md.camera]; rh += '<div data-k="camera"><span class="ick">触发画面</span>' + (cn ? camLabel(cn) : '摄像头') + ' #' + md.camera + '</div>'; }
  if (md.campaign_id) rh += '<div><span class="ick">活动ID</span>' + md.campaign_id + '</div>';
  rh += '<div><span class="ick">画面</span>' + clip.vids.length + ' 路</div>';
  if (curClips.length > 1) rh += '<div><span class="ick">片段</span>共 ' + curClips.length + ' 段, 当前第 ' + (curClipIx + 1) + ' 段</div>';
  rh += camSort(clip.vids).map(v => '<div><span class="ick">' + camLabel(v.cam) + '</span>' + v.url.split('/').pop() + '</div>').join('');
  if (!md.timestamp) rh += '<div><span class="ick">元数据</span>无 event.json（该事件未写入元数据）</div>';
  const rows = document.getElementById('ic-rows');
  rows.innerHTML = rh;
  rows.classList.remove('open');
  document.getElementById('ic-fold').textContent = '展开详细数据 ⌄';
  layoutPlayer();
  document.getElementById('player').classList.add('open');
  requestAnimationFrame(layoutPlayer);  // 可见后再算一次, 保证拿到真实宽度设置像素行高
  const sm = document.getElementById('startmask');
  if ('ontouchstart' in window) { sm.classList.add('show'); }
  playAll();
  if (syncTimer) clearInterval(syncTimer);
  syncTimer = setInterval(tick, 250);
  const sortedVids = camSort(clip.vids);
  loadTelemetry(sortedVids.length ? sortedVids[0].url : null, md);
}

/* ---------- 行车遥测面板 (SEI, 视频上方) ---------- */
let telT0 = null, TEL_META = {};
function loadTelemetry(url, meta) {
  TEL = null;
  TEL_META = meta || {};
  const p = document.getElementById('tpanel'), card = document.getElementById('infocard');
  p.classList.remove('show', 'plain');
  if (card) card.classList.remove('plain');
  p.classList.add('folded');   // 详细数据默认折叠
  document.getElementById('tp-fold').textContent = '展开详细数据 ⌄';
  document.getElementById('tp-loc').textContent =
    [TEL_META.city, TEL_META.street].filter(Boolean).join(' · ');
  // 触发画面摄像头: 只有哨兵事件显示 (手动保存等也会带无意义的 camera:"0")
  const camTag = document.getElementById('tp-tag');
  if (TEL_META.camera && isSentryEv(TEL_META)) {
    const cn = CAM_IDX[+TEL_META.camera];
    camTag.innerHTML = '触发画面 <b>' + (cn ? camLabel(cn) : '摄像头') + ' #' + TEL_META.camera + '</b>';
    camTag.classList.add('show');
  } else {
    camTag.classList.remove('show');
    camTag.innerHTML = '';
  }
  telT0 = null;
  if (!url) return;
  const m = url.match(/(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/);
  if (m) telT0 = new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]);
  // 顶部时间: 优先 event.json 的 timestamp, 否则退回片段文件名里的时间
  const ts = String(TEL_META.timestamp || '').replace('T', ' ').replace(/\.\d+/, '').replace(/Z$/, '').slice(0, 19);
  document.getElementById('tp-date').textContent = ts || (telT0 ? fmtTelDate(telT0) : '—');
  const fp = url.replace('/media/', '/archive/');
  fetch('/api/telemetry?path=' + encodeURIComponent(fp))
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.frames || !d.frames.length) { showPlainPanel(); return; }
      TEL = d;
      p.classList.add('show');
      renderHud(d.frames[0]);
      // 事件信息已搬到上方遥测面板, 下方卡片去掉重复项
      ['time', 'city', 'street', 'coord', 'camera'].forEach(k => {
        const el = document.querySelector('#ic-rows > div[data-k="' + k + '"]');
        if (el) el.remove();
      });
      document.getElementById('ic-rows').insertAdjacentHTML('beforeend',
        '<div><span class="ick">刹车占比</span>' + d.brake_ratio + '%</div>' +
        '<div><span class="ick">遥测</span>SEI 元数据 · ' + d.count + ' 帧 @ ' + d.fps + 'fps</div>');
    }).catch(() => showPlainPanel());
}
/* 哨兵/停车片段没有 SEI 遥测: 顶部仍然显示 时间 + 城市·位置, 只是不带车速/挡位 */
function showPlainPanel() {
  const p = document.getElementById('tpanel'), card = document.getElementById('infocard');
  p.classList.add('show', 'plain');
  if (card) card.classList.add('plain');
  ['time', 'city', 'street', 'camera'].forEach(k => {
    const el = document.querySelector('#ic-rows > div[data-k="' + k + '"]');
    if (el) el.remove();
  });
  layoutPlayer();
}
function fmtTelDate(dt) {
  const z = n => (n < 10 ? '0' : '') + n;
  return dt.getFullYear() + '/' + z(dt.getMonth()+1) + '/' + z(dt.getDate()) + ' ' +
    z(dt.getHours()) + ':' + z(dt.getMinutes()) + ':' + z(dt.getSeconds());
}
function renderHud(f) {
  // f: 0=t 1=挡位 2=车速km/h 3=油门% 4=方向盘角度 5=刹车 6=转向灯 7=AP 8=lat 9=lon 10=heading 11=Ax 12=Ay 13=Az
  const q = id => document.getElementById(id);
  q('tp-gear').textContent = f[1];
  q('tp-spd').textContent = Math.round(f[2]);
  q('tp-date').textContent = telT0 ? fmtTelDate(new Date(telT0.getTime() + f[0]*1000)) : '—';
  q('tp-thr-n').textContent = f[3] + '%';
  q('tp-brk').classList.toggle('on', !!f[5]);   // 刹车只有布尔量, 不给百分比
  q('tp-bl').classList.toggle('on', f[6] === 1);
  q('tp-br').classList.toggle('on', f[6] === 2);
  const apNames = { 1: 'FSD', 2: 'AP', 3: 'TACC' };
  const ap = q('tp-ap');
  if (f[7]) { ap.style.display = ''; ap.textContent = apNames[f[7]] || 'AP'; }
  else ap.style.display = 'none';
  q('tp-st').title = '方向盘角度 ' + f[4] + '°';
  const md = TEL_META || {};
  q('tp-rows').innerHTML =
    (md.est_lat && md.est_lon ? '<div><span class="k">坐标</span>' + md.est_lat + ', ' + md.est_lon + '</div>' : '') +
    '<div><span class="k">方向盘角度</span>' + f[4] + '°</div>' +
    '<div><span class="k">横向加速度 Ax</span>' + f[11].toFixed(2) + ' m/s²</div>' +
    '<div><span class="k">前后加速度 Ay</span>' + f[12].toFixed(2) + ' m/s²</div>' +
    '<div><span class="k">垂直加速度 Az</span>' + f[13].toFixed(2) + ' m/s²</div>';
}
function toggleTelRows() {
  const p = document.getElementById('tpanel'), b = document.getElementById('tp-fold');
  p.classList.toggle('folded');
  b.textContent = p.classList.contains('folded') ? '展开详细数据 ⌄' : '收起详细数据 ⌃';
  layoutPlayer();
}

/* ---------- 导出视频水印 (与遥测面板同款) ---------- */
function telFrameAt(t) {
  const fr = TEL.frames;
  let lo = 0, hi = fr.length - 1, ans = fr[0];
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (fr[mid][0] <= t) { ans = fr[mid]; lo = mid + 1; } else { hi = mid - 1; }
  }
  return ans;
}
function rrPath(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function wmPill(ctx, xRight, yMid, text, bg, fg, s) {
  const fs = Math.round(12 * s);
  ctx.font = '700 ' + fs + 'px system-ui,"Segoe UI",sans-serif';
  const pw = ctx.measureText(text).width + Math.round(18 * s), ph = Math.round(24 * s);
  const x = xRight - pw;
  const sh = ctx.shadowBlur;
  ctx.shadowBlur = 0;               // 胶囊是实心色块, 不需要投影
  ctx.fillStyle = bg;
  rrPath(ctx, x, yMid - ph / 2, pw, ph, ph / 2); ctx.fill();
  ctx.shadowBlur = sh;
  ctx.fillStyle = fg;
  ctx.fillText(text, x + Math.round(9 * s), yMid + Math.round(fs * 0.36));
  return x;
}
function drawWm(ctx, t, W) {
  if (!TEL || !TEL.frames || !TEL.frames.length) return;
  const f = telFrameAt(t);
  const s = W / 1280, pad = Math.round(14 * s);
  const h = Math.round(46 * s), yc = h / 2;   // 单行窄条: 1280 宽时约 46px, 仅占画面高度 ~5%
  ctx.save();
  ctx.textBaseline = 'middle';
  // 极淡底 (15% 不透明): 只做一块轻微压暗, 天空/路面几乎不受影响
  ctx.shadowBlur = 0;
  ctx.fillStyle = 'rgba(13,18,26,.15)';
  ctx.fillRect(0, 0, W, h);
  // 文字加深色投影, 保证在明亮天空上也能看清
  ctx.shadowColor = 'rgba(0,0,0,.8)';
  ctx.shadowBlur = Math.round(5 * s);
  ctx.shadowOffsetY = Math.round(1.2 * s);
  // 左: 日期时间 + 城市/位置
  ctx.textAlign = 'left';
  ctx.font = '600 ' + Math.round(14 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#fff';
  const dateStr = telT0 ? fmtTelDate(new Date(telT0.getTime() + f[0] * 1000)) : '';
  ctx.fillText(dateStr, pad, yc + Math.round(1 * s));
  const dw = ctx.measureText(dateStr).width;
  const loc = [(TEL_META || {}).city, (TEL_META || {}).street].filter(Boolean).join(' · ');
  if (loc) {
    ctx.font = '500 ' + Math.round(13 * s) + 'px system-ui,"Segoe UI",sans-serif';
    ctx.fillStyle = '#eef3f8';
    ctx.fillText(loc, pad + dw + Math.round(10 * s), yc + Math.round(1 * s));
  }
  // 右侧自右向左: 油门 / 刹车 / 挡位 / 转向灯 / 车速
  let xr = W - pad;
  xr = wmPill(ctx, xr, yc, f[3] + '%', '#1f7ae0', '#fff', s) - Math.round(8 * s);
  xr = wmPill(ctx, xr, yc, '刹车',
    f[5] ? '#ff453a' : 'rgba(32,40,50,.5)', f[5] ? '#fff' : '#eef3f8', s) - Math.round(12 * s);
  // 挡位圆徽
  ctx.shadowBlur = 0;

  const gr = Math.round(13 * s);
  ctx.fillStyle = '#34c759';
  ctx.beginPath(); ctx.arc(xr - gr, yc, gr, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
  ctx.font = '800 ' + Math.round(14 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillText(f[1], xr - gr, yc + Math.round(1 * s));
  xr -= gr * 2 + Math.round(10 * s);
  // 转向灯三角 (右/左)
  const triS = Math.round(9 * s);
  ctx.fillStyle = f[6] === 2 ? '#f59e0b' : '#4a545f';
  ctx.beginPath(); ctx.moveTo(xr - triS, yc - triS * 0.62); ctx.lineTo(xr - triS, yc + triS * 0.62); ctx.lineTo(xr, yc); ctx.closePath(); ctx.fill();
  xr -= triS + Math.round(7 * s);
  ctx.fillStyle = f[6] === 1 ? '#f59e0b' : '#4a545f';
  ctx.beginPath(); ctx.moveTo(xr, yc - triS * 0.62); ctx.lineTo(xr, yc + triS * 0.62); ctx.lineTo(xr - triS, yc); ctx.closePath(); ctx.fill();
  xr -= triS + Math.round(12 * s);
  // 车速 (先画右侧单位, 再画数字, 保证 "数字 km/h" 顺序)
  ctx.shadowBlur = Math.round(4 * s);   // 文字恢复投影
  ctx.textAlign = 'right';
  ctx.font = '500 ' + Math.round(11 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#eef3f8';
  ctx.fillText('km/h', xr, yc + Math.round(1 * s));
  xr -= ctx.measureText('km/h').width + Math.round(6 * s);
  ctx.font = '800 ' + Math.round(22 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#fff';
  const sp = String(Math.round(f[2]));
  ctx.fillText(sp, xr, yc + Math.round(1 * s));
  ctx.restore();
}
function updateHud(t) {
  if (!TEL || !TEL.frames || !TEL.frames.length) return;
  const fr = TEL.frames;
  let lo = 0, hi = fr.length - 1, ans = fr[0];
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (fr[mid][0] <= t) { ans = fr[mid]; lo = mid + 1; } else { hi = mid - 1; }
  }
  renderHud(ans);
}
window.addEventListener('resize', () => { if (document.getElementById('player').classList.contains('open')) layoutPlayer(); });

/* ---------- 准实时预览 ---------- */
let liveCam = localStorage.getItem('tsflivecam') || 'front';
let liveFolder = '', liveTimer = null, liveBusy = false, liveLoading = false, liveSyncTimer = null, liveEv = null;
function latestLiveEv() { const arr = (data && data.RecentClips) || []; return arr[0] || null; }
function openLive() {
  document.getElementById('livep').classList.add('open');
  const ev = latestLiveEv();
  if (ev) { liveFolder = ev.name; renderLive(ev); }
  liveTick();
  liveRestart();
}
function closeLive() {
  document.getElementById('livep').classList.remove('open');
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  clearLiveGrid();
  const v = document.getElementById('lv-video');
  try { v.pause(); v.removeAttribute('src'); v.load(); } catch (err) {}
  /* 从实时预览返回时, 高亮恢复到当前分类页签 (不是固定第一个) */
  const tl = document.getElementById('tab-live');
  if (tl && tl.classList.contains('active')) {
    tl.classList.remove('active');
    const all = document.querySelectorAll('#tabs .tab');
    const ix = Object.keys(data || {}).indexOf(currentCat);
    const target = all[ix >= 0 ? ix : 0];
    if (target && target !== tl) target.classList.add('active');
  }
}
function renderLive(ev) {
  liveEv = ev;
  document.getElementById('lv-name').textContent = '最新片段: ' + ev.name.replace(/_/g, ' ');
  const vids = camSort(ev.videos || []);
  if (!vids.length) return;
  if (liveCam !== '__all' && !vids.find(v => v.cam === liveCam)) liveCam = '__all';
  const row = document.getElementById('lv-cams');
  row.innerHTML = '';
  const mkBtn = (cam, label) => {
    const b = document.createElement('button');
    b.className = 'lvbtn' + (cam === liveCam ? ' on' : '');
    b.textContent = label;
    b.onclick = () => { liveCam = cam; localStorage.setItem('tsflivecam', liveCam); renderLive(ev); };
    row.appendChild(b);
  };
  vids.forEach(v => mkBtn(v.cam, camLabel(v.cam)));
  /* 最后一个: 全览 (所有摄像头同屏) */
  mkBtn('__all', '全览 (' + vids.length + ')');
  const vid = document.getElementById('lv-video');
  const grid = document.getElementById('lv-grid');
  if (liveCam === '__all') {
    vid.style.display = 'none';
    try { vid.pause(); vid.removeAttribute('src'); vid.load(); } catch (err) {}
    buildLiveGrid(vids);
  } else {
    clearLiveGrid();
    vid.style.display = '';
    const v = vids.find(x => x.cam === liveCam) || vids[0];
    if (v && vid.src.indexOf(v.url) === -1) { vid.src = v.url; vid.play().catch(() => {}); }
  }
}
/* 全览: 每路一个 video, 同屏播放并互相对齐进度 */
function clearLiveGrid() {
  if (liveSyncTimer) { clearInterval(liveSyncTimer); liveSyncTimer = null; }
  const g = document.getElementById('lv-grid');
  g.classList.remove('on', 'n1');
  [...g.querySelectorAll('video')].forEach(v => { try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) {} });
  g.innerHTML = '';
}
function buildLiveGrid(vids) {
  const g = document.getElementById('lv-grid');
  [...g.querySelectorAll('video')].forEach(v => { try { v.pause(); v.removeAttribute('src'); } catch (e) {} });
  g.innerHTML = '';
  g.classList.add('on');
  g.classList.toggle('n1', vids.length === 1);
  vids.forEach((v, ix) => {
    const cell = document.createElement('div');
    cell.className = 'lvcell';
    const el = document.createElement('video');
    el.playsInline = true; el.muted = true; el.preload = 'auto';
    el.onloadedmetadata = () => { el.currentTime = 0.05; el.play().catch(() => {}); };
    const lb = document.createElement('div');
    lb.className = 'clabel';
    lb.textContent = camLabel(v.cam);
    cell.appendChild(el); cell.appendChild(lb);
    cell.onclick = () => { liveCam = v.cam; localStorage.setItem('tsflivecam', liveCam); if (liveEv) renderLive(liveEv); };
    g.appendChild(cell);
    /* 逐路上屏: 每路错开 350ms, 避免 4 路同时抢占上行 */
    el.oncanplay = null;
    setTimeout(() => { if (!el.getAttribute('src')) el.src = v.url; }, ix * 350);
  });
  /* 每秒对齐一次进度, 任一路拖进度条/暂停 → 其余跟随 */
  if (liveSyncTimer) clearInterval(liveSyncTimer);
  liveSyncTimer = setInterval(() => {
    const vs = [...g.querySelectorAll('video')];
    if (!vs.length) return;
    const lead = vs.find(v => !v.paused && !v.seeking) || vs[0];
    vs.forEach(v => {
      if (v === lead) return;
      if (Math.abs((v.currentTime || 0) - (lead.currentTime || 0)) > 0.45) v.currentTime = lead.currentTime;
    });
  }, 1000);
}
async function liveTick() {
  if (liveBusy || !document.getElementById('livep').classList.contains('open')) return;
  liveBusy = true;
  const st = document.getElementById('lv-status');
  if (!st.textContent) st.textContent = '正在检查…';   /* 首次进入先给占位, 不让状态栏空白 */
  try {
    /* 网络抖动/上行被视频占满时响应可能为空或迟迟不回, 6 秒超时后放行, 下轮再试 */
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 6000);
    let txt;
    try { txt = await fetch('/api/live/status', { signal: ctrl.signal }).then(x => x.text()); }
    finally { clearTimeout(to); }
    let r = null;
    try { r = JSON.parse(txt); } catch (e) {}
    if (!r) { st.textContent = '检查暂时不可达, 下轮重试…'; return; }
    /* 状态文字先更新: 下面的全量列表刷新可能很慢 (设备负载高时数秒), 不能让它挡住状态栏 */
    if (!r.folder) st.textContent = '归档里还没有行车片段';
    else {
      /* age = 入库时刻 - 当前时刻 (负数), -1 表示目录里还没有可读文件 */
      const ago = (typeof r.age === 'number' && r.age < -1) ? -r.age : 0;
      st.textContent = !ago ? '' : ago < 60 ? '刚刚入库'
        : ago < 3600 ? Math.round(ago / 60) + ' 分钟前入库'
        : Math.round(ago / 3600) + ' 小时前入库';
    }
    /* 列表刷新异步进行, 不阻塞轮询; 防止上一次还没完成又发起一次 */
    if (r.folder && r.folder !== liveFolder && !liveLoading) {
      liveFolder = r.folder;
      liveLoading = true;
      load().then(() => {
        const ev = latestLiveEv();
        if (ev && document.getElementById('livep').classList.contains('open')) renderLive(ev);
      }).catch(() => {}).finally(() => { liveLoading = false; });
    }
  } catch (e) { st.textContent = '检查暂时不可达, 下轮重试…'; }
  finally { liveBusy = false; }
}
function liveRestart() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  const iv = +document.getElementById('lv-iv').value;
  if (iv > 0) liveTimer = setInterval(liveTick, iv * 1000);
}

/* ---------- 导出 ---------- */
let expQ = 'orig';
function renderExpQ() {
  const curLabel = (Q_LIST.find(x => x[0] === expQ) || Q_LIST[0])[1];
  document.getElementById('expq').innerHTML =
    `<button class="qbtn ${expQ !== 'orig' ? 'on' : ''}" onclick="toggleExpQ(event)">${curLabel} ⌃</button>` +
    '<div class="qmenu">' + Q_LIST.map(([k, label]) =>
      `<button class="${k === expQ ? 'on' : ''}" onclick="setExpQuality('${k}')">${label}</button>`).join('') + '</div>';
}
function toggleExpQ(e) {
  if (e) e.stopPropagation();
  document.getElementById('expq').classList.toggle('open');
}
function openExport() {
  if (!cur) return;
  const box = document.getElementById('expchks'); box.innerHTML = '';
  camSort(curVids()).forEach(v => {
    const lb = document.createElement('label'); lb.className = 'expitem';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = v.cam; cb.checked = true;
    lb.appendChild(cb);
    lb.appendChild(document.createTextNode(camLabel(v.cam) + ' (' + v.cam + ')'));
    box.appendChild(lb);
  });
  // 水印勾选: 有遥测数据才可用, 默认勾上
  const hasTel = !!(TEL && TEL.frames && TEL.frames.length);
  const wm = document.getElementById('expwm'), wmRow = document.getElementById('expwm-row');
  wm.disabled = !hasTel;
  wm.checked = hasTel;
  wmRow.style.opacity = hasTel ? '' : '.45';
  wmRow.title = hasTel ? '' : '该片段没有行车遥测数据 (SEI)';
  expQ = curQ;  // 默认跟随当前播放清晰度
  renderExpQ();
  document.getElementById('expp').classList.add('open');
}
function closeExport() { document.getElementById('expp').classList.remove('open'); }
function expChk(on) { [...document.querySelectorAll('#expchks input')].forEach(c => c.checked = on); }
function doExport() {
  if (!cur) return;
  const sel = [...document.querySelectorAll('#expchks input:checked')].map(c => c.value);
  if (!sel.length) { toast('请至少选择一路'); return; }
  const telOk = !!(TEL && TEL.frames && TEL.frames.length);
  const wm = document.getElementById('expwm').checked && telOk;
  closeExport();
  if (sel.length === 1 && !wm) {
    const v = curVids().find(x => x.cam === sel[0]);
    if (v) location.href = '/dl?path=' + encodeURIComponent('/archive/' + v.url.replace(/^\/media\//, '')) + (expQ !== 'orig' ? '&q=' + expQ : '');
    return;
  }
  startLocalStitch(sel, document.getElementById('stitch'), wm);
}
function doPushFeishu() {
  if (!cur) return;
  const sel = [...document.querySelectorAll('#expchks input:checked')].map(c => c.value);
  if (!sel.length) { toast('请至少选择一路'); return; }
  const telOk = !!(TEL && TEL.frames && TEL.frames.length);
  const wm = document.getElementById('expwm').checked && telOk;
  closeExport();
  const parts = ((curVids()[0] || {}).url || '').replace(/^\/media\//, '').split('/');
  const cat = parts[0] || '', name = parts[1] || '';
  if (!name) { toast('取不到当前事件信息, 请刷新重试'); return; }
  startLocalStitch(sel, document.getElementById('stitch'), wm, { cat: cat, name: name, q: expQ });
}
function uploadPushVideo(blob, push) {
  const fill = document.getElementById('stfill'), pct = document.getElementById('stpct'),
        hint = document.getElementById('sthint');
  fill.style.width = '60%'; pct.textContent = '';
  hint.textContent = '已上传到设备, 正在转码并推送到已选渠道 (约1-3分钟, 请保持页面打开)…';
  fetch('/api/push/upload?cat=' + encodeURIComponent(push.cat) + '&name=' + encodeURIComponent(push.name) +
        '&q=' + encodeURIComponent(push.q),
        { method: 'POST', body: blob, headers: { 'Content-Type': blob.type || 'video/webm' } })
    .then(r => r.json()).then(d => {
      if (d.ok) {
        fill.style.width = '100%'; pct.textContent = '100%';
        hint.textContent = '✅ ' + d.msg;
        setTimeout(() => document.getElementById('stitch').classList.remove('open'), 6000);
      } else stitchFail(d.msg || d.error || '推送失败');
    })
    .catch(e => stitchFail('推送失败: ' + e));
}
/* 浏览器本机合成: 设备端 ffmpeg 多路 1080p 会触发内核重启, 改由电脑实时录制合成 */
let recBusy = false;
function startLocalStitch(sel, st, wm, push) {
  if (recBusy) { toast('已有合成在进行'); return; }
  const vids = camSort(curVids()).filter(v => sel.includes(v.cam));
  if (!vids.length) { toast('请至少选择一路'); return; }
  if (!window.MediaRecorder || !HTMLCanvasElement.prototype.captureStream) {
    toast('此浏览器不支持本机合成, 请使用打包下载'); return;
  }
  const fill = document.getElementById('stfill'), pct = document.getElementById('stpct'),
        hint = document.getElementById('sthint');
  fill.style.width = '0%'; pct.textContent = '0%';
  hint.textContent = '正在本机合成 ' + vids.length + ' 路画面 (实时录制, 约 1 分钟), 请保持页面前台';
  st.classList.add('open');
  recBusy = true;
  const n = vids.length;
  let cols, CW, CH;
  if (n === 1) { cols = 1; CW = 1280; CH = 960; }        // 单路保持 4:3 原比例
  else { cols = n <= 4 ? 2 : 3; CW = 640; CH = 360; }
  const canvas = document.createElement('canvas');
  canvas.width = cols * CW; canvas.height = Math.ceil(n / cols) * CH;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const els = vids.map(v => {
    const el = document.createElement('video');
    el.muted = true; el.defaultMuted = true; el.preload = 'auto';
    el.setAttribute('muted', ''); el.setAttribute('playsinline', '');
    el.setAttribute('webkit-playsinline', ''); el.setAttribute('x5-playsinline', '');
    el.src = v.url;
    return el;
  });
  const cleanup = () => {
    els.forEach(e => { try { e.pause(); e.removeAttribute('src'); e.load(); } catch (err) {} });
    recBusy = false;
  };
  Promise.all(els.map(el => new Promise(res => {
    if (el.readyState >= 2) return res();
    el.addEventListener('loadeddata', res, { once: true });
    el.addEventListener('error', () => { toast('有画面加载失败, 请重试'); res(); }, { once: true });
    setTimeout(res, 30000);
  }))).then(() => {
    let mime = '';
    for (const m of ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm', 'video/mp4']) {
      if (MediaRecorder.isTypeSupported(m)) { mime = m; break; }
    }
    if (!mime) { cleanup(); st.classList.remove('open'); toast('此浏览器不支持本机合成'); return; }
    const ext = mime.indexOf('mp4') >= 0 ? 'mp4' : 'webm';
    const dur = Math.max(1, ...els.map(e => isFinite(e.duration) ? e.duration : 0));
    const chunks = [];
    const rec = new MediaRecorder(canvas.captureStream(30), { mimeType: mime, videoBitsPerSecond: 5000000 });
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onerror = () => { cleanup(); st.classList.remove('open'); toast('录制失败, 请重试'); };
    rec.onstop = () => {
      cleanup();
      const blob = new Blob(chunks, { type: mime.split(';')[0] });
      if (push) { uploadPushVideo(blob, push); return; }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (cur ? cur.name : 'teslacam') + '_' + sel.length + '路.' + ext;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      hint.textContent = t('合成完成') + ', ' + t('已开始下载');
      pct.textContent = '100%'; fill.style.width = '100%';
      setTimeout(() => st.classList.remove('open'), 2500);
    };
    els.forEach(e => { try { e.currentTime = 0; e.play().catch(() => {}); } catch (err) {} });
    rec.start(1000);
    const t0 = performance.now();
    (function draw() {
      const t = (performance.now() - t0) / 1000;
      els.forEach((e, i) => {
        try { ctx.drawImage(e, (i % cols) * CW, Math.floor(i / cols) * CH, CW, CH); } catch (err) {}
      });
      if (wm) drawWm(ctx, t, canvas.width);
      const p = Math.min(99, Math.round(t / dur * 100));
      fill.style.width = p + '%'; pct.textContent = p + '%';
      const allDone = els.every(e => e.ended || (isFinite(e.duration) && e.currentTime >= e.duration - 0.05));
      if (t >= dur + 0.8 || allDone) { try { rec.stop(); } catch (err) {} return; }
      requestAnimationFrame(draw);
    })();
  });
}
function pollStitch(id) {
  fetch('/api/stitch/status?id=' + encodeURIComponent(id)).then(r => r.json()).then(d => {
    if (d.state === 'error') { stitchFail(d.err || t('合成失败')); return; }
    const pct = d.pct || 0;
    document.getElementById('stfill').style.width = pct + '%';
    document.getElementById('stpct').textContent = pct + '%';
    if (d.state === 'done') {
      document.getElementById('sthint').textContent = '合成完成, 开始下载…';
      location.href = '/api/stitch/dl?id=' + encodeURIComponent(id);
      setTimeout(() => document.getElementById('stitch').classList.remove('open'), 2500);
      return;
    }
    setTimeout(() => pollStitch(id), 800);
  }).catch(() => setTimeout(() => pollStitch(id), 1500));
}
function stitchFail(msg) {
  document.getElementById('sthint').textContent = t('合成失败') + ': ' + msg;
  document.getElementById('stpct').textContent = '';
  setTimeout(() => document.getElementById('stitch').classList.remove('open'), 5000);
}
function doExportZip() {
  if (!cur) return;
  const sel = [...document.querySelectorAll('#expchks input:checked')].map(c => c.value);
  if (!sel.length) { toast('请至少选择一路'); return; }
  closeExport();
  location.href = '/api/export/' + encodeURIComponent(currentCat) + '/' + encodeURIComponent(cur.name) + '?cams=' + encodeURIComponent(sel.join(',')) + (expQ !== 'orig' ? '&q=' + expQ : '');
}
document.getElementById('startmask').addEventListener('click', playAll);
function tryPlay(v) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
function playAll() {
  const vs = [...document.querySelectorAll('#vgrid video')];
  // iOS/微信只允许在用户手势里逐个调 play(); 同步循环内依次触发
  vs.forEach(tryPlay);
  const m = document.getElementById('startmask');
  if (m) m.classList.remove('show');
  const pp = document.getElementById('ppbtn'); if (pp) { pp.innerHTML = icon('pause'); pp.setAttribute('aria-label', t('暂停')); }
}
function pauseAll() {
  document.querySelectorAll('#vgrid video').forEach(v => v.pause());
  const pp = document.getElementById('ppbtn'); if (pp) { pp.innerHTML = icon('play'); pp.setAttribute('aria-label', t('播放')); }
}
let baseRate = 1;
function setSpeed(s) {
  const r = parseFloat(s);
  baseRate = r;
  document.querySelectorAll('#vgrid video').forEach(v => v.playbackRate = r);
  const b = document.getElementById('spdbtn'); if (b) b.textContent = r + 'x';
}
const SPEEDS = [0.5, 1, 2, 4];
function cycleSpeed() {
  const now = parseFloat(document.getElementById('spdbtn').textContent) || 1;
  setSpeed(SPEEDS[(SPEEDS.indexOf(now) + 1) % SPEEDS.length]);
}
function togglePlay() {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (!vs.length) return;
  if (vs.some(v => v.paused)) playAll(); else pauseAll();
}
function tick() {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (!vs.length || !vs[0].duration) return;
  const t = vs[0].currentTime;
  vs.forEach((v, i) => {
    if (i && v.paused && !vs[0].paused && !v.ended) {
      if (v.readyState >= 2) tryPlay(v);   // 还在缓冲(waiting)的视频不要反复强拉, 会卡
      return;
    }
    if (vs[0].paused || v.paused || v.ended) return;
    const d = v.currentTime - t;
    if (Math.abs(d) > 1.2) { v.currentTime = t; }              // 大漂移才硬seek
    else if (Math.abs(d) > 0.05) {                             // 小漂移: 微调倍速平滑收敛, 不跳帧不抽搐
      const m = Math.min(1.12, Math.max(0.88, 1 - d / 2));
      v.playbackRate = baseRate * m;
    } else if (v.playbackRate !== baseRate) {
      v.playbackRate = baseRate;
    }
  });
  document.getElementById('seek').value = Math.round(t / vs[0].duration * 1000);
  document.getElementById('ptime').textContent = fmtSec(t);
  document.getElementById('ic-dur').textContent = fmtSec(vs[0].duration);
  updateHud(t);
}
function fmtSec(x) {
  x = Math.max(0, Math.floor(x || 0));
  return String(Math.floor(x / 60)).padStart(2, '0') + ':' + String(x % 60).padStart(2, '0');
}
let scrubT = 0;
function applySeekAll(frac) {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (!vs.length || !vs[0].duration) return;
  const tt = frac / 1000 * vs[0].duration;
  vs.forEach(v => { try { v.currentTime = tt; } catch (e) {} });
}
document.getElementById('seek').oninput = function () {
  const now = Date.now();
  if (now - scrubT < 250) return;   // 拖动时节流, 避免 4-6 路同时连发 seek 卡顿
  scrubT = now;
  applySeekAll(+this.value);
};
document.getElementById('seek').onchange = function () {
  scrubT = 0;
  applySeekAll(+this.value);   // 松手必须对齐全部, 否则节流会吞掉最后位置
};
function closePlayer() {
  pauseAll();
  if (syncTimer) clearInterval(syncTimer);
  [...document.querySelectorAll('#vgrid .vcell')].forEach(c => { c.classList.remove('focused'); c.style.display = ''; });
  document.getElementById('player').classList.remove('open');
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const f = document.querySelector('#vgrid .vcell.focused');
  if (f) { focusCell(f); return; }
  closeExport(); closePlayer(); closePanel();
});

/* ---------- 设置面板 ---------- */
function toast(msg) {
  const box = document.getElementById('toast');
  const symbol = /^[❌⚠]/u.test(msg) ? 'alert' : /^⏳/u.test(msg) ? 'update' : 'check';
  const clean = String(msg).replace(/^[✅❌⏳⚠🔗🟢]+\s*/u, '');
  box.innerHTML = icon(symbol) + '<span>' + esc(t(clean)) + '</span>'; box.style.display = 'flex';
  setTimeout(() => box.style.display = 'none', 2500);
}
function doLogout() {
  if (!confirm(t('确定要退出登录吗？'))) return;
  location.href = '/logout';
}
const SETTING_TABS = ['files', 'audio', 'wraps', 'notify', 'auth', 'lang', 'skin', 'update'];
function stab(which) {
  SETTING_TABS.forEach(k => {
    const b = document.getElementById('st-' + k), pane = document.getElementById('tab-' + k);
    if (b) b.classList.toggle('active', k === which);
    if (b) { b.setAttribute('aria-selected', String(k === which)); b.tabIndex = k === which ? 0 : -1; }
    if (pane) pane.style.display = k === which ? '' : 'none';
  });
  document.getElementById('settings-content').scrollTop = 0;
  if (which === 'update') otaStatus();
  if (which === 'notify') ntLoad();
  if (which === 'auth') pwLoad();
  if (which === 'wraps') refreshWraps();
}

/* ---------- 推送设置 ---------- */
const NT_SENS = ['nt-to', 'nt-appid', 'nt-hook', 'nt-url', 'nt-qq-appid', 'nt-qq-openid'];
let ntHidden = true;            // 默认隐藏, 截图安全
function ntMask(v) {
  v = (v || '').trim();
  if (!v) return '';
  if (v.length <= 8) return '•'.repeat(10);
  return v.slice(0, 4) + '•'.repeat(Math.min(20, v.length - 7)) + v.slice(-3);
}
function ntMaskUrl(v) {
  v = (v || '').trim();
  if (!v) return '';
  const m = /^([a-z]+:\/\/)/i.exec(v);
  return (m ? m[1] : '') + '•'.repeat(12);
}
function ntClearReal() {
  NT_SENS.forEach(id => { const el = document.getElementById(id); if (el) delete el.dataset.real; });
}
function ntReal(id) {
  const el = document.getElementById(id);
  if (!el) return '';
  return (el.dataset.real !== undefined ? el.dataset.real : el.value || '').trim();
}
function ntSetEye(hide) {
  ntHidden = !!hide;
  NT_SENS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (ntHidden) {
      if (el.dataset.real === undefined) el.dataset.real = el.value || '';
      el.value = id === 'nt-url' ? ntMaskUrl(el.dataset.real) : ntMask(el.dataset.real);
      el.readOnly = true;
      el.classList.add('masked');
    } else {
      if (el.dataset.real !== undefined) { el.value = el.dataset.real; delete el.dataset.real; }
      el.readOnly = false;
      el.classList.remove('masked');
    }
  });
  const sec = document.getElementById('nt-secret');
  if (sec) sec.placeholder = ntHidden ? 'App Secret ••••••••••'
    : (sec.dataset.has === '1' ? 'App Secret 已保存, 留空表示不修改' : 'App Secret');
  const qqsec = document.getElementById('nt-qq-secret');
  if (qqsec) qqsec.placeholder = qqsec.dataset.has === '1' ? 'QQ AppSecret 已保存, 留空表示不修改' : 'QQ AppSecret';
  const btn = document.getElementById('nt-eye'), tip = document.getElementById('nt-eyetip');
  if (btn) btn.innerHTML = icon(ntHidden ? 'eye' : 'eyeOff') + '<span>' + t(ntHidden ? '显示' : '隐藏') + '</span>';
  if (tip) tip.textContent = ntHidden ? '敏感信息已隐藏（截图安全）' : '敏感信息已明文显示，注意别外发';
}
function ntToggleEye() { ntSetEye(!ntHidden); }

function ntProviderUI() {
  const qq = document.getElementById('nt-provider').value === 'qq';
  document.getElementById('nt-feishu-fields').style.display = qq ? 'none' : '';
  document.getElementById('nt-qq-fields').style.display = qq ? '' : 'none';
}

async function ntLoad() {
  try {
    const j = await (await fetch('/api/notify', { cache: 'no-store' })).json();
    document.getElementById('nt-provider').value = j.provider || 'feishu';
    document.getElementById('nt-qq-type').value = j.qq_target_type || 'user';
    document.getElementById('nt-en').checked = !!j.enabled;
    document.getElementById('nt-saved').checked = !!j.saved;
    document.getElementById('nt-mode').value = j.mode || 'dm';
    document.getElementById('nt-media').value = j.media || 'thumb';
    document.getElementById('nt-quality').value = j.quality || '480p';
    document.getElementById('nt-vdur').value = j.vdur || 30;
    ntMediaUI();
    ntClearReal();
    document.getElementById('nt-to').value = j.receive_id || '';
    document.getElementById('nt-hook').value = j.webhook || '';
    document.getElementById('nt-appid').value = j.app_id || '';
    document.getElementById('nt-secret').value = '';
    document.getElementById('nt-secret').dataset.has = j.has_secret ? '1' : '0';
    document.getElementById('nt-qq-appid').value = j.qq_app_id || '';
    document.getElementById('nt-qq-openid').value = j.qq_openid || '';
    document.getElementById('nt-qq-secret').value = '';
    document.getElementById('nt-qq-secret').dataset.has = j.qq_has_secret ? '1' : '0';
    document.getElementById('nt-url').value = j.public_url || '';
    document.getElementById('nt-delay').value = j.delay || 20;
    ntSetEye(ntHidden);
    ntProviderUI();
    const m = document.getElementById('nt-msg');
    if (j.last && j.last.msg) {
      m.textContent = t('最近一次') + ': ' + (j.last.ok ? '✅ ' : '❌ ') + j.last.msg + ' (' + new Date(j.last.at * 1000).toLocaleString() + ')';
      m.style.color = j.last.ok ? 'var(--positive)' : 'var(--accent)';
    } else if (!j.enabled) { m.textContent = t('未启用'); m.style.color = ''; }
    else if (j.ready) { m.textContent = '已启用推送, 等下一个哨兵事件'; m.style.color = ''; }
    else { m.textContent = t('已启用, 但接收人/应用凭据未配全'); m.style.color = 'var(--accent)'; }
  } catch (e) { document.getElementById('nt-msg').textContent = t('读取设置失败') + ': ' + e; }
}
async function ntSave() {
  const body = {
    provider: document.getElementById('nt-provider').value,
    qq_app_id: ntReal('nt-qq-appid'),
    qq_app_secret: document.getElementById('nt-qq-secret').value.trim(),
    qq_target_type: document.getElementById('nt-qq-type').value,
    qq_openid: ntReal('nt-qq-openid'),
    enabled: document.getElementById('nt-en').checked,
    saved: document.getElementById('nt-saved').checked,
    mode: document.getElementById('nt-mode').value,
    media: document.getElementById('nt-media').value,
    quality: document.getElementById('nt-quality').value,
    vdur: +document.getElementById('nt-vdur').value || 30,
    receive_id: ntReal('nt-to'),
    webhook: ntReal('nt-hook'),
    app_id: ntReal('nt-appid'),
    app_secret: document.getElementById('nt-secret').value.trim(),
    public_url: ntReal('nt-url'),
    delay: +document.getElementById('nt-delay').value || 20,
  };
  if (body.enabled) {
    if (body.provider === 'qq') {
      const saved = document.getElementById('nt-qq-secret').dataset.has === '1';
      if (!body.qq_app_id || !(body.qq_app_secret || saved) || !body.qq_openid) {
        toast('QQ 需要 AppID / AppSecret 和接收 OpenID'); return;
      }
    } else {
      const secSaved = document.getElementById('nt-secret').dataset.has === '1';
      const hasApp = body.app_id && (body.app_secret || secSaved);
      if (body.mode === 'dm' && (!hasApp || !body.receive_id)) { toast('私聊需要 App ID / Secret + 接收人'); return; }
      if (body.mode === 'group' && !body.webhook) { toast('请先填群机器人 webhook'); return; }
    }
  }
  const m = document.getElementById('nt-msg');
  m.textContent = t('保存中…');
  try {
    const j = await (await fetch('/api/notify/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    m.textContent = (j.ok ? '✅ ' + t('已保存') : '❌ ' + j.msg) + (j.warn ? ' · ' + j.warn : '');
    m.style.color = j.ok ? 'var(--positive)' : 'var(--accent)';
    if (j.ok) ntLoad();
  } catch (e) { m.textContent = t('保存失败') + ': ' + e; m.style.color = 'var(--accent)'; }
}
async function ntTest() {
  const m = document.getElementById('nt-msg');
  m.textContent = '正在发送测试消息（使用已保存的设置）…'; m.style.color = '';
  try {
    const j = await (await fetch('/api/notify/test', { method: 'POST' })).json();
    m.textContent = (j.ok ? '✅ ' : '❌ ') + j.msg;
    m.style.color = j.ok ? 'var(--positive)' : 'var(--accent)';
  } catch (e) { m.textContent = t('发送失败') + ': ' + e; m.style.color = 'var(--accent)'; }
}

function ntMediaUI() {
  const v = document.getElementById('nt-media').value === 'video';
  document.getElementById('nt-vwrap').style.display = v ? '' : 'none';
}

/* ---------- 账号: 改登录密码 ---------- */
async function pwLoad() {
  try {
    const j = await (await fetch('/api/auth/info', { cache: 'no-store' })).json();
    document.getElementById('pw-user').textContent = j.user || 'admin';
    const m = document.getElementById('pw-msg');
    if (!j.on) { m.textContent = t('当前未开启登录保护, 设置密码后即开启'); m.style.color = 'var(--muted2)'; }
  } catch (e) {}
}
async function pwSave() {
  const m = document.getElementById('pw-msg');
  const o = document.getElementById('pw-old').value;
  const n = document.getElementById('pw-new').value;
  const n2 = document.getElementById('pw-new2').value;
  if (n.length < 6) { m.textContent = t('新密码至少 6 位'); m.style.color = 'var(--accent)'; return; }
  if (n !== n2) { m.textContent = t('两次输入的新密码不一致'); m.style.color = 'var(--accent)'; return; }
  m.textContent = t('提交中…'); m.style.color = '';
  try {
    const j = await (await fetch('/api/auth/pass', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: o, 'new': n }) })).json();
    if (!j.ok) { m.textContent = '❌ ' + t(j.msg); m.style.color = 'var(--accent)'; return; }
    // 旧 cookie 已随 secret 更换失效, 用新密码立即重新登录, 保持本机不掉线
    try {
      await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'u=' + encodeURIComponent(j.user || 'admin') + '&p=' + encodeURIComponent(n) });
    } catch (e) {}
    document.getElementById('pw-old').value = '';
    document.getElementById('pw-new').value = '';
    document.getElementById('pw-new2').value = '';
    m.textContent = '✅ ' + j.msg;
    m.style.color = 'var(--positive)';
  } catch (e) { m.textContent = t('修改失败') + ': ' + e; m.style.color = 'var(--accent)'; }
}

/* ---------- 手动 ZIP 更新 ---------- */
let otaCurrent = '', otaBusy = false;
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function otaSetBusy(busy) {
  otaBusy = busy;
  ['upd-go', 'upd-rb', 'upd-url', 'upd-sha'].forEach(id => {
    document.getElementById(id).disabled = busy;
  });
}
async function otaStatus() {
  // This endpoint reads the installed version and rollback metadata only.
  try {
    const response = await fetch('/api/ota/status', { cache: 'no-store' });
    if (!response.ok) throw new Error('无法读取本机更新状态');
    const j = await response.json();
    otaCurrent = j.current;
    document.getElementById('upd-cur').textContent = 'v' + j.current;
    document.getElementById('upd-rb').style.display = j.rollback_ready ? '' : 'none';
    document.getElementById('upd-tag').textContent = j.busy ? '正在更新或等待重启' : '手动 ZIP 更新';
    otaSetBusy(!!j.busy);
  } catch (e) {
    document.getElementById('upd-msg').textContent = '读取本机状态失败，请刷新页面重试';
  }
}
/* Only poll our own device after an explicitly requested update or rollback. */
async function otaWaitBack(previous) {
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const r = await fetch('/api/ota/status', { cache: 'no-store' });
      // Older rollback targets do not have the status route.
      if (r.status === 404 || r.status === 401) { location.reload(); return; }
      if (r.ok && (await r.json()).current !== previous) { location.reload(); return; }
    } catch (e) {}
  }
  document.getElementById('upd-msg').textContent = '服务尚未完成重启，请稍后手动刷新页面';
}
async function otaApply() {
  if (otaBusy) return;
  const url = document.getElementById('upd-url').value.trim();
  const sha256 = document.getElementById('upd-sha').value.trim();
  const msg = document.getElementById('upd-msg');
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.hash) throw new Error();
  } catch (e) { msg.textContent = '请输入 HTTP / HTTPS 的 ZIP 下载直链'; return; }
  if (sha256 && !/^[a-fA-F0-9]{64}$/.test(sha256)) { msg.textContent = 'SHA-256 须为 64 位十六进制，也可留空'; return; }
  if (!otaCurrent) await otaStatus();
  otaSetBusy(true);
  msg.textContent = '正在从指定 URL 下载、校验 ZIP 并安装，请保持页面打开…';
  try {
    const j = await (await fetch('/api/ota/apply', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, sha256 }) })).json();
    msg.textContent = j.msg;
    if (!j.ok) { otaSetBusy(false); return; }
    document.getElementById('upd-url').value = ''; // signed URL stays out of browser storage
    otaWaitBack(otaCurrent);
  } catch (e) {
    msg.textContent = '请求中断，请稍后刷新页面确认安装状态';
    otaSetBusy(false);
  }
}
async function otaRollback() {
  if (otaBusy) return;
  if (!confirm('回滚到升级前的版本?\n适合新版本出现问题时使用, 服务会自动重启。')) return;
  otaSetBusy(true);
  const msg = document.getElementById('upd-msg');
  msg.textContent = '正在回滚…';
  try {
    const j = await (await fetch('/api/ota/rollback', { method: 'POST' })).json();
    msg.textContent = j.msg;
    if (j.ok) otaWaitBack(otaCurrent);
    else otaSetBusy(false);
  } catch (e) { msg.textContent = '回滚请求中断，请刷新页面确认状态'; otaSetBusy(false); }
}
function openPanel(which = 'files') {
  document.getElementById('mask').classList.add('open');
  document.getElementById('panel').classList.add('open');
  document.getElementById('panel').setAttribute('aria-hidden', 'false');
  document.body.classList.add('dialog-open');
  document.querySelectorAll('.library,.app-header').forEach(el => { el.inert = true; });
  stab(which);
  refreshSkins(); refreshAudio(); refreshFiles();
  document.getElementById('st-' + which).focus();
}
document.getElementById('gear').onclick = () => openPanel();
function closePanel() {
  const wasOpen = document.getElementById('panel').classList.contains('open');
  document.getElementById('mask').classList.remove('open');
  document.getElementById('panel').classList.remove('open');
  document.getElementById('panel').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('dialog-open');
  document.querySelectorAll('.library,.app-header').forEach(el => { el.inert = false; });
  if (wasOpen) document.getElementById('gear').focus();
}

SETTING_TABS.forEach(key => {
  const button = document.getElementById('st-' + key), pane = document.getElementById('tab-' + key);
  button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', 'tab-' + key);
  button.setAttribute('aria-selected', String(key === 'files')); button.tabIndex = key === 'files' ? 0 : -1;
  pane.setAttribute('role', 'tabpanel'); pane.setAttribute('aria-labelledby', 'st-' + key);
  button.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    let index = SETTING_TABS.indexOf(key);
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = SETTING_TABS.length - 1;
    else index = (index + (['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1) + SETTING_TABS.length) % SETTING_TABS.length;
    stab(SETTING_TABS[index]); document.getElementById('st-' + SETTING_TABS[index]).focus();
  });
});
document.addEventListener('keydown', event => {
  const panel = document.getElementById('panel');
  if (event.key !== 'Tab' || !panel.classList.contains('open')) return;
  const focusable = [...panel.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href],summary,[tabindex="0"]')]
    .filter(el => el.offsetParent !== null && el.tabIndex >= 0);
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (!first) return;
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

async function uploadFile(input, url, fields) {
  const f = input.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  for (const k in (fields || {})) fd.append(k, fields[k]);
  const r = await fetch(url, { method: 'POST', body: fd });
  const j = await r.json();
  toast(j.ok ? '✅ ' + j.msg : '❌ ' + j.msg);
  input.value = '';
  return j;
}

/* 皮肤 */
async function refreshSkins() {
  const j = await (await fetch('/api/skins')).json();
  const list = document.getElementById('skin-list'); list.innerHTML = '';
  if (!j.skins.length) { list.innerHTML = '<div class="hint">暂无皮肤, 使用默认外观</div>'; return; }
  for (const s of j.skins) {
    const d = document.createElement('div'); d.className = 'item';
    d.innerHTML = `<span class="n">${s}</span>` + (s === j.active ? '<span class="tag">使用中</span>' : '') +
      (s === j.active ? '' : `<button class="primary" onclick="applySkin('${s}')">应用</button>`) +
      `<button class="danger" onclick="delSkin('${s}')">删除</button>`;
    list.appendChild(d);
  }
}
async function applySkin(s) {
  await fetch('/api/skin/activate', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name: s }) });
  toast('皮肤已应用: ' + s); injectSkin(); refreshSkins();
}
async function delSkin(s) {
  if (!confirm('删除皮肤 ' + s + ' ?')) return;
  await fetch('/api/skin/' + encodeURIComponent(s), { method: 'DELETE' });
  toast('已删除'); injectSkin(); refreshSkins();
}
async function injectSkin() {
  let el = document.getElementById('userskin');
  if (!el) { el = document.createElement('style'); el.id = 'userskin'; document.head.appendChild(el); }
  const r = await fetch('/api/skin');
  el.textContent = r.ok ? await r.text() : '';
}
document.getElementById('skin-file').onchange = async function () {
  const j = await uploadFile(this, '/api/upload/skin');
  if (j && j.ok) { refreshSkins(); if (j.activated) { injectSkin(); } }
};

/* 锁车语音 */
async function refreshAudio() {
  const j = await (await fetch('/api/audio')).json();
  const mk = (arr, elid, isDone) => {
    const list = document.getElementById(elid); list.innerHTML = '';
    if (!arr.length) { list.innerHTML = '<div class="hint" style="margin:0">无</div>'; return; }
    for (const f of arr) {
      const d = document.createElement('div'); d.className = 'item';
      const lockTag = f === 'LockChime.wav' ? '<span class="tag">锁车音</span>' : '';
      d.innerHTML = `<span class="n">${f}</span>${lockTag}` +
        (isDone ? '' : `<button class="primary" onclick="setLock('${f}')">设为锁车音</button>`) +
        `<button class="danger" onclick="delAudio('${f}',${isDone})">删除</button>`;
      list.appendChild(d);
    }
  };
  mk(j.queue, 'audio-list', false);
  mk(j.done, 'audio-done', true);
}
async function setLock(f) {
  const r = await fetch('/api/audio/setlockchime', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name: f }) });
  const j = await r.json();
  toast(j.ok ? '✅ 已设为锁车音 (LockChime.wav)' : '❌ ' + j.msg);
  refreshAudio();
}
async function delAudio(f, isDone) {
  if (!confirm('删除 ' + f + ' ?')) return;
  await fetch('/api/audio/' + encodeURIComponent(f) + '?done=' + (isDone ? 1 : 0), { method: 'DELETE' });
  refreshAudio();
}
document.getElementById('audio-file').onchange = async function () {
  const msg = document.getElementById('audio-msg');
  this.disabled = true;
  msg.textContent = '正在上传并转换锁车音…';
  try {
    const j = await uploadFile(this, '/api/upload/audio', {
      start: document.getElementById('audio-start').value || '0',
      duration: document.getElementById('audio-duration').value || '5',
    });
    msg.textContent = j ? j.msg : '未选择音频';
    if (j && j.ok) refreshAudio();
  } catch (e) { msg.textContent = '转换失败: ' + e; }
  finally { this.disabled = false; this.value = ''; }
};

/* 文件管理器 */
let curDir = '';
let area = 'usb';  // 'usb' = 车机U盘镜像, 'archive' = 归档区
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
/* ---------- 涂装 (Wraps) ---------- */
function wrapStatusHtml(attached) {
  return usbStatusHtml(attached, 'wraps');
}
async function wrapAttach() {
  toast('⏳ 正在连接, 管理期间车机读取将暂停…');
  const j = await (await fetch('/api/usb/attach', { method: 'POST' })).json();
  toast(j.ok ? '✅ 已连接车机U盘' : '❌ ' + j.msg);
  refreshWraps();
}
async function wrapDetach() {
  const j = await (await fetch('/api/usb/detach', { method: 'POST' })).json();
  toast((j.ok ? '✅ ' : '❌ ') + j.msg);
  refreshWraps();
}
async function refreshWraps() {
  const st = document.getElementById('wraps-status');
  const grid = document.getElementById('wraps-grid');
  let j;
  try {
    j = await (await fetch('/api/usb/browse?dir=' + encodeURIComponent('Wraps'))).json();
  } catch (e) { return; }
  st.innerHTML = wrapStatusHtml(j.attached);
  document.getElementById('wraps-file').disabled = !j.attached;
  grid.innerHTML = '';
  if (!j.ok) { grid.innerHTML = '<div class="hint">Wraps 目录不存在, 连接 U 盘后上传图片会自动创建</div>'; return; }
  const files = (j.files || []).filter(f => f.name.toLowerCase().endsWith('.png'));
  if (!files.length) { grid.innerHTML = '<div class="hint">Wraps 目录为空, 上传图片即可在车机上选用</div>'; return; }
  for (const f of files) {
    const box = document.createElement('div');
    box.className = 'wrap-card';
    const img = document.createElement('img');
    img.src = '/usbdl?path=' + encodeURIComponent('Wraps/' + f.name);
    img.className = 'wrap-preview';
    img.alt = f.name;
    img.onclick = () => window.open('/usbdl?path=' + encodeURIComponent('Wraps/' + f.name));
    const cap = document.createElement('div');
    cap.textContent = f.name;
    cap.className = 'wrap-name';
    const btn = document.createElement('button');
    btn.className = 'mini danger'; btn.textContent = t('删除');
    btn.onclick = async () => {
      if (!confirm(t('删除') + ' Wraps/' + f.name + ' ?')) return;
      const r = await fetch('/api/usb?path=' + encodeURIComponent('Wraps/' + f.name), { method: 'DELETE' });
      const rj = await r.json().catch(() => ({}));
      toast(rj.ok ? '✅ ' + t('已删除') : '❌ ' + (rj.msg || t('删除失败')));
      refreshWraps();
    };
    box.append(img, cap, btn);
    grid.appendChild(box);
  }
}
document.getElementById('wraps-file').onchange = async function () {
  if (!this.files || !this.files.length) return;
  for (const f of this.files) {
    const fd = new FormData(); fd.append('file', f);
    try {
      let r = await fetch('/api/upload/wrap', { method: 'POST', body: fd });
      let j = await r.json();
      if (j.exists && confirm(f.name + '：' + j.msg)) {
        fd.append('overwrite', '1');
        r = await fetch('/api/upload/wrap', { method: 'POST', body: fd });
        j = await r.json();
      }
      toast((j.ok ? '✅ ' : '❌ ') + f.name + ' — ' + (j.msg || ''));
    } catch (e) { toast('❌ ' + f.name + ' — ' + t('上传失败')); }
  }
  this.value = '';
  refreshWraps();
};
function switchArea(a) {
  area = a;
  document.getElementById('area-usb').classList.toggle('active', a === 'usb');
  document.getElementById('area-archive').classList.toggle('active', a === 'archive');
  document.getElementById('usb-hint').style.display = a === 'usb' ? '' : 'none';
  document.getElementById('arch-hint').style.display = a === 'archive' ? '' : 'none';
  curDir = a === 'usb' ? '' : '/archive/files';
  refreshFiles();
}
function usbStatusHtml(attached, context = 'files') {
  const wrap = context === 'wraps';
  if (!attached) {
    return '<div class="notice"><span class="notice-icon">' + icon('storage') + '</span><div class="notice-content"><div class="notice-title">' + t('尚未进入 U 盘管理模式') + '</div><p>' + t('连接后车机读写会暂停，请在停车后操作。') + '</p><button class="mini primary" onclick="' + (wrap ? 'wrapAttach()' : 'usbAttach()') + '">' + t('连接车机 U 盘') + '</button></div></div>';
  }
  return '<div class="notice positive"><span class="notice-icon">' + icon('storage') + '</span><div class="notice-content"><div class="notice-title">' + t('U 盘管理已连接') + '</div><p>' + t(wrap ? '图片将直接写入 Wraps 目录，完成后请恢复车机导出。' : '网页改动会写入车机 U 盘，操作完成后请恢复导出。') + '</p><button class="mini" onclick="' + (wrap ? 'wrapDetach()' : 'usbDetach()') + '">' + t('完成并恢复导出') + '</button><small>' + t('闲置后系统会尝试自动恢复导出') + '</small></div></div>';
}
async function usbAttach() {
  toast('⏳ 正在连接 (车机读取将断开几秒)...');
  const r = await fetch('/api/usb/attach', { method: 'POST' });
  const j = await r.json();
  toast(j.ok ? '✅ 已连接车机U盘' : '❌ ' + j.msg);
  curDir = '';
  refreshFiles();
}
async function usbDetach() {
  const j = await (await fetch('/api/usb/detach', { method: 'POST' })).json();
  toast(j.ok ? '✅ ' + t('已恢复车机导出') : '❌ ' + t(j.msg));
  document.getElementById('dir-nav').style.display = 'none';
  refreshFiles();
}
function goDir(d) {
  if (area === 'usb') {
    d = (d || '').trim().replace(/^\/+|\/+$/g, '');
    refreshFiles(d);
  } else {
    d = (d || '').trim().replace(/\/+$/, '') || '/archive';
    if (!d.startsWith('/archive')) { toast('❌ 仅支持 /archive 内的路径'); return; }
    refreshFiles(d);
  }
}
async function refreshFiles(dir) {
  if (dir !== undefined) curDir = dir;
  const statusEl = document.getElementById('usb-status');
  const nav = document.getElementById('dir-nav');
  const crumb = document.getElementById('crumb');
  const list = document.getElementById('file-list');
  let j;
  if (area === 'usb') {
    j = await (await fetch('/api/usb/browse?dir=' + encodeURIComponent(curDir))).json();
    statusEl.style.display = 'block';
    statusEl.innerHTML = usbStatusHtml(j.attached);
    if (!j.attached) { nav.style.display = 'none'; return; }
    nav.style.display = 'block';
  } else {
    j = await (await fetch('/api/browse?dir=' + encodeURIComponent(curDir))).json();
    statusEl.style.display = 'none';
    if (!j.ok) { toast('❌ ' + (j.msg || '目录不可访问')); return; }
    nav.style.display = 'block';
  }
  curDir = area === 'usb' ? j.dir : j.dir;
  document.getElementById('dest-dir').value = curDir;
  crumb.innerHTML = '';
  const mkLink = (label, target, folder) => {
    const a = document.createElement('a');
    a.textContent = label; a.href = 'javascript:void(0)';
    a.onclick = () => goDir(target);
    crumb.appendChild(a);
    const s = document.createElement('span'); s.textContent = ' / ';
    crumb.appendChild(s);
  };
  if (area === 'usb') {
    mkLink(t('U盘根目录'), '', true);
    let acc = '';
    for (const seg of curDir.split('/').filter(Boolean)) {
      acc = acc ? acc + '/' + seg : seg;
      mkLink(seg, acc, true);
    }
  } else {
    let acc = '';
    for (const seg of curDir.split('/').filter(Boolean)) { acc += '/' + seg; mkLink(seg, acc, true); }
  }
  list.innerHTML = '';
  let empty = true;
  for (const d of j.dirs) {
    empty = false;
    const el = document.createElement('div'); el.className = 'item';
    const n = document.createElement('span'); n.className = 'n folder';
    n.textContent = d;
    n.onclick = () => goDir(area === 'usb' ? (curDir ? curDir + '/' + d : d) : curDir + '/' + d);
    const symbol = document.createElement('span'); symbol.innerHTML = icon('folder');
    el.append(symbol, n); list.appendChild(el);
  }
  for (const f of j.files) {
    empty = false;
    const d = document.createElement('div'); d.className = 'item';
    const n = document.createElement('span'); n.className = 'n'; n.textContent = f.name;
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = fmtSize(f.size);
    const btnDl = document.createElement('button'); btnDl.className = 'primary'; btnDl.textContent = '下载';
    btnDl.onclick = async () => {
      const full = area === 'usb' ? (curDir ? curDir + '/' + f.name : f.name) : curDir + '/' + f.name;
      const url = (area === 'usb' ? '/usbdl?path=' : '/dl?path=') + encodeURIComponent(full);
      if (area !== 'usb') { location.href = url; return; }   // 归档区可能是大视频, 走浏览器流式下载
      try {   // U盘文件多为小件: fetch 成 blob, 失败只弹提示, 不再整页跳走
        const r = await fetch(url);
        if (!r.ok) { toast('❌ ' + t('文件读不到, U盘可能已自动恢复车机导出, 请重新连接')); return; }
        const b = await r.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(b); a.download = f.name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      } catch (e) { toast('❌ ' + t('下载失败')); }
    };
    const btnDel = document.createElement('button'); btnDel.className = 'danger'; btnDel.textContent = '删除';
    btnDel.onclick = () => delFile(area === 'usb' ? (curDir ? curDir + '/' + f.name : f.name) : curDir + '/' + f.name);
    d.append(n, tag, btnDl, btnDel); list.appendChild(d);
  }
  if (empty) list.innerHTML = '<div class="hint" style="margin:0">' + t('空目录') + '</div>';
}
async function delFile(p) {
  if (!confirm('删除 ' + (area === 'usb' ? 'U盘/' : '') + p + ' ?')) return;
  const url = area === 'usb' ? '/api/usb?path=' : '/api/file?path=';
  await fetch(url + encodeURIComponent(p), { method: 'DELETE' });
  toast('已删除'); refreshFiles();
}
document.getElementById('anyfile-file').onchange = async function () {
  const url = area === 'usb' ? '/api/upload/usb' : '/api/upload/file';
  const j = await uploadFile(this, url, { path: area === 'usb' ? curDir : curDir });
  if (j && j.ok) refreshFiles();
};

injectSkin();
applyLang();
load();

function setExpQuality(quality) {
  expQ = quality;
  renderExpQ();
  closeQ();
}

window.addEventListener('tsf:language', () => {
  applyTheme(localStorage.getItem('tsf_theme') || 'auto');
  if (data && document.getElementById('tabs')) load();
});

// Public actions referenced by static and generated HTML. All other state stays in this module.
Object.assign(window, {
  clearFilters,
  openPanel,
  ntProviderUI,
  wrapDetach,
  applySkin,
  closeExport,
  closeLive,
  closePanel,
  closePlayer,
  cycleSpeed,
  cycleTheme,
  delAudio,
  delSkin,
  doExport,
  doExportZip,
  doLogout,
  doPushFeishu,
  expChk,
  goDir,
  liveRestart,
  liveTick,
  ntMediaUI,
  ntSave,
  ntTest,
  ntToggleEye,
  openExport,
  otaApply,
  otaStatus,
  otaRollback,
  pickDate,
  pwSave,
  setDate,
  setExpQuality,
  setLang,
  setLock,
  setQuality,
  setReason,
  stab,
  switchArea,
  switchClip,
  thumbErr,
  toggleDetail,
  toggleExpQ,
  togglePlay,
  toggleQ,
  toggleTelRows,
  usbAttach,
  usbDetach,
  wrapAttach
});
