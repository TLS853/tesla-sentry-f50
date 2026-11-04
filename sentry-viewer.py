#!/usr/bin/env python3
"""Tesla Sentry F50 - 特斯拉哨兵查看器 (单文件, 纯标准库, 零第三方依赖)

功能:
  - 六路同步播放器 / 时间线 / 分类
  - 页面皮肤 (CSS/图片上传)
  - 锁车语音 / Boombox 音效上传 (经归档同步写入车机U盘)
  - 车机U盘 & 归档区文件管理 (临时断开/恢复导出)

访问: http://<device-ip>:8080
配置: /etc/tesla-sentry.conf (或同名环境变量), 见 README
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------- 配置 ----------
def _load_conf():
    conf = {}
    try:
        with open("/etc/tesla-sentry.conf", "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return conf


_CONF = _load_conf()


def cfg(key, default=""):
    return os.environ.get(key) or _CONF.get(key) or default


BASE_DIR = cfg("TSF_DIR", os.path.dirname(os.path.abspath(__file__)))
IMG = cfg("TSF_IMG", os.path.join(BASE_DIR, "teslacam.img"))
ARCHIVE = cfg("TSF_ARCHIVE", "/archive")
MNT = cfg("TSF_MNT", "/tmp/teslamnt")
LOOPDEV = cfg("TSF_LOOP", "/dev/loop3")
PORT = int(cfg("TSF_PORT", "8080"))
USB_AUTO_DETACH = int(cfg("TSF_USB_TIMEOUT", "300"))  # 秒, U盘管理无操作自动恢复导出

SKIN_DIR = os.path.join(ARCHIVE, ".skins")
AUDIO_Q = os.path.join(ARCHIVE, "boombox-queue")
AUDIO_DONE = os.path.join(ARCHIVE, "boombox-done")
FILES_DIR = os.path.join(ARCHIVE, "files")
CATS = ["SentryClips", "SavedClips", "RecentClips"]
CAT_NAMES = {"SentryClips": "哨兵告警", "SavedClips": "手动保存", "RecentClips": "行车记录"}
MAX_UPLOAD = 100 * 1024 * 1024  # 通用文件上传上限 100MB (皮肤/音频有自己的更小限制)


def find_gadget():
    """自动定位 configfs 里的 USB gadget 目录 (不同设备名字不同)"""
    g = cfg("TSF_GADGET", "")
    if g and os.path.isdir(g):
        return g
    base = "/sys/kernel/config/usb_gadget"
    try:
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            if os.path.isdir(os.path.join(p, "configs")):
                return p
    except Exception:
        pass
    return os.path.join(base, "linux")


def losetup_cmd(args):
    """调用 losetup, 自动兼容 busybox 精简系统"""
    candidates = [cfg("TSF_LOSETUP", ""), shutil.which("losetup"),
                  "/opt/mu300/busybox-bin/losetup", "/sbin/losetup", "/usr/sbin/losetup"]
    for p in candidates:
        if p and os.path.exists(p):
            return subprocess.run([p] + args, capture_output=True, text=True)
    return subprocess.run(["busybox", "losetup"] + args, capture_output=True, text=True)


HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>特斯拉哨兵查看器</title>
<style id="base">
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:#101418; color:#e8eaed; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
header { padding:14px 18px; background:#1a2027; display:flex; align-items:center; gap:12px; position:sticky; top:0; z-index:10; }
header h1 { font-size:17px; font-weight:600; }
header .sub { font-size:12px; color:#9aa4af; }
.tabs { display:flex; gap:8px; margin-left:auto; }
.tab { padding:6px 14px; border-radius:18px; background:#242c35; color:#9aa4af; cursor:pointer; font-size:13px; border:none; }
.tab.active { background:#e8443f; color:#fff; }
#gear { background:#242c35; border:none; color:#9aa4af; font-size:18px; cursor:pointer; border-radius:10px; width:36px; height:32px; }
#gear:hover { color:#fff; }
.grid-wrap { padding:16px; display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
.card { background:#1a2027; border-radius:12px; overflow:hidden; cursor:pointer; transition:transform .15s; border:1px solid #232b34; }
.card:hover { transform:translateY(-2px); border-color:#e8443f55; }
.thumb { display:grid; grid-template-columns:repeat(3,1fr); gap:2px; background:#000; aspect-ratio:16/9; }
.thumb video { width:100%; height:100%; object-fit:cover; pointer-events:none; }
.thumb .ph { background:#0b0f13; display:flex; align-items:center; justify-content:center; color:#3a4652; font-size:22px; }
.meta { padding:9px 12px; }
.meta .t { font-size:13.5px; font-weight:600; }
.meta .s { font-size:11.5px; color:#9aa4af; margin-top:2px; }
.player { display:none; position:fixed; inset:0; background:rgba(0,0,0,.94); z-index:100; flex-direction:column; }
.player.open { display:flex; }
.player .vgrid { flex:1; display:grid; grid-template-columns:1fr 1fr 1fr; grid-template-rows:1fr 1fr; gap:4px; padding:4px; }
.player video { width:100%; height:100%; background:#000; object-fit:contain; }
.pbar { display:flex; align-items:center; gap:12px; padding:10px 16px; background:#1a2027; }
.pbar input[type=range] { flex:1; }
.pbar button { background:#e8443f; border:none; color:#fff; border-radius:8px; padding:8px 18px; font-size:14px; cursor:pointer; }
.pbar .t { font-size:13px; color:#9aa4af; min-width:90px; text-align:center; }
.close { position:absolute; top:10px; right:14px; background:none; border:none; color:#fff; font-size:30px; cursor:pointer; z-index:101; }
.empty { text-align:center; padding:80px 20px; color:#5c6873; }
.empty .big { font-size:44px; margin-bottom:14px; }
/* 设置面板 */
.mask { display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:200; }
.mask.open { display:block; }
.panel { position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); width:min(560px,94vw); max-height:86vh; overflow:auto; background:#1a2027; border-radius:14px; z-index:201; padding:18px; display:none; }
.panel.open { display:block; }
.panel h2 { font-size:15px; margin-bottom:12px; display:flex; align-items:center; }
.panel h2 .x { margin-left:auto; background:none; border:none; color:#9aa4af; font-size:20px; cursor:pointer; }
.stabs { display:flex; gap:8px; margin-bottom:14px; }
.stab { flex:1; padding:8px; border-radius:10px; background:#242c35; color:#9aa4af; text-align:center; cursor:pointer; font-size:13px; border:none; }
.stab.active { background:#e8443f; color:#fff; }
.hint { font-size:11.5px; color:#7d8894; line-height:1.6; margin-bottom:10px; }
.upbtn { display:block; width:100%; padding:10px; border:1px dashed #3a4652; border-radius:10px; color:#9aa4af; text-align:center; cursor:pointer; font-size:13px; margin-bottom:12px; background:#161c22; }
.upbtn:hover { border-color:#e8443f; color:#fff; }
.item { display:flex; align-items:center; gap:10px; background:#161c22; border-radius:10px; padding:9px 12px; margin-bottom:8px; font-size:13px; }
.item .n { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.item button { background:#242c35; border:none; color:#9aa4af; border-radius:7px; padding:5px 10px; font-size:12px; cursor:pointer; }
.item button.danger:hover { background:#e8443f; color:#fff; }
.item button.primary { background:#e8443f; color:#fff; }
.item .tag { font-size:10.5px; color:#4cc38a; }
.toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#e8443f; color:#fff; padding:9px 20px; border-radius:20px; font-size:13px; z-index:300; display:none; }
@media (max-width:600px){ .player .vgrid { grid-template-columns:1fr 1fr; grid-template-rows:1fr 1fr 1fr; } }
</style></head><body>
<header><h1>🚗 特斯拉哨兵查看器</h1><span class="sub" id="stat"></span>
<span id="live" title="车机在线只读同步状态" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#7d8894;background:#1a2027;border:1px solid #232b34;border-radius:14px;padding:4px 10px;white-space:nowrap"><span id="livedot" style="width:7px;height:7px;border-radius:50%;background:#5f6b76"></span><span id="livetxt">同步状态</span></span>
<nav class="tabs" id="tabs"></nav><button id="gear" title="皮肤与锁车音">⚙</button></header>
<div class="grid-wrap" id="grid"></div>
<div class="empty" id="empty" style="display:none"><div class="big">📹</div>
<div>暂无归档视频<br><small style="font-size:12px">车机录制后, 归档同步任务会自动把视频收进来 (镜像需空闲10分钟)</small></div></div>

<div class="player" id="player">
  <button class="close" onclick="closePlayer()">✕</button>
  <div class="vgrid" id="vgrid"></div>
  <div class="pbar">
    <button onclick="playAll()">▶ 播放</button>
    <button onclick="pauseAll()" style="background:#242c35">⏸</button>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span class="t" id="ptime">0:00</span>
    <select id="speed" onchange="setSpeed(this.value)" style="background:#242c35;color:#e8eaed;border:none;border-radius:8px;padding:6px">
      <option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option><option value="4">4x</option>
    </select>
  </div>
</div>

<div class="mask" id="mask" onclick="closePanel()"></div>
<div class="panel" id="panel">
  <h2>⚙ 个性化 <button class="x" onclick="closePanel()">✕</button></h2>
  <div class="stabs">
    <button class="stab active" id="st-skin" onclick="stab('skin')">🎨 页面皮肤</button>
    <button class="stab" id="st-audio" onclick="stab('audio')">🔊 锁车语音</button>
    <button class="stab" id="st-files" onclick="stab('files')">📂 文件</button>
  </div>
  <div id="tab-skin">
    <div class="hint">上传 CSS 皮肤文件或背景图 (jpg/png/webp)。CSS 中引用图片请用 <b>/skin-media/文件名</b>。CSS 示例:<br><code>body{background:url(/skin-media/bg.jpg) center/cover}</code></div>
    <label class="upbtn">＋ 上传皮肤文件 (css/jpg/png/webp)<input type="file" id="skin-file" accept=".css,.jpg,.jpeg,.png,.webp" hidden></label>
    <div id="skin-list"></div>
  </div>
  <div id="tab-audio" style="display:none">
    <div class="hint">锁车音要求: <b>WAV · 44.1kHz · 16-bit · &lt;1MB · 1~5秒</b>, 文件名保存为 <b>LockChime.wav</b> 后写入车机U盘 <b>Boombox/</b> 目录 (车机端 Toybox→Boombox→Lock Sound 选 USB)。上传其他 mp3/wav 可作为 Boombox 喇叭音效 (车机只认字母序前5个, 每个同样 &lt;1MB)。<br>文件将在<b>归档同步时</b>自动写入车机U盘 (镜像空闲10分钟后, 每10分钟检查一次)。</div>
    <label class="upbtn">＋ 上传音频 (wav/mp3, &lt;1MB)<input type="file" id="audio-file" accept=".wav,.mp3" hidden></label>
    <div class="hint" style="color:#4cc38a">⏳ 待写入车机U盘:</div>
    <div id="audio-list"></div>
    <div class="hint" style="color:#4cc38a">✅ 已写入:</div>
    <div id="audio-done"></div>
  </div>
  <div id="tab-files" style="display:none">
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button id="area-usb" class="stab" style="flex:1" onclick="switchArea('usb')">🚗 车机U盘</button>
      <button id="area-archive" class="stab" style="flex:1" onclick="switchArea('archive')">🗄 归档区</button>
    </div>
    <div id="usb-status" style="display:none;margin-bottom:10px"></div>
    <div id="usb-hint" class="hint">浏览和管理<b>车机U盘</b> (特斯拉写入的镜像)。连接后会<b>临时断开车机读取几秒</b>, 断开5分钟无操作自动恢复导出, 也可手动恢复。恢复后 U 盘重新出现在车上。</div>
    <div id="arch-hint" class="hint" style="display:none">归档区 (F50 内部存储): 查看器用的视频副本、皮肤、文件中转。</div>
    <div id="dir-nav" style="display:none">
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input id="dest-dir" placeholder="/" style="flex:1;background:#161c22;border:1px solid #3a4652;border-radius:8px;color:#e8eaed;padding:8px 10px;font-size:12px;outline:none">
        <button onclick="goDir(document.getElementById('dest-dir').value)" style="background:#e8443f;border:none;color:#fff;border-radius:8px;padding:0 14px;font-size:12px;cursor:pointer">跳转</button>
      </div>
      <div id="crumb" style="font-size:12px;color:#5ea9ff;margin-bottom:8px;line-height:1.8"></div>
      <label class="upbtn" id="upbtn">＋ 上传文件到当前目录 (任意类型, ≤100MB)<input type="file" id="anyfile-file" hidden></label>
      <div id="file-list"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let data = {}, cur = null, syncTimer = null, currentCat = 'SentryClips', lastSig = '';
const CATS_NAME = __CAT_NAMES__;

async function load() {
  data = await (await fetch('/api/events')).json();
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  for (const c of Object.keys(data)) {
    const b = document.createElement('button');
    b.className = 'tab' + (c === 'SentryClips' ? ' active' : '');
    b.textContent = `${CATS_NAME[c] || c} (${data[c].length})`;
    b.onclick = () => { document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); b.classList.add('active'); currentCat = c; render(c); };
    tabs.appendChild(b);
  }
  document.getElementById('stat').textContent = `归档 ${Object.values(data).reduce((a, v) => a + v.length, 0)} 个事件`;
  lastSig = sigOf(data);
  render(currentCat);
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
    dot.style.background = '#4cc38a';
    if (live.pending > 0) txt.textContent = `实时同步 · ${live.pending} 个待确认`;
    else if (live.ago !== null && live.ago !== undefined) txt.textContent = `实时同步 · ${live.ago}s 前`;
    else txt.textContent = '实时同步中';
  } else {
    dot.style.background = '#e8443f';
    txt.textContent = '同步服务未运行';
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
        if (c) b.textContent = `${CATS_NAME[c] || c} (${d[c].length})`;
      });
      document.getElementById('stat').textContent = `归档 ${Object.values(d).reduce((a, v) => a + v.length, 0)} 个事件`;
      if (!document.getElementById('player').classList.contains('open')) render(currentCat);
    }
  } catch (e) {}
}
refreshLive();
setInterval(refreshLive, 10000);

function render(cat) {
  const g = document.getElementById('grid'), e = document.getElementById('empty');
  g.innerHTML = '';
  const evs = data[cat] || [];
  e.style.display = evs.length ? 'none' : 'block';
  for (const ev of evs) {
    const card = document.createElement('div'); card.className = 'card';
    const vids = ev.videos;
    let th = '<div class="thumb">';
    if (vids.length) vids.forEach(v => th += `<video src="${v.url}" preload="metadata" muted></video>`);
    else th = '<div class="thumb"><div class="ph">🎞</div></div>';
    card.innerHTML = th + `<div class="meta"><div class="t">${ev.name}</div><div class="s">${CATS_NAME[cat] || cat} · ${vids.length} 路画面 · 点击放大播放</div></div>`;
    card.onclick = () => openPlayer(ev);
    g.appendChild(card);
  }
}

function openPlayer(ev) {
  cur = ev;
  const vg = document.getElementById('vgrid'); vg.innerHTML = '';
  ev.videos.forEach(v => {
    const vid = document.createElement('video');
    vid.src = v.url; vid.muted = true; vid.preload = 'auto';
    vg.appendChild(vid);
  });
  document.getElementById('player').classList.add('open');
  playAll();
  if (syncTimer) clearInterval(syncTimer);
  syncTimer = setInterval(tick, 250);
}
function playAll() { document.querySelectorAll('#vgrid video').forEach(v => v.play()); }
function pauseAll() { document.querySelectorAll('#vgrid video').forEach(v => v.pause()); }
function setSpeed(s) { document.querySelectorAll('#vgrid video').forEach(v => v.playbackRate = parseFloat(s)); }
function tick() {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (!vs.length || !vs[0].duration) return;
  const t = vs[0].currentTime;
  vs.forEach(v => { if (Math.abs(v.currentTime - t) > 0.35) { v.currentTime = t; if (!v.paused) v.play(); } });
  document.getElementById('seek').value = Math.round(t / vs[0].duration * 1000);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  document.getElementById('ptime').textContent = `${m}:${String(s).padStart(2, '0')}`;
}
document.getElementById('seek').oninput = function () {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (vs.length && vs[0].duration) vs.forEach(v => v.currentTime = this.value / 1000 * vs[0].duration);
};
function closePlayer() {
  pauseAll();
  if (syncTimer) clearInterval(syncTimer);
  document.getElementById('player').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closePlayer(); closePanel(); } });

/* ---------- 设置面板 ---------- */
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2500);
}
function stab(which) {
  document.getElementById('st-skin').classList.toggle('active', which === 'skin');
  document.getElementById('st-audio').classList.toggle('active', which === 'audio');
  document.getElementById('st-files').classList.toggle('active', which === 'files');
  document.getElementById('tab-skin').style.display = which === 'skin' ? '' : 'none';
  document.getElementById('tab-audio').style.display = which === 'audio' ? '' : 'none';
  document.getElementById('tab-files').style.display = which === 'files' ? '' : 'none';
}
document.getElementById('gear').onclick = () => {
  document.getElementById('mask').classList.add('open');
  document.getElementById('panel').classList.add('open');
  refreshSkins(); refreshAudio(); refreshFiles();
};
function closePanel() {
  document.getElementById('mask').classList.remove('open');
  document.getElementById('panel').classList.remove('open');
}

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
  const j = await uploadFile(this, '/api/upload/audio');
  if (j && j.ok) refreshAudio();
};

/* 文件管理器 */
let curDir = '';
let area = 'usb';  // 'usb' = 车机U盘镜像, 'archive' = 归档区
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function switchArea(a) {
  area = a;
  document.getElementById('area-usb').classList.toggle('active', a === 'usb');
  document.getElementById('area-archive').classList.toggle('active', a === 'archive');
  document.getElementById('usb-hint').style.display = a === 'usb' ? '' : 'none';
  document.getElementById('arch-hint').style.display = a === 'archive' ? '' : 'none';
  curDir = a === 'usb' ? '' : '/archive/files';
  refreshFiles();
}
function usbStatusHtml(attached, msg) {
  if (!attached) {
    return '<div style="background:#3a2a12;border-radius:10px;padding:10px 12px;font-size:12.5px;color:#f0b429;line-height:1.6">' +
      '⚠ U盘当前未连接管理 (车机正常读写中)<br>' +
      '<button onclick="usbAttach()" style="background:#e8443f;border:none;color:#fff;border-radius:8px;padding:7px 16px;font-size:12.5px;cursor:pointer;margin-top:6px">🔗 连接车机U盘 (临时断开车机读取)</button></div>';
  }
  return '<div style="background:#1a3325;border-radius:10px;padding:10px 12px;font-size:12.5px;color:#4cc38a;line-height:1.6">' +
    '🟢 已连接 — U盘已从车机临时断开, 你在网页上的改动会直接写入车机U盘<br>' +
    '<span style="color:#7d8894;font-size:11.5px">断开满 5 分钟无操作会自动恢复车机导出</span><br>' +
    '<button onclick="usbDetach()" style="background:#242c35;border:none;color:#e8eaed;border-radius:8px;padding:7px 16px;font-size:12.5px;cursor:pointer;margin-top:6px">✅ 恢复车机导出</button></div>';
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
  toast(j.ok ? '✅ 已恢复车机导出' : '❌ ' + j.msg);
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
    statusEl.innerHTML = usbStatusHtml(j.attached, j.msg);
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
    a.style.cssText = 'color:' + (folder ? '#5ea9ff' : '#7d8894') + ';cursor:pointer;text-decoration:underline';
    a.onclick = () => goDir(target);
    crumb.appendChild(a);
    const s = document.createElement('span'); s.textContent = ' / '; s.style.color = '#3a4652';
    crumb.appendChild(s);
  };
  if (area === 'usb') {
    mkLink('🚗 U盘根目录', '', true);
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
    const n = document.createElement('span'); n.className = 'n';
    n.textContent = '📁 ' + d; n.style.cssText = 'cursor:pointer;color:#5ea9ff';
    n.onclick = () => goDir(area === 'usb' ? (curDir ? curDir + '/' + d : d) : curDir + '/' + d);
    el.appendChild(n); list.appendChild(el);
  }
  for (const f of j.files) {
    empty = false;
    const d = document.createElement('div'); d.className = 'item';
    const n = document.createElement('span'); n.className = 'n'; n.textContent = f.name;
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = fmtSize(f.size);
    const btnDl = document.createElement('button'); btnDl.className = 'primary'; btnDl.textContent = '下载';
    btnDl.onclick = () => {
      const full = area === 'usb' ? (curDir ? curDir + '/' + f.name : f.name) : curDir + '/' + f.name;
      location.href = (area === 'usb' ? '/usbdl?path=' : '/dl?path=') + encodeURIComponent(full);
    };
    const btnDel = document.createElement('button'); btnDel.className = 'danger'; btnDel.textContent = '删除';
    btnDel.onclick = () => delFile(area === 'usb' ? (curDir ? curDir + '/' + f.name : f.name) : curDir + '/' + f.name);
    d.append(n, tag, btnDl, btnDel); list.appendChild(d);
  }
  if (empty) list.innerHTML = '<div class="hint" style="margin:0">空目录</div>';
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
load();
</script></body></html>"""


def ensure_dirs():
    for d in (SKIN_DIR, AUDIO_Q, AUDIO_DONE, FILES_DIR, ARCHIVE):
        os.makedirs(d, exist_ok=True)
    for c in CATS:
        os.makedirs(os.path.join(ARCHIVE, c), exist_ok=True)


def safe_name(name):
    name = os.path.basename(name or "")
    return name if re.match(r'^[\w\-. ]{1,80}$', name) else None


def list_events():
    out = {}
    for cat in CATS:
        base = os.path.join(ARCHIVE, cat)
        events = []
        if os.path.isdir(base):
            for name in sorted(os.listdir(base), reverse=True):
                d = os.path.join(base, name)
                if not os.path.isdir(d):
                    continue
                vids = []
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".mp4"):
                        cam = f.rsplit("-", 1)[-1][:-4] if "-" in f else f
                        vids.append({"url": f"/media/{cat}/{name}/{f}", "cam": cam})
                events.append({"name": name, "videos": vids})
        out[cat] = events
    return out


def safe_archive_dir(p):
    """校验目录必须落在 /archive 内 (防路径逃逸), 返回 realpath 或 None"""
    p = (p or "").strip()
    if not p.startswith("/archive"):
        return None
    rp = os.path.realpath(p)
    if rp != "/archive" and not rp.startswith("/archive/"):
        return None
    return rp


def safe_archive_path(p):
    """校验文件路径必须落在 /archive 内且真实存在"""
    p = (p or "").strip()
    if not p.startswith("/archive"):
        return None
    rp = os.path.realpath(p)
    if not rp.startswith("/archive/") or not os.path.isfile(rp):
        return None
    return rp


# ---------- 车机U盘(镜像)临时管理 ----------
usb_lock = threading.Lock()
usb_attached = False
usb_timer = None


def _udc():
    try:
        return os.listdir("/sys/class/udc")[0]
    except Exception:
        return None


def _gadget_rebind(bind):
    udc = _udc()
    if not udc:
        return False
    try:
        with open(os.path.join(find_gadget(), "UDC"), "w") as f:
            f.write(udc if bind else "")
        return True
    except Exception:
        return False


def usb_attach():
    """临时断开车机读取, 挂载镜像到 MNT (读写). 返回 (ok, msg)"""
    global usb_attached, usb_timer
    with usb_lock:
        if usb_attached and os.path.ismount(MNT):
            return True, "already"
        os.makedirs(MNT, exist_ok=True)
        _gadget_rebind(False)  # 停止导出
        time.sleep(1)
        r = losetup_cmd([LOOPDEV, IMG])
        if r.returncode != 0:
            _gadget_rebind(True)
            return False, "losetup 失败: " + r.stderr.strip()[-120:]
        r = subprocess.run(["mount", "-t", "exfat", LOOPDEV, MNT], capture_output=True, text=True)
        if r.returncode != 0:
            losetup_cmd(["-d", LOOPDEV])
            _gadget_rebind(True)
            return False, "挂载失败: " + r.stderr.strip()[-120:]
        usb_attached = True
        # 无操作自动恢复
        if usb_timer:
            usb_timer.cancel()
        usb_timer = threading.Timer(USB_AUTO_DETACH, usb_detach_safe)
        usb_timer.daemon = True
        usb_timer.start()
        return True, "attached"


def usb_detach():
    """卸载镜像并恢复车机导出. 返回 (ok, msg)"""
    global usb_attached, usb_timer
    with usb_lock:
        if usb_timer:
            usb_timer.cancel()
            usb_timer = None
        if os.path.ismount(MNT):
            subprocess.run(["sync"], capture_output=True)
            for _ in range(3):
                r = subprocess.run(["umount", MNT], capture_output=True, text=True)
                if r.returncode == 0:
                    break
                time.sleep(2)
            else:
                return False, "卸载失败, 有进程占用 " + MNT
        losetup_cmd(["-d", LOOPDEV])
        _gadget_rebind(True)
        usb_attached = False
        return True, "detached"


def usb_detach_safe():
    try:
        ok, msg = usb_detach()
        print("auto detach:", ok, msg)
    except Exception as e:
        print("auto detach error:", e)


def usb_real(p):
    """校验镜像内相对路径, 返回挂载点下真实绝对路径或 None"""
    p = (p or "").strip().strip("/")
    if not p:
        return MNT
    rp = os.path.realpath(os.path.join(MNT, p))
    if rp != MNT and not rp.startswith(MNT + "/"):
        return None
    return rp


def serve_file(self, fp):
    """流式发送文件 (支持 Range), 用于下载"""
    fn = os.path.basename(fp)
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
          "css": "text/css", "html": "text/html", "htm": "text/html", "txt": "text/plain",
          "json": "application/json", "pdf": "application/pdf", "svg": "image/svg+xml",
          "mp4": "video/mp4", "mp3": "audio/mpeg", "wav": "audio/wav"}.get(ext, "application/octet-stream")
    size = os.path.getsize(fp)
    rng = self.headers.get("Range")
    if rng:
        mm2 = re.match(r"bytes=(\d+)-(\d*)", rng)
        start = int(mm2.group(1))
        end = int(mm2.group(2)) if mm2.group(2) else size - 1
    else:
        start, end = 0, size - 1
    length = end - start + 1
    self.send_response(206 if rng else 200)
    self.send_header("Content-Type", ct)
    self.send_header("Content-Length", str(length))
    self.send_header("Accept-Ranges", "bytes")
    self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(fn)}"')
    if rng:
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    self.end_headers()
    with open(fp, "rb") as f:
        f.seek(start)
        remain = length
        while remain > 0:
            chunk = f.read(min(65536, remain))
            if not chunk:
                break
            self.wfile.write(chunk)
            remain -= len(chunk)


LIVE_SCRIPT = "tesla-live-sync.py"
LIVE_LOG = cfg("TSF_LIVE_LOG", os.path.join(BASE_DIR, "tesla-live.log"))
LIVE_STATE = cfg("TSF_LIVE_STATE", os.path.join(BASE_DIR, ".tesla-live-state.json"))


def _proc_running(needle):
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if needle.encode() in f.read():
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def live_status():
    """车机在线只读同步的健康状态 (给页面显示心跳)"""
    running = _proc_running(LIVE_SCRIPT)
    pending = 0
    try:
        with open(LIVE_STATE) as f:
            pending = len(json.load(f).get("__pending__", {}))
    except Exception:
        pass
    ago, last = None, ""
    try:
        st = os.stat(LIVE_LOG)
        ago = int(time.time() - st.st_mtime)
        with open(LIVE_LOG, "rb") as f:
            f.seek(max(0, st.st_size - 2048))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        for line in reversed(lines):
            if "已同步" in line:
                last = line.split("] ", 1)[-1]
                break
        if not last and lines:
            last = lines[-1].split("] ", 1)[-1]
    except Exception:
        pass
    return {"running": running, "pending": pending, "ago": ago, "last": last}


def active_skin():
    ap = os.path.join(SKIN_DIR, "active.json")
    try:
        with open(ap) as f:
            return json.load(f).get("skin")
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _read_upload(self):
        """解析 multipart: 返回 (文件名, 文件数据, 普通字段dict)"""
        ctype = self.headers.get("Content-Type", "")
        mm = re.search(r'boundary=([^;]+)', ctype)
        if "multipart/form-data" not in ctype or not mm:
            return None, None, {}
        boundary = mm.group(1).strip('"').encode()
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD:
            return "TOOBIG", None, {}
        body = self.rfile.read(length)
        fields = {}
        file_fn = None
        file_data = None
        for part in body.split(b"--" + boundary):
            i = part.find(b"\r\n\r\n")
            if i == -1:
                continue
            headers = part[:i].decode("utf-8", "replace")
            data = part[i + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            nm = re.search(r'name="([^"]*)"', headers)
            if 'filename="' in headers:
                fn = re.search(r'filename="([^"]*)"', headers)
                if fn and fn.group(1) and data:
                    file_fn, file_data = fn.group(1), data
            elif nm and nm.group(1):
                fields[nm.group(1)] = data.decode("utf-8", "replace").strip()
        return file_fn, file_data, fields

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path in ("/", "/index.html"):
            html = HTML.replace("__CAT_NAMES__", json.dumps(CAT_NAMES, ensure_ascii=False))
            self._send(200, html.encode())
        elif path == "/api/events":
            self._json(list_events())
        elif path == "/api/live":
            self._json(live_status())
        elif path == "/api/skins":
            skins = sorted(f for f in os.listdir(SKIN_DIR)
                           if f != "active.json" and os.path.isfile(os.path.join(SKIN_DIR, f)))
            self._json({"skins": skins, "active": active_skin()})
        elif path == "/api/skin":
            s = active_skin()
            if s and s.endswith(".css") and os.path.isfile(os.path.join(SKIN_DIR, s)):
                with open(os.path.join(SKIN_DIR, s), "rb") as f:
                    self._send(200, f.read(), "text/css; charset=utf-8")
            else:
                self._send(204, b"", "text/css")
        elif path.startswith("/skin-media/"):
            fn = safe_name(path[len("/skin-media/"):])
            fp = os.path.join(SKIN_DIR, fn) if fn else ""
            if fn and os.path.isfile(fp):
                ext = fn.rsplit(".", 1)[-1].lower()
                ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "css": "text/css"}.get(ext, "application/octet-stream")
                with open(fp, "rb") as f:
                    self._send(200, f.read(), ct)
            else:
                self._send(404, b"not found")
        elif path == "/api/audio":
            q = sorted(f for f in os.listdir(AUDIO_Q) if os.path.isfile(os.path.join(AUDIO_Q, f)))
            d = sorted(f for f in os.listdir(AUDIO_DONE) if os.path.isfile(os.path.join(AUDIO_DONE, f)))
            self._json({"queue": q, "done": d})
        elif path == "/api/browse":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            d = safe_archive_dir(q.get("dir", [""])[0])
            if not d or not os.path.isdir(d):
                self._json({"ok": False, "msg": "目录不存在或不在 /archive 内"}, 404)
                return
            dirs, files = [], []
            for e in sorted(os.listdir(d)):
                ep = os.path.join(d, e)
                if os.path.isdir(ep):
                    dirs.append(e)
                elif os.path.isfile(ep):
                    files.append({"name": e, "size": os.path.getsize(ep)})
            self._json({"ok": True, "dir": d, "dirs": dirs, "files": files})
        elif path == "/api/usb/browse":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = usb_real(q.get("dir", [""])[0])
            if not usb_attached or not os.path.ismount(MNT):
                self._json({"ok": False, "attached": False, "msg": "车机U盘未连接"})
                return
            if fp is None or not os.path.isdir(fp):
                self._json({"ok": False, "attached": True, "msg": "目录不存在"}, 404)
                return
            dirs, files = [], []
            for e in sorted(os.listdir(fp)):
                ep = os.path.join(fp, e)
                if os.path.isdir(ep):
                    dirs.append(e)
                elif os.path.isfile(ep):
                    files.append({"name": e, "size": os.path.getsize(ep)})
            rel = os.path.relpath(fp, MNT).replace("\\", "/")
            self._json({"ok": True, "attached": True, "dir": "" if rel == "." else rel,
                        "dirs": dirs, "files": files})
        elif path == "/usbdl":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = usb_real(q.get("path", [""])[0])
            if not fp or not os.path.isfile(fp):
                self._send(404, b"not found")
                return
            serve_file(self, fp)
        elif path == "/dl":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = safe_archive_path(q.get("path", [""])[0])
            if not fp:
                self._send(404, b"not found")
                return
            serve_file(self, fp)
        else:
            m = re.match(r"^/media/([\w-]+)/([\w\-. ]+)/([\w\-. ]+)$", path)
            if not m:
                self._send(404, b"not found")
                return
            fp = os.path.normpath(os.path.join(ARCHIVE, m.group(1), m.group(2), m.group(3)))
            if not fp.startswith(ARCHIVE) or not os.path.isfile(fp):
                self._send(404, b"not found")
                return
            size = os.path.getsize(fp)
            rng = self.headers.get("Range")
            if rng:
                mm2 = re.match(r"bytes=(\d+)-(\d*)", rng)
                start = int(mm2.group(1))
                end = int(mm2.group(2)) if mm2.group(2) else size - 1
            else:
                start, end = 0, size - 1
            length = end - start + 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(65536, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)

    def do_POST(self):
        path = urllib.parse.unquote(self.path)
        if path == "/api/upload/skin":
            fn, data, _f = self._read_upload()
            if fn == "TOOBIG":
                self._json({"ok": False, "msg": "文件超过 3MB"}, 400)
                return
            fn = safe_name(fn)
            if not fn or not data or not fn.lower().endswith((".css", ".jpg", ".jpeg", ".png", ".webp")):
                self._json({"ok": False, "msg": "仅支持 css/jpg/png/webp"}, 400)
                return
            with open(os.path.join(SKIN_DIR, fn), "wb") as f:
                f.write(data)
            activated = False
            if fn.endswith(".css"):
                with open(os.path.join(SKIN_DIR, "active.json"), "w") as f:
                    json.dump({"skin": fn}, f)
                activated = True
            self._json({"ok": True, "msg": ("皮肤已上传并应用" if activated else "图片已上传, 请在 CSS 中用 /skin-media/" + fn), "activated": activated})
        elif path == "/api/upload/audio":
            fn, data, _f = self._read_upload()
            if fn == "TOOBIG":
                self._json({"ok": False, "msg": "文件超过 3MB (车机要求 <1MB)"}, 400)
                return
            fn = safe_name(fn)
            if not fn or not data or not fn.lower().endswith((".wav", ".mp3")):
                self._json({"ok": False, "msg": "仅支持 wav/mp3"}, 400)
                return
            if len(data) > 1024 * 1024:
                self._json({"ok": False, "msg": "车机要求单个音频 <1MB, 请裁剪后重试"}, 400)
                return
            with open(os.path.join(AUDIO_Q, fn), "wb") as f:
                f.write(data)
            self._json({"ok": True, "msg": "已加入队列, 归档同步时写入车机U盘"})
        elif path == "/api/usb/attach":
            ok, msg = usb_attach()
            self._json({"ok": ok, "msg": "车机U盘已连接 (已临时断开车机读取, 5分钟无操作自动恢复)" if ok and msg != "already" else ("已处于管理状态" if ok else msg)})
        elif path == "/api/usb/detach":
            ok, msg = usb_detach()
            self._json({"ok": ok, "msg": "已恢复车机导出" if ok else msg})
        elif path == "/api/upload/usb":
            fn, data, fields = self._read_upload()
            if fn == "TOOBIG":
                self._json({"ok": False, "msg": "文件超过 100MB 上限"}, 400)
                return
            fn = safe_name(fn)
            if not fn or not data:
                self._json({"ok": False, "msg": "无效的文件"}, 400)
                return
            if not usb_attached or not os.path.ismount(MNT):
                self._json({"ok": False, "msg": "车机U盘未连接, 请先点「连接车机U盘」"}, 400)
                return
            dest = usb_real(fields.get("path"))
            if dest is None or not os.path.isdir(dest):
                self._json({"ok": False, "msg": "目标目录不存在"}, 400)
                return
            with open(os.path.join(dest, fn), "wb") as f:
                f.write(data)
            subprocess.run(["sync"], capture_output=True)
            rel = os.path.relpath(dest, MNT).replace("\\", "/")
            self._json({"ok": True, "msg": f"已上传到 U盘/{'' if rel == '.' else rel + '/' }{fn} ({len(data)//1024} KB)"})
        elif path == "/api/upload/file":
            fn, data, fields = self._read_upload()
            if fn == "TOOBIG":
                self._json({"ok": False, "msg": "文件超过 100MB 上限"}, 400)
                return
            fn = safe_name(fn)
            if not fn or not data:
                self._json({"ok": False, "msg": "无效的文件"}, 400)
                return
            req_path = (fields.get("path") or "").strip()
            dest_dir = safe_archive_dir(req_path)
            if req_path and not dest_dir:
                self._json({"ok": False, "msg": "目标路径无效 (必须在 /archive 内)"}, 400)
                return
            if not dest_dir:
                dest_dir = FILES_DIR
            os.makedirs(dest_dir, exist_ok=True)
            with open(os.path.join(dest_dir, fn), "wb") as f:
                f.write(data)
            self._json({"ok": True, "msg": f"已上传到 {dest_dir}/{fn} ({len(data)//1024} KB)"})
        elif path == "/api/skin/activate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                name = json.loads(self.rfile.read(length)).get("name")
            except Exception:
                name = None
            name = safe_name(name)
            if name and os.path.isfile(os.path.join(SKIN_DIR, name)):
                with open(os.path.join(SKIN_DIR, "active.json"), "w") as f:
                    json.dump({"skin": name}, f)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "皮肤不存在"}, 404)
        elif path == "/api/audio/setlockchime":
            length = int(self.headers.get("Content-Length", 0))
            try:
                name = json.loads(self.rfile.read(length)).get("name")
            except Exception:
                name = None
            name = safe_name(name)
            src = os.path.join(AUDIO_Q, name) if name else ""
            if name and os.path.isfile(src):
                with open(src, "rb") as f:
                    data = f.read()
                if not name.lower().endswith(".wav"):
                    self._json({"ok": False, "msg": "锁车音必须是 WAV (mp3 只能当喇叭音效)"})
                    return
                with open(os.path.join(AUDIO_Q, "LockChime.wav"), "wb") as f:
                    f.write(data)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "文件不存在"}, 404)
        else:
            self._send(404, b"not found")

    def do_DELETE(self):
        path = urllib.parse.unquote(self.path)
        if path.startswith("/api/skin/"):
            name = safe_name(path[len("/api/skin/"):])
            fp = os.path.join(SKIN_DIR, name) if name else ""
            if name and os.path.isfile(fp):
                os.remove(fp)
                if active_skin() == name:
                    os.remove(os.path.join(SKIN_DIR, "active.json"))
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        elif path.startswith("/api/audio/"):
            name = safe_name(path[len("/api/audio/"):].split("?")[0])
            done = "done=1" in self.path
            base = AUDIO_DONE if done else AUDIO_Q
            fp = os.path.join(base, name) if name else ""
            if name and os.path.isfile(fp) and name != "LockChime.wav":
                os.remove(fp)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        elif path.startswith("/api/usb"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = usb_real(q.get("path", [""])[0])
            if not fp or not os.path.isfile(fp) or fp == MNT:
                self._json({"ok": False, "msg": "not found"}, 404)
                return
            os.remove(fp)
            subprocess.run(["sync"], capture_output=True)
            self._json({"ok": True})
        elif path.startswith("/api/file"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = safe_archive_path(q.get("path", [""])[0])
            if fp:
                os.remove(fp)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        else:
            self._send(404, b"not found")


if __name__ == "__main__":
    ensure_dirs()
    # 启动自愈: 若上次退出时镜像仍处于管理挂载状态, 恢复导出
    if os.path.ismount(MNT):
        subprocess.run(["umount", MNT], capture_output=True)
    losetup_cmd(["-d", LOOPDEV])
    _gadget_rebind(True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sentry viewer on 0.0.0.0:{PORT}")
    srv.serve_forever()
