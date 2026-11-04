#!/usr/bin/env python3
"""Tesla 哨兵视频查看器 - 单文件, 纯标准库, 无依赖
功能: 多路播放器(4路/6路自适应) / 时间线 / 皮肤上传 / 锁车语音上传(经归档同步写入车机U盘)
访问: http://<f50-ip>:8080
"""
import json
import os
import re
import hashlib
import struct
import subprocess
import threading
import time
import urllib.parse
import zipfile
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 路径/端口优先取环境变量 (install.sh 写入 /etc/tesla-sentry.conf, 服务单元以 EnvironmentFile 注入),
# 便于装到非 F50 设备上时改归档区、端口、缩略图缓存位置
ARCHIVE = os.environ.get("TSF_ARCHIVE", "/archive")
SKIN_DIR = os.path.join(ARCHIVE, ".skins")
AUDIO_Q = os.path.join(ARCHIVE, "boombox-queue")
AUDIO_DONE = os.path.join(ARCHIVE, "boombox-done")
FILES_DIR = os.path.join(ARCHIVE, "files")
PORT = int(os.environ.get("TSF_PORT", "8080"))
CATS = ["SentryClips", "SavedClips", "RecentClips"]
CAT_NAMES = {"SentryClips": "哨兵告警", "SavedClips": "手动保存", "RecentClips": "行车记录"}
MAX_UPLOAD = 100 * 1024 * 1024  # 通用文件上传上限 100MB (皮肤/音频有自己的更小限制)

HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>特斯拉哨兵查看器</title>
<style id="base">
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:#0e1216; color:#e8eaed; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
header { padding:12px 16px; background:linear-gradient(180deg,#1d242e,#171d24); display:flex; flex-wrap:wrap; align-items:center; gap:10px; position:sticky; top:0; z-index:10; box-shadow:0 1px 0 #ffffff0d; }
header h1 { font-size:16px; font-weight:700; white-space:nowrap; letter-spacing:.5px; }
header .sub { font-size:12px; color:#8d99a5; white-space:nowrap; }
#live { margin-left:auto; }
.tabs { display:flex; gap:6px; overflow-x:auto; scrollbar-width:none; }
.tabs::-webkit-scrollbar { display:none; }
.tab { padding:6px 13px; border-radius:18px; background:#232b35; color:#9aa4af; cursor:pointer; font-size:12.5px; border:1px solid #ffffff08; white-space:nowrap; flex:0 0 auto; transition:background .15s,color .15s; }
.tab:hover { color:#e8eaed; }
.tab.active { background:#e8443f; color:#fff; box-shadow:0 2px 10px #e8443f55; }
.filters { display:none; gap:6px; overflow-x:auto; padding:10px 16px 0; scrollbar-width:none; }
.filters.show { display:flex; }
.filters::-webkit-scrollbar { display:none; }
.chip { padding:5px 12px; border-radius:14px; background:#1e252e; color:#98a3ae; border:1px solid #2a333d;
  font-size:12px; cursor:pointer; white-space:nowrap; flex:0 0 auto; transition:background .15s,color .15s; }
.chip:hover { color:#e8eaed; border-color:#3a4653; }
.chip.on { background:#2f6ee0; border-color:#2f6ee0; color:#fff; }
.chip .n { opacity:.62; margin-left:4px; font-variant-numeric:tabular-nums; }
@media (max-width:640px){
  header .sub { display:none; } .tabs { order:10; width:100%; margin-left:0; } #gear { order:5; margin-left:0; }
  /* 播放器手机端紧凑化: 面板/卡片占高更小, 视频尽量多露 */
  .pscroll { padding:2px 8px 10px; gap:8px; }
  .tpanel { padding:10px 12px 8px; }
  .tp-spd { font-size:26px; }
  .tp-date { font-size:12px; } .tp-loc { font-size:11.5px; }
  .tp-gear { width:22px; height:22px; font-size:12px; }
  .tp-c { width:28px; height:28px; } .tp-c.st svg { width:16px; height:16px; }
  .tp-badge { height:25px; font-size:11px; padding:0 8px 0 6px; } .tp-badge svg { width:13px; height:13px; }
  .infocard { padding:10px 12px 8px; }
  .ptop { padding:6px 10px; }
}
#gear { background:#242c35; border:none; color:#9aa4af; font-size:18px; cursor:pointer; border-radius:10px; width:36px; height:32px; }
#gear:hover { color:#fff; }
.grid-wrap { padding:16px; display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
.card { background:linear-gradient(180deg,#1c232c,#181e26); border-radius:14px; overflow:hidden; cursor:pointer; transition:transform .15s,border-color .15s; border:1px solid #232b34; box-shadow:0 4px 14px #00000066; }
.card:hover { transform:translateY(-2px); border-color:#e8443f66; }
.thumb { display:grid; grid-template-columns:repeat(3,1fr); gap:2px; background:#000; aspect-ratio:16/9; }
.thumb video { width:100%; height:100%; object-fit:cover; pointer-events:none; }
.thumb.one { position:relative; display:block; background:#0b0f13; }
.thumb.one img { width:100%; height:100%; object-fit:cover; display:block; }
.cnum { position:absolute; right:7px; bottom:7px; background:#000000b8; color:#fff; font-size:11px; line-height:1; padding:4px 7px; border-radius:9px; }
.thumb .ph { background:#0b0f13; display:flex; align-items:center; justify-content:center; color:#3a4652; font-size:22px; }
.meta { padding:10px 13px; }
.meta .t { font-size:13.5px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.meta .s { font-size:11.5px; color:#9aa4af; margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.player { display:none; position:fixed; inset:0; height:100vh; height:100dvh; background:#0d1117; z-index:100; flex-direction:column; }
.player.open { display:flex; }
.ptop { display:flex; align-items:center; gap:10px; padding:10px 14px; }
.ptop .pdl { margin-left:auto; }
.pback { background:none; border:none; color:#fff; font-size:16.5px; cursor:pointer; padding:4px 2px; white-space:nowrap; }
.pdl { background:none; border:none; color:#fff; font-size:19px; cursor:pointer; padding:4px 6px; }
.pscroll { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch; padding:2px 14px 14px; display:flex; flex-direction:column; gap:12px; }
.pscroll > * { flex:0 0 auto; }
.vwrap { position:relative; border-radius:14px; overflow:hidden; max-width:980px; width:100%; margin:0 auto; }
/* 行车遥测面板 (视频上方, Tesla App 样式, 不遮挡画面) */
.tpanel { display:none; background:#1c232c; border-radius:14px; padding:14px 18px 10px; max-width:980px; width:100%; margin:0 auto; }
.tpanel.show { display:block; }
.tpanel.folded .tp-rows { display:none; }
.tp-top { display:flex; justify-content:space-between; align-items:center; }
.tp-l { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.tp-date { font-size:13.5px; color:#cfd6dd; font-variant-numeric:tabular-nums; }
.tp-loc { font-size:12.5px; color:#8d99a5; }
.tp-tr { display:flex; align-items:center; gap:9px; }
.tp-sig { font-size:16px; color:#3a4450; line-height:1; }
.tp-sig.on { color:#f59e0b; }
.tp-gear { background:#34c759; color:#fff; font-weight:800; font-size:14px; width:26px; height:26px;
  border-radius:50%; display:inline-flex; align-items:center; justify-content:center; }
.tp-mid { display:flex; align-items:center; margin-top:8px; }
.tp-spd { font-size:34px; font-weight:800; color:#fff; line-height:1.1; font-variant-numeric:tabular-nums; }
.tp-unit { font-size:13px; color:#9aa4af; font-weight:600; margin-left:6px; }
.tp-badges { display:flex; align-items:center; gap:10px; margin-left:auto; }
.tp-c { width:34px; height:34px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:11.5px; font-weight:700; flex:none; }
.tp-c.t { background:#3a4450; color:#e8edf2; font-size:15px; }
.tp-badge { display:inline-flex; align-items:center; gap:5px; height:30px; padding:0 11px 0 8px;
  border-radius:15px; font-size:12.5px; font-weight:700; font-variant-numeric:tabular-nums; flex:none; }
.tp-badge svg { width:16px; height:16px; flex:none; }
.tp-badge.thr { background:#1f7ae0; color:#fff; }
.tp-badge.brk { background:#2a323d; color:#8d99a5; }
.tp-badge.brk.on { background:#ff453a; color:#fff; box-shadow:0 0 9px #ff453a66; }
.tp-c.ap { background:linear-gradient(135deg,#f59e0b,#ef4444); color:#fff; font-size:10.5px; }
.tp-c.st { background:#2a323d; }
.tp-c.st svg { width:20px; height:20px; }
.tp-fold { display:block; margin:8px 0 0 auto; background:none; border:none; color:#8d99a5;
  font-size:12px; cursor:pointer; padding:4px 2px; }
.tp-rows { border-top:1px solid #2a323d; margin-top:2px; padding-top:8px; display:grid;
  grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:1px 20px; }
.tp-rows div { color:#e8edf2; font-size:13px; padding:3px 0; font-variant-numeric:tabular-nums; }
.tp-rows .k { color:#8d99a5; margin-right:9px; }
.vgrid { display:grid; gap:3px; background:#000; }
.vcell { position:relative; overflow:hidden; background:#000; cursor:zoom-in; aspect-ratio:16/9; }
.vcell.focused { grid-column:1/-1; grid-row:1/-1; cursor:zoom-out; }
.vcell video { width:100%; height:100%; background:#000; object-fit:cover; display:block; }
.startmask { position:absolute; inset:0; z-index:5; background:rgba(0,0,0,.55); color:#fff; font-size:16px; display:none; align-items:center; justify-content:center; cursor:pointer; letter-spacing:1px; }
.startmask.show { display:flex; }
.vcell .clabel { position:absolute; right:7px; bottom:7px; font-size:11px; color:#e8eef4; background:rgba(20,24,29,.72); border-radius:6px; padding:2px 7px; pointer-events:none; }
.infocard { background:#1d242e; border-radius:16px; padding:14px 16px 10px; max-width:980px; width:100%; margin:0 auto; box-shadow:0 6px 20px #0008; }
.ic-top { display:flex; align-items:center; gap:10px; }
.ic-time { font-size:14px; color:#c6cdd4; }
.ic-badge { margin-left:auto; background:#3b82f6; color:#fff; font-size:12px; font-weight:600; border-radius:14px; padding:3px 12px; }
.ic-mid { display:flex; align-items:baseline; gap:12px; margin:10px 0 2px; flex-wrap:wrap; }
.ic-big { font-size:30px; font-weight:700; letter-spacing:.5px; }
.ic-sub { font-size:12.5px; color:#8d99a5; }
.clipbar { display:none; gap:6px; flex-wrap:wrap; margin-top:9px; }
.clipbar.show { display:flex; }
.clipbar button { background:#232b36; color:#9aa6b2; border:1px solid #2d3843; border-radius:8px;
  font-size:12px; padding:4px 9px; cursor:pointer; font-variant-numeric:tabular-nums; }
.clipbar button.on { background:#2f6ee0; border-color:#2f6ee0; color:#fff; }
.ic-rows { display:none; font-size:12.5px; line-height:1.9; color:#aeb7c0; word-break:break-all; border-top:1px solid #ffffff10; margin-top:8px; padding-top:8px; }
.ic-rows.open { display:block; }
.ick { display:inline-block; min-width:64px; color:#8d99a5; }
.ic-fold { background:none; border:none; color:#8d99a5; font-size:12px; cursor:pointer; padding:4px 0 2px; margin-left:auto; display:block; }
.pbar { display:flex; align-items:center; gap:12px; padding:10px 16px calc(12px + env(safe-area-inset-bottom,0px)); max-width:1008px; margin:0 auto; width:100%; box-sizing:border-box; }
.pp { width:44px; height:44px; border-radius:50%; background:#fff; color:#14181d; border:none; font-size:16px; cursor:pointer; flex:0 0 auto; }
.pbar input[type=range] { flex:1; accent-color:#3b82f6; min-width:80px; }
.pbar .t { font-size:13.5px; color:#c6cdd4; min-width:44px; text-align:center; }
.spd { background:#3b82f6; color:#fff; border:none; border-radius:10px; padding:9px 15px; font-size:14px; font-weight:600; cursor:pointer; flex:0 0 auto; }
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
.expp { display:none; position:fixed; right:14px; bottom:70px; width:min(320px,92vw); background:#1d242e; border:1px solid #2a333e; border-radius:14px; padding:14px; z-index:150; box-shadow:0 10px 30px #000a; }
.expp.open { display:block; }
.expp .expt { font-size:13.5px; font-weight:600; margin-bottom:10px; display:flex; align-items:center; }
.expp .expt .x { margin-left:auto; background:none; border:none; color:#9aa4af; font-size:17px; cursor:pointer; }
.expitem { display:flex; align-items:center; gap:8px; padding:7px 4px; font-size:13px; color:#c6cdd4; cursor:pointer; }
.expitem input { accent-color:#e8443f; width:16px; height:16px; }
.expr { display:flex; gap:8px; margin-top:10px; }
.expr .mini { flex:1; padding:7px 0; border-radius:9px; background:#242c35; border:none; color:#c6cdd4; font-size:12.5px; cursor:pointer; }
.expr .mini.primary { background:#e8443f; color:#fff; }
.stitch { display:none; position:fixed; inset:0; background:rgba(0,0,0,.72); z-index:260; align-items:center; justify-content:center; }
.stitch.open { display:flex; }
.stbox { width:min(340px,88vw); background:#1d242e; border-radius:16px; padding:20px 20px 16px; box-shadow:0 12px 40px #000c; text-align:center; }
.stt { font-size:14.5px; font-weight:600; margin-bottom:14px; }
.stbar { height:8px; border-radius:5px; background:#2a333e; overflow:hidden; }
.stfill { height:100%; width:0; background:linear-gradient(90deg,#3b82f6,#60a5fa); border-radius:5px; transition:width .4s; }
.stpct { font-size:13px; color:#8d99a5; margin-top:8px; }
.sthint { font-size:11.5px; color:#7d8894; margin-top:4px; line-height:1.5; }
</style></head><body>
<header><h1>🚗 特斯拉哨兵查看器</h1><span class="sub" id="stat"></span>
<span id="live" title="车机在线只读同步状态" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#7d8894;background:#1a2027;border:1px solid #232b34;border-radius:14px;padding:4px 10px;white-space:nowrap"><span id="livedot" style="width:7px;height:7px;border-radius:50%;background:#5f6b76"></span><span id="livetxt">同步状态</span></span>
<nav class="tabs" id="tabs"></nav><button id="gear" title="皮肤与锁车音">⚙</button></header>
<nav class="filters" id="filters"></nav>
<div class="grid-wrap" id="grid"></div>
<div class="empty" id="empty" style="display:none"><div class="big">📹</div>
<div>暂无归档视频<br><small style="font-size:12px">车机录制后, 归档同步任务会自动把视频收进来 (镜像需空闲10分钟)</small></div></div>

<div class="player" id="player">
  <div class="ptop">
    <button class="pback" onclick="closePlayer()">‹ 返回</button>
    <button class="pdl" onclick="openExport()" title="导出">⬇</button>
  </div>
  <div class="pscroll">
    <div class="tpanel" id="tpanel">
        <div class="tp-top">
          <span class="tp-l"><span class="tp-date" id="tp-date">—</span><span class="tp-loc" id="tp-loc"></span></span>
          <span class="tp-tr"><span class="tp-sig" id="tp-bl">⬅</span><span class="tp-sig" id="tp-br">➡</span><span class="tp-gear" id="tp-gear">D</span></span>
        </div>
        <div class="tp-mid">
          <span class="tp-spd" id="tp-spd">0</span><span class="tp-unit">km/h</span>
          <span class="tp-badges">
            <span class="tp-badge brk" id="tp-brk" title="刹车"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="7.6"/><circle cx="12" cy="12" r="2.5"/><path d="M17.4 6.6l2.7-2.7"/></svg><b id="tp-brk-n">0%</b></span>
            <span class="tp-badge thr" id="tp-thr" title="油门"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M9.4 3.6h5.2a1.7 1.7 0 0 1 1.68 1.93l-1.35 12.3a1.8 1.8 0 0 1-1.79 1.6h-2.28a1.8 1.8 0 0 1-1.79-1.6L7.72 5.53A1.7 1.7 0 0 1 9.4 3.6z"/><path d="M8.6 15.1h6.8"/></svg><b id="tp-thr-n">0%</b></span>
            <span class="tp-c st" id="tp-st" title="方向盘角度"><svg viewBox="0 0 24 24" fill="none" stroke="#cfd6dd" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.6"/><path d="M3.2 10.5h6M14.8 10.5h6M12 14.6V21"/></svg></span>
            <span class="tp-c ap" id="tp-ap" style="display:none">AP</span>
          </span>
        </div>
        <button class="tp-fold" id="tp-fold" onclick="toggleTelRows()">收起详细数据 ⌃</button>
        <div class="tp-rows" id="tp-rows"></div>
    </div>
    <div class="vwrap">
      <div class="startmask" id="startmask">▶ 轻触开始同步播放</div>
      <div class="vgrid" id="vgrid"></div>
    </div>
    <div class="infocard">
      <div class="ic-top"><span class="ic-time" id="ic-time">—</span><span class="ic-badge" id="ic-cat">—</span></div>
      <div class="ic-mid"><span class="ic-big" id="ic-big">—</span><span class="ic-sub" id="ic-sub"></span></div>
      <div class="clipbar" id="clipbar"></div>
      <button class="ic-fold" id="ic-fold" onclick="toggleDetail()">收起详细数据 ⌃</button>
      <div class="ic-rows" id="ic-rows"></div>
    </div>
  </div>
  <div class="pbar">
    <button class="pp" id="ppbtn" onclick="togglePlay()">⏸</button>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span class="t" id="ptime">00:00</span>
    <button class="spd" id="spdbtn" onclick="cycleSpeed()">1x</button>
  </div>
</div>

<div class="expp" id="expp">
  <div class="expt">选择要导出的画面<button class="x" onclick="closeExport()">✕</button></div>
  <div id="expchks"></div>
  <label class="expitem" id="expwm-row" style="border-top:1px solid #2a333e;margin-top:6px;align-items:flex-start">
    <input type="checkbox" id="expwm" style="margin-top:2px">
    <span style="line-height:1.45">叠加行车信息水印<small style="display:block;color:#7d8894;font-size:11px;margin-top:2px">时间/位置/车速/挡位/油门/刹车, 烧录到画面顶部</small></span>
  </label>
  <div class="expr">
    <button class="mini" onclick="expChk(true)">全选</button>
    <button class="mini" onclick="expChk(false)">清空</button>
    <button class="mini" onclick="doExportZip()">打包zip</button>
    <button class="mini primary" onclick="doExport()">合成视频</button>
  </div>
  <div class="hint" style="margin-top:8px;margin-bottom:0">单选直接下载原文件(加水印时走本机合成); 多选合成 <b>一个多画面视频</b></div>
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

<div class="stitch" id="stitch">
  <div class="stbox">
    <div class="stt">正在合成多画面视频</div>
    <div class="stbar"><div class="stfill" id="stfill"></div></div>
    <div class="stpct" id="stpct">0%</div>
    <div class="sthint" id="sthint">设备正在拼接各路画面, 请勿关闭页面</div>
  </div>
</div>

<script>
let data = {}, cur = null, syncTimer = null, currentCat = 'SentryClips', lastSig = '', reasonFilter = 'all';
let curClips = [], curClipIx = 0, TEL = null;
const CATS_NAME = __CAT_NAMES__;
/* event.json 里的 reason → 中文名 */
const REASON_LABEL = {
  sentry_aware_object_detection: '哨兵 · 物体感知',
  sentry_trigger: '哨兵模式触发',
  user_interaction_dashcam_saved: '手动保存',
  user_interaction_dashcam_menu: '行车记录仪菜单保存',
  user_interaction_dashcam_launcher_action_tapped: '点击行车记录仪图标',
  user_interaction_honk: '按喇叭保存'
};
const EMPTY_HTML = document.getElementById('empty').innerHTML;
/* Model 3/Y (HW3) 只有 4 路: front / back / left_repeater / right_repeater
   Model S/X 与 HW4 车型才有 6 路 (含 left_pillar / right_pillar)
   页面按每个事件实际的文件数自适应, 不写死路数 */
const CAM_NAME = { front:'前视', back:'后视', left_repeater:'左B柱', right_repeater:'右B柱',
                   left_pillar:'左前翼', right_pillar:'右前翼' };
/* 排列顺序按「位置成对」：
   4 路(3/Y)      第1排 前视 后视 / 第2排 左B柱 右B柱
   6 路(S/X,HW4)  第1排 左前翼 前视 右前翼 / 第2排 左B柱 后视 右B柱
   这样左右对称, 前后同一排, 一眼能看出方位 */
const CAM_ORDER_4 = ['front','back','left_repeater','right_repeater','left_pillar','right_pillar'];
const CAM_ORDER_6 = ['left_pillar','front','right_pillar','left_repeater','back','right_repeater'];
function camLabel(c) { return CAM_NAME[c] || c || '画面'; }
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
function camSort(vs) {
  const order = vs.length > 4 ? CAM_ORDER_6 : CAM_ORDER_4;
  return vs.slice().sort((a, b) => {
    const ia = order.indexOf(a.cam), ib = order.indexOf(b.cam);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
}
/* 事件目录里通常有多组片段 (每分钟一组, 每组 4~6 路), 按组切开 */
function clipKeyOf(v) {
  const f = v.f || v.url.split('/').pop();
  return f.replace(/\.mp4$/i, '').replace(/-[^-]+$/, '');
}
function groupVideos(vids) {
  const m = new Map();
  for (const v of camSort(vids || [])) {
    const k = clipKeyOf(v);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(v);
  }
  return [...m.keys()].sort().map(k => ({ key: k, vids: m.get(k) }));
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

async function load() {
  data = await (await fetch('/api/events')).json();
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  for (const c of Object.keys(data)) {
    const b = document.createElement('button');
    b.className = 'tab' + (c === 'SentryClips' ? ' active' : '');
    b.textContent = `${CATS_NAME[c] || c} (${data[c].length})`;
    b.onclick = () => { document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); b.classList.add('active'); currentCat = c; reasonFilter = 'all'; render(c); };
    tabs.appendChild(b);
  }
  document.getElementById('stat').textContent = `归档 ${Object.values(data).reduce((a, v) => a + v.length, 0)} 个事件`;
  lastSig = sigOf(data);
  render(currentCat);
  openDeepLink();
}

/* 支持 ?ev=SentryClips/2026-11-04_12-30-00 直接打开某个事件 */
function openDeepLink() {
  const q = new URLSearchParams(location.search);
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

/* 缩略图: 服务端抽帧的静态图, 原生懒加载; 不再用 <video> 预览 (会把设备和解码器一起拖死) */
function reasonOf(ev) {
  return (ev.meta && ev.meta.reason) || 'unknown';
}
/* 按触发原因生成筛选按钮 (只在当前分类有 2 种以上原因时出现) */
function renderFilters(cat) {
  const box = document.getElementById('filters');
  const evs = data[cat] || [];
  const cnt = {};
  evs.forEach(ev => { const r = reasonOf(ev); cnt[r] = (cnt[r] || 0) + 1; });
  const keys = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
  if (reasonFilter !== 'all' && !cnt[reasonFilter]) reasonFilter = 'all';
  if (keys.length < 2) { box.className = 'filters'; box.innerHTML = ''; return; }
  box.className = 'filters show';
  const items = [['all', '全部', evs.length]].concat(keys.map(k => [k, REASON_LABEL[k] || '未标注原因', cnt[k]]));
  box.innerHTML = items.map(([k, label, n]) =>
    `<button class="chip${k === reasonFilter ? ' on' : ''}" onclick="setReason('${k}')">${label}<span class="n">${n}</span></button>`
  ).join('');
}
function setReason(k) {
  reasonFilter = k;
  renderFilters(currentCat);
  render(currentCat);
}
function render(cat) {
  const g = document.getElementById('grid'), e = document.getElementById('empty');
  g.innerHTML = '';
  renderFilters(cat);
  let evs = data[cat] || [];
  if (reasonFilter !== 'all') evs = evs.filter(ev => reasonOf(ev) === reasonFilter);
  e.style.display = evs.length ? 'none' : 'block';
  if (!evs.length) {
    e.innerHTML = reasonFilter !== 'all'
      ? '<div class="big">🔍</div><div>该筛选下暂无事件<br><small style="font-size:12px">点「全部」查看本分类所有事件</small></div>'
      : EMPTY_HTML;
  }
  for (const ev of evs) {
    const card = document.createElement('div'); card.className = 'card';
    const groups = groupVideos(ev.videos);
    const vids = camSort(groups.length ? groups[groups.length - 1].vids : []);
    let th;
    if (vids.length) {
      const fr = vids.find(v => v.cam === 'front') || vids[0];
      const src = fr.url.replace('/media/', '/thumb/');
      th = `<div class="thumb one"><img loading="lazy" decoding="async" src="${src}" alt="" onerror="this.parentNode.innerHTML='<div class=&quot;ph&quot;>🎞</div>'"><span class="cnum">${vids.length} 路</span></div>`;
    } else {
      th = `<div class="thumb"><div class="ph">🎞</div></div>`;
    }
    const mm = ev.meta || {};
    const extra = (mm.city ? ' · ' + mm.city : '') + (mm.street ? ' ' + mm.street : '');
    card.innerHTML = th + `<div class="meta"><div class="t" title="${ev.name}">${fmtName(ev.name)}</div><div class="s">${CATS_NAME[cat] || cat} · ${vids.length} 路画面${extra}</div></div>`;
    card.onclick = () => openPlayer(ev);
    g.appendChild(card);
  }
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
    vid.src = v.url; vid.muted = true; vid.defaultMuted = true; vid.preload = 'auto';
    vid.setAttribute('muted', ''); vid.setAttribute('playsinline', '');
    vid.setAttribute('webkit-playsinline', ''); vid.setAttribute('x5-playsinline', '');
    const lb = document.createElement('span'); lb.className = 'clabel'; lb.textContent = camShort(v.cam);
    cell.appendChild(vid); cell.appendChild(lb);
    cell.onclick = () => focusCell(cell);
    vg.appendChild(cell);
  });
  document.getElementById('ic-time').textContent = fmtFull(ev.name);
  document.getElementById('ic-cat').textContent = CATS_NAME[currentCat] || currentCat;
  const md0 = ev.meta || {};
  document.getElementById('ic-big').textContent =
    (md0.reason && (REASON_LABEL[md0.reason] || md0.reason)) || CATS_NAME[currentCat] || currentCat;
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
  if (md.reason) rh += '<div><span class="ick">触发原因</span>' + (REASON_LABEL[md.reason] || md.reason) + '</div>';
  if (md.camera) rh += '<div><span class="ick">触发画面</span>摄像头 #' + md.camera + '</div>';
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
  const p = document.getElementById('tpanel');
  p.classList.remove('show');
  p.classList.add('folded');   // 详细数据默认折叠
  document.getElementById('tp-fold').textContent = '展开详细数据 ⌄';
  document.getElementById('tp-loc').textContent =
    [TEL_META.city, TEL_META.street].filter(Boolean).join(' · ');
  telT0 = null;
  if (!url) return;
  const m = url.match(/(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/);
  if (m) telT0 = new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]);
  const fp = url.replace('/media/', '/archive/');
  fetch('/api/telemetry?path=' + encodeURIComponent(fp))
    .then(r => r.json())
    .then(d => {
      if (!d.ok || !d.frames || !d.frames.length) return;
      TEL = d;
      p.classList.add('show');
      renderHud(d.frames[0]);
      // 事件信息已搬到上方遥测面板, 下方卡片去掉重复项
      ['time', 'city', 'street', 'coord'].forEach(k => {
        const el = document.querySelector('#ic-rows > div[data-k="' + k + '"]');
        if (el) el.remove();
      });
      document.getElementById('ic-rows').insertAdjacentHTML('beforeend',
        '<div><span class="ick">刹车占比</span>' + d.brake_ratio + '%</div>' +
        '<div><span class="ick">遥测</span>SEI 元数据 · ' + d.count + ' 帧 @ ' + d.fps + 'fps</div>');
    }).catch(() => {});
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
  q('tp-brk-n').textContent = (f[5] ? 100 : 0) + '%';
  q('tp-brk').classList.toggle('on', !!f[5]);
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
  const fs = Math.round(15 * s);
  ctx.font = '700 ' + fs + 'px system-ui,"Segoe UI",sans-serif';
  const pw = ctx.measureText(text).width + Math.round(24 * s), ph = Math.round(27 * s);
  const x = xRight - pw;
  ctx.fillStyle = bg;
  rrPath(ctx, x, yMid - ph / 2, pw, ph, ph / 2); ctx.fill();
  ctx.fillStyle = fg;
  ctx.fillText(text, x + Math.round(12 * s), yMid + Math.round(fs * 0.36));
  return x;
}
function drawWm(ctx, t, W) {
  if (!TEL || !TEL.frames || !TEL.frames.length) return;
  const f = telFrameAt(t);
  const s = W / 1280, pad = Math.round(16 * s);
  const h = Math.round(100 * s), yc1 = Math.round(34 * s), yMid2 = h - Math.round(30 * s);
  ctx.save();
  ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
  // 面板底
  ctx.fillStyle = 'rgba(16,22,30,.92)';
  ctx.fillRect(0, 0, W, h);
  ctx.fillStyle = 'rgba(255,255,255,.08)';
  ctx.fillRect(0, h, W, Math.max(1, Math.round(1 * s)));
  // 行1: 日期时间 + 城市/位置
  ctx.font = '600 ' + Math.round(19 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#fff';
  const dateStr = telT0 ? fmtTelDate(new Date(telT0.getTime() + f[0] * 1000)) : '';
  ctx.fillText(dateStr, pad, yc1 + Math.round(7 * s));
  const dw = ctx.measureText(dateStr).width;
  const loc = [(TEL_META || {}).city, (TEL_META || {}).street].filter(Boolean).join(' · ');
  if (loc) {
    ctx.fillStyle = '#93a0ac';
    ctx.fillText(loc, pad + dw + Math.round(12 * s), yc1 + Math.round(7 * s));
  }
  // 行1 右: 转向灯三角 + 绿色挡位圆徽
  const gr = Math.round(16 * s), gx = W - pad - gr;
  const triS = Math.round(13 * s);
  ctx.fillStyle = f[6] === 1 ? '#f59e0b' : '#39434f';
  ctx.beginPath(); ctx.moveTo(gx - gr - Math.round(46*s), yc1 - triS*0.62); ctx.lineTo(gx - gr - Math.round(46*s), yc1 + triS*0.62); ctx.lineTo(gx - gr - Math.round(46*s) - triS, yc1); ctx.closePath(); ctx.fill();
  ctx.fillStyle = f[6] === 2 ? '#f59e0b' : '#39434f';
  ctx.beginPath(); ctx.moveTo(gx - gr - Math.round(22*s), yc1 - triS*0.62); ctx.lineTo(gx - gr - Math.round(22*s), yc1 + triS*0.62); ctx.lineTo(gx - gr - Math.round(22*s) + triS, yc1); ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#34c759';
  ctx.beginPath(); ctx.arc(gx, yc1, gr, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
  ctx.font = '800 ' + Math.round(19 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillText(f[1], gx, yc1 + Math.round(7 * s));
  ctx.textAlign = 'left';
  // 行2 左: 大字车速
  ctx.font = '800 ' + Math.round(40 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#fff';
  const sp = String(Math.round(f[2]));
  ctx.fillText(sp, pad, yMid2 + Math.round(12 * s));
  const sw = ctx.measureText(sp).width;
  ctx.font = '600 ' + Math.round(15 * s) + 'px system-ui,"Segoe UI",sans-serif';
  ctx.fillStyle = '#9aa4af';
  ctx.fillText('km/h', pad + sw + Math.round(8 * s), yMid2 + Math.round(13 * s));
  // 行2 右: 油门/刹车 胶囊
  let xr = W - pad;
  xr = wmPill(ctx, xr, yMid2, f[3] + '%', '#1f7ae0', '#fff', s);
  xr -= Math.round(10 * s);
  wmPill(ctx, xr, yMid2, '刹车 ' + (f[5] ? '100%' : '0%'),
    f[5] ? '#ff453a' : '#2a323d', f[5] ? '#fff' : '#8d99a5', s);
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

/* ---------- 导出 ---------- */
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
    if (v) location.href = '/dl?path=' + encodeURIComponent('/archive/' + v.url.replace(/^\/media\//, ''));
    return;
  }
  startLocalStitch(sel, document.getElementById('stitch'), wm);
}
/* 浏览器本机合成: 设备端 ffmpeg 多路 1080p 会触发内核重启, 改由电脑实时录制合成 */
let recBusy = false;
function startLocalStitch(sel, st, wm) {
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
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (cur ? cur.name : 'teslacam') + '_' + sel.length + '路.' + ext;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      hint.textContent = '合成完成, 已开始下载';
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
    if (d.state === 'error') { stitchFail(d.err || '合成失败'); return; }
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
  document.getElementById('sthint').textContent = '合成失败: ' + msg;
  document.getElementById('stpct').textContent = '';
  setTimeout(() => document.getElementById('stitch').classList.remove('open'), 5000);
}
function doExportZip() {
  if (!cur) return;
  const sel = [...document.querySelectorAll('#expchks input:checked')].map(c => c.value);
  if (!sel.length) { toast('请至少选择一路'); return; }
  closeExport();
  location.href = '/api/export/' + encodeURIComponent(currentCat) + '/' + encodeURIComponent(cur.name) + '?cams=' + encodeURIComponent(sel.join(','));
}
document.getElementById('startmask').addEventListener('click', playAll);
function tryPlay(v) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
function playAll() {
  const vs = [...document.querySelectorAll('#vgrid video')];
  // iOS/微信只允许在用户手势里逐个调 play(); 同步循环内依次触发
  vs.forEach(tryPlay);
  const m = document.getElementById('startmask');
  if (m) m.classList.remove('show');
  const pp = document.getElementById('ppbtn'); if (pp) pp.textContent = '⏸';
}
function pauseAll() {
  document.querySelectorAll('#vgrid video').forEach(v => v.pause());
  const pp = document.getElementById('ppbtn'); if (pp) pp.textContent = '▶';
}
function setSpeed(s) {
  const r = parseFloat(s);
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
    if (i && v.paused && !vs[0].paused) { tryPlay(v); return; }
    if (Math.abs(v.currentTime - t) > 0.35) { v.currentTime = t; if (!v.paused) v.play(); }
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
document.getElementById('seek').oninput = function () {
  const vs = [...document.querySelectorAll('#vgrid video')];
  if (vs.length && vs[0].duration) vs.forEach(v => v.currentTime = this.value / 1000 * vs[0].duration);
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
                total = 0
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".mp4"):
                        cam = f.rsplit("-", 1)[-1][:-4] if "-" in f else f
                        vids.append({"url": f"/media/{cat}/{name}/{f}", "cam": cam, "f": f})
                        try:
                            total += os.path.getsize(os.path.join(d, f))
                        except OSError:
                            pass
                meta = {}
                ej = os.path.join(d, "event.json")
                if os.path.isfile(ej):
                    try:
                        with open(ej, "r", encoding="utf-8", errors="ignore") as jf:
                            meta = json.load(jf)
                    except Exception:
                        meta = {}
                events.append({"name": name, "videos": vids, "size": total, "meta": meta})
        out[cat] = events
    return out


def _map_archive(p):
    """前端与深链固定用 /archive/... 作为虚拟前缀; 归档区可配置时映射到实际目录"""
    if ARCHIVE != "/archive" and (p == "/archive" or p.startswith("/archive/")):
        return ARCHIVE + p[len("/archive"):]
    return p


def safe_archive_dir(p):
    """校验目录必须落在归档区内 (防路径逃逸), 返回 realpath 或 None"""
    p = _map_archive((p or "").strip())
    if not p.startswith(ARCHIVE):
        return None
    rp = os.path.realpath(p)
    root = os.path.realpath(ARCHIVE)
    if rp != root and not rp.startswith(root + "/"):
        return None
    return rp


def safe_archive_path(p):
    """校验文件路径必须落在归档区内且真实存在"""
    p = _map_archive((p or "").strip())
    if not p.startswith(ARCHIVE):
        return None
    rp = os.path.realpath(p)
    if not rp.startswith(os.path.realpath(ARCHIVE) + "/") or not os.path.isfile(rp):
        return None
    return rp


# ---------- 车机U盘(镜像)临时管理 ----------
IMG = os.environ.get("TSF_IMG", "/root/teslacam.img")
MNT = os.environ.get("TSF_MNT", "/tmp/teslamnt")
LOOPDEV = os.environ.get("TSF_LOOP", "/dev/loop3")
LOSETUP = os.environ.get("TSF_LOSETUP") or (
    "/opt/mu300/busybox-bin/losetup"
    if os.path.exists("/opt/mu300/busybox-bin/losetup")
    else "losetup"  # 通用发行版走 PATH (util-linux)
)
GADGET_DIR = os.environ.get("TSF_GADGET", "/sys/kernel/config/usb_gadget/linux")
USB_AUTO_DETACH = 300  # 秒, 无操作自动恢复导出

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
        with open(os.path.join(GADGET_DIR, "UDC"), "w") as f:
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
        r = subprocess.run([LOSETUP, LOOPDEV, IMG], capture_output=True, text=True)
        if r.returncode != 0:
            _gadget_rebind(True)
            return False, "losetup 失败: " + r.stderr.strip()[-120:]
        r = subprocess.run(["mount", "-t", "exfat", LOOPDEV, MNT], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run([LOSETUP, "-d", LOOPDEV], capture_output=True)
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
        subprocess.run([LOSETUP, "-d", LOOPDEV], capture_output=True)
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
LIVE_LOG = "/root/tesla-live.log"
LIVE_STATE = "/root/.tesla-live-state.json"


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


# ---------------- 多路合成导出 (ffmpeg) ----------------
FFMPEG_BIN = os.environ.get("TSF_FFMPEG") or (
    "/opt/mu300/ffmpeg/ffmpeg" if os.path.exists("/opt/mu300/ffmpeg/ffmpeg") else "ffmpeg")
FFPROBE_BIN = os.environ.get("TSF_FFPROBE") or (
    "/opt/mu300/ffmpeg/ffprobe" if os.path.exists("/opt/mu300/ffmpeg/ffprobe") else "ffprobe")
STITCH_DIR = os.environ.get("TSF_CACHE", "/opt/mu300") + "/stitch-tmp"  # 不用 /tmp: tmpfs 占内存, 大视频会挤爆 RAM
STITCH_JOBS = {}
STITCH_LOCK = threading.Lock()
# 视频流并发上限: 事件多的时候浏览器会一口气请求上百个预览, 不限流会拖死设备
MEDIA_SEM = threading.BoundedSemaphore(int(os.environ.get("TSF_MEDIA_CONN", "8")))
THUMB_DIR = os.environ.get("TSF_CACHE", "/opt/mu300") + "/thumbs"
THUMB_SEM = threading.BoundedSemaphore(2)
THUMB_W = 400
CAM_ORDER6 = ["left_pillar", "front", "right_pillar", "left_repeater", "back", "right_repeater"]
CAM_ORDER4 = ["front", "back", "left_repeater", "right_repeater"]


def cam_of(f):
    return f.rsplit("-", 1)[-1][:-4] if "-" in f else f


def clip_key(f):
    """文件名里的片段组标识: '2026-11-01_12-41-49-front.mp4' -> '2026-11-01_12-41-49'
    一个事件目录里常有 10 分钟的多组片段 (每组 4-6 路), 需要按组区分"""
    base = f[:-4] if f.lower().endswith(".mp4") else f
    return base.rsplit("-", 1)[0] if "-" in base else base


def latest_group(names):
    """取时间戳最新的一组片段 (与事件时间最接近的一组)"""
    if not names:
        return []
    groups = {}
    for f in names:
        groups.setdefault(clip_key(f), []).append(f)
    return groups[max(groups)]


def order_files(names):
    """按方位顺序排列 mp4: 6 路左右分列/前后居中, 4 路前后一排、左右B柱一排 (与前端 camSort 一致)"""
    order = CAM_ORDER6 if len(names) >= 6 else CAM_ORDER4
    return sorted(names, key=lambda f: (order.index(cam_of(f)) if cam_of(f) in order else 99, f))


def probe_duration(path):
    try:
        out = subprocess.run([FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, timeout=20).stdout.decode().strip()
        return float(out)
    except Exception:
        return 0.0


def mem_available_mb():
    """读取 MemAvailable, 用于合成前的内存兜底检查"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def _stitch_worker(job_id, paths, out_path, total):
    n = len(paths)
    cols = 1 if n == 1 else (2 if n <= 4 else 3)
    # 每格分辨率: 默认 640x360 (4 路整幅 1280x720)。设备只有 1.4G 内存,
    # 输出 1080p 时 4 路 1080p 解码 + x264 帧缓存会把内存吃光导致整机失联。
    # 需要更高画质时用环境变量 TSF_CELL=800x450 之类覆盖。
    cell = os.environ.get("TSF_CELL", "").lower()
    if "x" in cell:
        try:
            W, H = [int(x) for x in cell.split("x", 1)]
        except ValueError:
            W, H = 640, 360
    elif n == 2:
        W, H = 960, 540      # 两路时整幅 1920x540, 负载可控
    else:
        W, H = 640, 360
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
           "-filter_threads", "1"]
    for f in paths:
        cmd += ["-i", f]
    fc = []
    for i in range(n):
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=15[v{i}]")
    layout = "|".join("%d_%d" % ((i % cols) * W, (i // cols) * H) for i in range(n))
    fc.append("".join(f"[v{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[vout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-threads", "2", "-x264-params", "ref=2:bframes=0",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # 万一仍然吃爆内存, 让内核优先杀 ffmpeg, 保住 init/viewer 不被 OOM 带走
        try:
            with open("/proc/%d/oom_score_adj" % proc.pid, "w") as f:
                f.write("800")
        except OSError:
            pass
        deadline = time.time() + max(600, total * 30)  # 超时保护, 防止任务永久卡住
        timed_out = False
        for raw in proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("out_time_us=") and total > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1000000.0
                    pct = max(0, min(99, int(done / total * 100)))
                    with STITCH_LOCK:
                        if job_id in STITCH_JOBS:
                            STITCH_JOBS[job_id]["pct"] = pct
                except ValueError:
                    pass
            if time.time() > deadline:
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
        err = proc.stderr.read().decode("utf-8", "ignore")
        code = proc.wait()
        with STITCH_LOCK:
            job = STITCH_JOBS.get(job_id)
            if not job:
                return
            if timed_out:
                job["state"] = "error"
                job["err"] = "合成超时(超过 %d 分钟), 请少选几路再试" % int(max(600, total * 30) // 60)
                return
            if code == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                job["state"] = "done"
                job["pct"] = 100
                job["path"] = out_path
                job["size"] = os.path.getsize(out_path)
            else:
                job["state"] = "error"
                tail = [l for l in (err or "").strip().splitlines() if l.strip()]
                job["err"] = (tail[-1] if tail else "ffmpeg 退出码 %d" % code)[:300]
    except Exception as e:
        with STITCH_LOCK:
            job = STITCH_JOBS.get(job_id)
            if job:
                job["state"] = "error"
                job["err"] = str(e)[:300]


def start_stitch(cat, name, cams):
    """创建合成任务, 返回 (job_id, 错误信息)"""
    # 本机 (F50) 整个系统跑在 loop 镜像上, 实测 ffmpeg 多路 1080p 解码+编码会触发
    # 内核 soft/hard lockup -> panic=5 自动重启, 已用 2/4 路真实素材复现两次。
    # 前端默认走浏览器本机合成, 这里仅保留 TSF_STITCH=1 的实验开关。
    if os.environ.get("TSF_STITCH", "0") != "1":
        return None, "设备端合成已停用(该负载会触发设备内核重启), 请使用浏览器合成或打包下载"
    if not os.path.isfile(FFMPEG_BIN):
        return None, "设备上未安装 ffmpeg (预期 %s)" % FFMPEG_BIN
    evdir = safe_archive_dir(os.path.join(ARCHIVE, cat, name))
    if not evdir or not os.path.isdir(evdir):
        return None, "事件不存在"
    files = [f for f in os.listdir(evdir) if f.lower().endswith(".mp4")]
    files = latest_group(files)  # 只取最新一组片段, 24 个片段全拼会直接压垮设备
    if cams:
        files = [f for f in files if cam_of(f) in cams]
    files = order_files(files)
    if len(files) < 2:
        return None, "至少需要选择两路画面"
    paths = [os.path.join(evdir, f) for f in files]
    os.makedirs(STITCH_DIR, exist_ok=True)
    # 内存与并发保护: 设备只有 1.4G, 同时跑两个合成会直接 OOM 拖垮整机
    with STITCH_LOCK:
        running = [j for j in STITCH_JOBS.values() if j.get("state") == "running"]
    if running:
        return None, "已有合成任务在进行, 请等待完成后再试"
    avail = mem_available_mb()
    if 0 < avail < 300:
        return None, "设备可用内存不足(%dMB), 暂停合成以免影响录像同步" % avail
    now = time.time()
    for old_f in os.listdir(STITCH_DIR):
        fp_old = os.path.join(STITCH_DIR, old_f)
        try:
            if os.path.isfile(fp_old) and now - os.path.getmtime(fp_old) > 7200:
                os.unlink(fp_old)
        except OSError:
            pass
    job_id = "%s-%d" % (name.replace(":", "-"), int(time.time() * 1000) % 1000000)
    out_path = os.path.join(STITCH_DIR, job_id + ".mp4")
    total = probe_duration(paths[0])
    with STITCH_LOCK:
        STITCH_JOBS[job_id] = {"state": "running", "pct": 0, "path": None,
                               "name": "%s_%d路.mp4" % (name, len(files)), "total": total}
    threading.Thread(target=_stitch_worker, args=(job_id, paths, out_path, total), daemon=True).start()
    return job_id, None


# ---------- 行车遥测: 解码视频 SEI 里的 SeiMetadata protobuf ----------
# Tesla 在车辆行驶中录制的片段里, 每帧塞一条 SEI(type 6) NAL,
# payload 以 magic 0x42 0x42 0x69 开头, 后跟 protobuf:
#   1 version, 2 gear(0P/1D/2R/3N), 3 frame_seq, 4 speed_mps, 5 油门%,
#   6 方向盘角度, 7/8 左右转向灯, 9 刹车, 10 AP状态, 11/12 经纬度,
#   13 航向, 14/15/16 三轴加速度
TEL_DIR = os.environ.get("TSF_CACHE", "/opt/mu300") + "/tel-cache"
TEL_SEM = threading.BoundedSemaphore(int(os.environ.get("TSF_TEL_CONN", "1")))


def _pb_varint(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def _pb_decode(b):
    out = {}
    i = 0
    n = len(b)
    while i < n:
        try:
            key, i = _pb_varint(b, i)
        except (IndexError, ValueError):
            break
        fn, wt = key >> 3, key & 7
        try:
            if wt == 0:
                out[fn], i = _pb_varint(b, i)
            elif wt == 5:
                out[fn] = struct.unpack("<f", b[i:i + 4])[0]
                i += 4
            elif wt == 1:
                out[fn] = struct.unpack("<d", b[i:i + 8])[0]
                i += 8
            elif wt == 2:
                ln, j = _pb_varint(b, i)
                i = j + ln
            else:
                return None
        except (IndexError, struct.error, ValueError):
            return None
    return out if i == n else None


def _strip_emulation(b):
    out = bytearray()
    zeros = 0
    for x in b:
        if zeros >= 2 and x == 3:
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def _mp4_video_samples(fp, max_samples=4000):
    """解析 moov, 返回 [(offset, size), ...] 与 lenSize; 失败返回 None"""
    with open(fp, "rb") as f:
        d = f.read(4096)
        f.seek(0, 2)
        fsize = f.tell()

    def boxes(buf, start, end):
        out = []
        i = start
        while i + 8 <= end:
            if i + 8 > len(buf) and len(buf) < fsize:
                return None  # 需要 moov 全量, 由调用方重新读
            size = struct.unpack(">I", buf[i:i + 4])[0]
            typ = buf[i + 4:i + 8]
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - i
            if size < hdr:
                break
            out.append((typ, i + hdr, i + size))
            i += size
        return out

    # moov 可能在文件尾, 先定位
    with open(fp, "rb") as f:
        pos = 0
        moov_rng = None
        while pos + 8 <= fsize:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8]
            hsz = 8
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                hsz = 16
            elif size == 0:
                size = fsize - pos
            if size < hsz:
                break
            if typ == b"moov":
                moov_rng = (pos + hsz, pos + size)
                break
            pos += size
    if not moov_rng:
        return None
    with open(fp, "rb") as f:
        f.seek(moov_rng[0])
        moov = f.read(moov_rng[1] - moov_rng[0])
    mv_s, mv_e = 0, len(moov)

    def find1(name, s, e):
        for typ, cs, ce in boxes(moov, s, e):
            if typ == name:
                return cs, ce
        return None

    stbl = None
    for typ, ts, te in boxes(moov, mv_s, mv_e):
        if typ != b"trak":
            continue
        r = find1(b"mdia", ts, te)
        if not r:
            continue
        r2 = find1(b"minf", r[0], r[1])
        if not r2:
            continue
        r3 = find1(b"stbl", r2[0], r2[1])
        if not r3:
            continue
        stsd = find1(b"stsd", r3[0], r3[1])
        if stsd and moov.find(b"avcC", stsd[0], stsd[1]) > 0:
            stbl = r3
            break
    if not stbl:
        return None
    s, e = stbl
    stsd = find1(b"stsd", s, e)
    avc = moov.find(b"avcC", stsd[0], stsd[1])
    len_size = (moov[avc + 8] & 3) + 1
    a = find1(b"stsz", s, e)[0]
    uniform = struct.unpack(">I", moov[a + 4:a + 8])[0]
    n = struct.unpack(">I", moov[a + 8:a + 12])[0]
    if uniform:
        sizes = [uniform] * n
        buf = b""
    else:
        take = min(n, max_samples * 2)
        sizes = list(struct.unpack(">%dI" % take, moov[a + 12:a + 12 + 4 * take]))
    ca = find1(b"stco", s, e)[0]
    nch = struct.unpack(">I", moov[ca + 4:ca + 8])[0]
    chunks = struct.unpack(">%dI" % nch, moov[ca + 8:ca + 8 + 4 * nch])
    sa = find1(b"stsc", s, e)[0]
    ns = struct.unpack(">I", moov[sa + 4:sa + 8])[0]
    stsc = struct.unpack(">%dIII" % ns, moov[sa + 8:sa + 8 + 12 * ns])
    offs = []
    si = 0
    for ci in range(nch):
        run = None
        for k in range(ns):
            if stsc[k * 3] - 1 <= ci:
                run = stsc[k * 3:k * 3 + 3]
        per = run[1] if run else 1
        o = chunks[ci]
        for _ in range(per):
            if si >= len(sizes) or len(offs) >= max_samples:
                break
            offs.append((o, sizes[si]))
            o += sizes[si]
            si += 1
    return offs, len_size


def parse_telemetry(fp, stride=3):
    """从 mp4 SEI 提取遥测, 返回 dict 或 None(无SEI)"""
    r = _mp4_video_samples(fp)
    if not r:
        return None
    offs, len_size = r
    dur = probe_duration(fp)
    if not dur or len(offs) < 2:
        return None
    fps = len(offs) / float(dur)
    fsize = os.path.getsize(fp)
    frames = []
    gear_map = {0: "P", 1: "D", 2: "R", 3: "N"}
    with open(fp, "rb") as f, TEL_SEM:
        for k in range(0, len(offs), stride):
            o, ln = offs[k]
            if o + ln > fsize:
                continue
            f.seek(o)
            data = f.read(ln)
            pos = 0
            while pos + len_size <= len(data):
                n2 = int.from_bytes(data[pos:pos + len_size], "big")
                if n2 <= 0 or pos + len_size + n2 > len(data):
                    break
                nal = data[pos + len_size:pos + len_size + n2]
                pos += len_size + n2
                if nal[0] & 0x1F != 6:
                    continue
                # 找 magic: 连续的 0x42 之后遇到 0x69
                pi = None
                for i in range(3, min(len(nal), 100)):
                    if nal[i] == 0x42:
                        continue
                    if nal[i] == 0x69:
                        pi = i + 1
                    break
                if pi is None:
                    continue
                payload = _strip_emulation(nal[pi:len(nal) - 1])
                msg = _pb_decode(payload)
                if not msg:
                    continue
                spd = msg.get(4, 0.0) * 3.6
                frames.append([
                    round(k / fps, 2),
                    gear_map.get(msg.get(2, 1), "D"),
                    round(spd, 1),
                    int(round(msg.get(5, 0.0))),
                    round(msg.get(6, 0.0), 1),
                    1 if msg.get(9) else 0,
                    1 if msg.get(7) else (2 if msg.get(8) else 0),
                    int(msg.get(10, 0)),
                    round(msg.get(11, 0.0), 6),
                    round(msg.get(12, 0.0), 6),
                    round(msg.get(13, 0.0), 1),
                    round(msg.get(14, 0.0), 2),
                    round(msg.get(15, 0.0), 2),
                    round(msg.get(16, 0.0), 2),
                ])
    if not frames:
        return None
    speeds = [f[2] for f in frames]
    brakes = sum(1 for f in frames if f[5])
    return {
        "fps": round(fps, 2),
        "count": len(offs),
        "duration": round(dur, 2),
        "max_speed": max(speeds),
        "avg_speed": round(sum(speeds) / len(speeds), 1),
        "brake_ratio": round(brakes * 100.0 / len(frames), 1),
        "frames": frames,
    }


def telemetry_cached(fp):
    os.makedirs(TEL_DIR, exist_ok=True)
    try:
        st = os.stat(fp)
    except OSError:
        return None
    key = hashlib.md5(("%s|%d|%d" % (fp, st.st_mtime_ns, st.st_size)).encode()).hexdigest()
    cache = os.path.join(TEL_DIR, key + ".json")
    if os.path.isfile(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        result = parse_telemetry(fp)
    except Exception:
        result = None
    if result:
        try:
            tmp = cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, separators=(",", ":"))
            os.replace(tmp, cache)
        except OSError:
            pass
    return result


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
        elif path == "/api/stitch/status":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            job = STITCH_JOBS.get(q.get("id", [""])[0])
            if not job:
                self._json({"state": "error", "err": "任务不存在或已过期"}, 404)
                return
            self._json({"state": job["state"], "pct": job.get("pct", 0),
                        "err": job.get("err", ""), "size": job.get("size", 0)})
        elif path == "/api/stitch/dl":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            jid = q.get("id", [""])[0]
            job = STITCH_JOBS.get(jid)
            if not job or job.get("state") != "done" or not os.path.isfile(job.get("path") or ""):
                self._send(404, b"not ready")
                return
            fp = job["path"]
            fname = job.get("name") or "stitch.mp4"
            size = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % urllib.parse.quote(fname))
            self.end_headers()
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            with STITCH_LOCK:
                STITCH_JOBS.pop(jid, None)
            try:
                os.unlink(fp)
            except OSError:
                pass
        elif path == "/api/events":
            self._json(list_events())
        elif path == "/api/telemetry":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            p = q.get("path", [""])[0]
            fp = safe_archive_dir(p) if p else None
            if not fp or not os.path.isfile(fp):
                self._json({"ok": False, "reason": "bad-path"}, 404)
                return
            tel = telemetry_cached(fp)
            if not tel:
                self._json({"ok": False, "reason": "no-sei"})
                return
            self._json({"ok": True, **tel})
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
        elif path.startswith("/api/export/"):
            m2 = re.match(r"^/api/export/([\w-]+)/([\w\-. ]+)/?$", path)
            if not m2:
                self._send(404, b"not found")
                return
            q2 = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cams = [c.strip() for c in (q2.get("cams", [""])[0] or "").split(",") if c.strip()]
            evdir = safe_archive_dir(ARCHIVE + "/" + m2.group(1) + "/" + m2.group(2))
            if not evdir or not os.path.isdir(evdir):
                self._send(404, b"not found")
                return
            vids = []
            allf = latest_group(sorted(f for f in os.listdir(evdir) if f.endswith(".mp4")))
            for f in allf:
                cam = f.rsplit("-", 1)[-1][:-4] if "-" in f else f
                if not cams or cam in cams:
                    vids.append(f)
            if not vids:
                self._send(404, b"no matching videos")
                return
            evname = m2.group(2)
            zname = evname + (("_" + "-".join(cams)) if cams and len(cams) < 6 else "") + ".zip"
            tmp = tempfile.NamedTemporaryFile(prefix="exp_", suffix=".zip", delete=False)
            tmp.close()
            try:
                with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
                    for f in vids:
                        z.write(os.path.join(evdir, f), arcname=evname + "/" + f)
                    ej = os.path.join(evdir, "event.json")
                    if os.path.isfile(ej):
                        z.write(ej, arcname=evname + "/event.json")
                zsize = os.path.getsize(tmp.name)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(zsize))
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % urllib.parse.quote(zname))
                self.end_headers()
                with open(tmp.name, "rb") as zf:
                    while True:
                        chunk = zf.read(262144)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        elif path.startswith("/thumb/"):
            self._serve_thumb(path)
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
            # 并发限流: 浏览器一次会为几十个缩略图发请求, 不限流会把设备拖死
            if not MEDIA_SEM.acquire(timeout=20):
                self._send(503, b"busy")
                return
            try:
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
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                MEDIA_SEM.release()

    def _serve_thumb(self, path):
        """列表缩略图: 用 ffmpeg 抽第 1 秒一帧, 结果按 (路径+大小+mtime) 哈希缓存在 eMMC"""
        parts = path[len("/thumb/"):].split("/")
        if len(parts) != 3:
            self._send(404, b"not found")
            return
        cat, name, fn = [urllib.parse.unquote(p) for p in parts]
        evdir = safe_archive_dir(os.path.join(ARCHIVE, cat, name))
        fn = safe_name(fn)
        if not evdir or not fn:
            self._send(404, b"not found")
            return
        fp = os.path.join(evdir, fn)
        if not os.path.isfile(fp):
            self._send(404, b"not found")
            return
        try:
            st = os.stat(fp)
        except OSError:
            self._send(404, b"not found")
            return
        key = hashlib.md5(("%s|%d|%d" % (fp, st.st_size, int(st.st_mtime))).encode()).hexdigest()
        out = os.path.join(THUMB_DIR, key + ".jpg")
        # 0 字节文件是上次 ffmpeg 失败留下的残骸 (片段当时还没同步完), 视为无效缓存
        try:
            if os.path.getsize(out) == 0:
                os.remove(out)
        except OSError:
            pass
        if not os.path.isfile(out):
            os.makedirs(THUMB_DIR, exist_ok=True)
            tmp = os.path.join(THUMB_DIR, "%s.%d.tmp.jpg" % (key, threading.get_ident()))
            if THUMB_SEM.acquire(timeout=12):
                try:
                    if not os.path.isfile(out):
                        # -ss 1 可能落在片段末尾之外(极短片段), 失败则退回第一帧
                        for ss in ("1", "0"):
                            try:
                                if os.path.exists(tmp):
                                    os.remove(tmp)
                                subprocess.run([FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                                                "-ss", ss, "-i", fp, "-frames:v", "1",
                                                "-vf", "scale=%d:-2" % THUMB_W, "-q:v", "7", tmp],
                                               capture_output=True, timeout=30)
                                if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                                    os.replace(tmp, out)
                                    break
                            except Exception:
                                pass
                finally:
                    THUMB_SEM.release()
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                self._send(404, b"no thumb")
                return
        try:
            with open(out, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.unquote(self.path)
        if path == "/api/stitch":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                self._json({"ok": False, "error": "参数错误"}, 400)
                return
            jid, err = start_stitch(body.get("cat") or "", body.get("name") or "", body.get("cams") or [])
            if not jid:
                self._json({"ok": False, "error": err}, 400)
                return
            self._json({"ok": True, "id": jid})
            return
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
    # 降低自身被 OOM 杀掉的概率 (合成任务会自己调高优先级先死)
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("-300")
    except OSError:
        pass
    # 启动自愈: 若上次退出时镜像仍处于管理挂载状态, 恢复导出
    if os.path.ismount(MNT):
        subprocess.run(["umount", MNT], capture_output=True)
    subprocess.run([LOSETUP, "-d", LOOPDEV], capture_output=True)
    _gadget_rebind(True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sentry viewer on 0.0.0.0:{PORT}")
    srv.serve_forever()
