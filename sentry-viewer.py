#!/usr/bin/env python3
"""Tesla 哨兵视频查看器 - 单文件, 纯标准库, 无依赖
功能: 多路播放器(4路/6路自适应) / 时间线 / 皮肤上传 / 锁车语音上传(经归档同步写入车机U盘)
访问: http://<f50-ip>:8080
"""
import base64
import datetime
import json
import os
import re
import hmac
import hashlib
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import tempfile
import sys
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

# ---------------- 版本与在线升级 (OTA) ----------------
# 升级方式: 从 GitHub 仓库拉 version.json (清单) 比对版本号, 再下 sentry-viewer.py 覆盖本机文件并重启。
# 国内直连 raw.githubusercontent 多数能通, 同时内置镜像源兜底, 依次尝试。
APP_VERSION = "2.1.1"
OTA_REPO = os.environ.get("TSF_OTA_REPO", "baicyber/tesla-sentry-f50")
_OTA_DEFAULT = ",".join([
    "https://raw.githubusercontent.com/%s/main" % OTA_REPO,
    "https://ghfast.top/https://raw.githubusercontent.com/%s/main" % OTA_REPO,
    "https://cdn.jsdelivr.net/gh/%s@main" % OTA_REPO,
])
OTA_SOURCES = [s.strip().rstrip("/") for s in os.environ.get("TSF_OTA_SOURCES", _OTA_DEFAULT).split(",") if s.strip()]
OTA_BACKUP = os.environ.get("TSF_OTA_BACKUP", "/opt/tesla-sentry/sentry-viewer.bak.py")
OTA_STATE = os.environ.get("TSF_OTA_STATE", "/opt/tesla-sentry/ota-state.json")
OTA_SRC_PREF = os.path.join(os.path.dirname(OTA_STATE) or ".", "ota-src.json")  # 记住上次最快的源
_OTA_CACHE = {"t": 0.0, "data": None}


def _http_get(url, timeout=10, limit=8 << 20):
    req = urllib.request.Request(url, headers={"User-Agent": "tsf-ota/" + APP_VERSION, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _src_order():
    """上次成功的源排最前 (竞速同时成功时优先选它)"""
    order = list(OTA_SOURCES)
    try:
        with open(OTA_SRC_PREF, encoding="utf-8") as f:
            good = json.load(f).get("last_good")
        if good in order:
            order.remove(good)
            order.insert(0, good)
    except Exception:
        pass
    return order


def _race_get(path, timeout, limit, validate=None):
    """并发竞速所有下载源, 谁先返回【通过校验】的数据就用谁 (DashPlay 式自动切换源)。
    单个源被墙/超时不影响其它源; 返回 (data, base)。"""
    lock = threading.Lock()
    got = []

    def worker(base):
        try:
            data = _http_get(base + path, timeout=timeout, limit=limit)
            if data and (validate is None or validate(data)):
                with lock:
                    got.append((base, data))
        except Exception:
            pass

    order = _src_order()
    for base in order:
        threading.Thread(target=worker, args=(base,), daemon=True).start()
    deadline = time.time() + timeout + 1.5
    while time.time() < deadline and not got:
        time.sleep(0.05)
    if not got:
        raise RuntimeError("所有下载源均不可用 (%s)" % ", ".join(
            urllib.parse.urlsplit(b).netloc for b in order))
    # 多个源同时成功时, 按源优先级取第一个
    got.sort(key=lambda x: order.index(x[0]))
    base, data = got[0]
    try:   # 记住本次最快的源
        with open(OTA_SRC_PREF, "w", encoding="utf-8") as f:
            json.dump({"last_good": base, "at": int(time.time())}, f)
    except OSError:
        pass
    return data, base


def _vtuple(v):
    out = []
    for x in re.split(r"[.\-+]", str(v)):
        out.append(int(x) if x.isdigit() else 0)
    return tuple(out) or (0,)


def ota_latest(force=False):
    """读仓库 version.json (含版本号/日期/更新说明), 并发竞速多个源, 结果缓存 10 分钟"""
    now = time.time()
    if not force and _OTA_CACHE["data"] and now - _OTA_CACHE["t"] < 600:
        return _OTA_CACHE["data"]

    def ok(raw):
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            return bool(str(j.get("version") or "").strip())
        except Exception:
            return False

    raw, base = _race_get("/version.json?ts=%d" % int(now), timeout=6, limit=256 << 10, validate=ok)
    j = json.loads(raw.decode("utf-8", "replace"))
    j["_base"] = base
    _OTA_CACHE.update({"t": now, "data": j})
    return j


def ota_check(force=False):
    cur = APP_VERSION
    out = {"ok": True, "current": cur, "latest": "", "date": "", "notes": [],
           "has_update": False, "source": "", "error": "",
           "rollback_ready": os.path.isfile(OTA_BACKUP)}
    try:
        j = ota_latest(force)
    except Exception as ex:
        out["error"] = str(ex)
        return out
    latest = str(j.get("version"))
    out.update({"latest": latest, "date": j.get("date") or "", "notes": j.get("notes") or [],
                "has_update": _vtuple(latest) > _vtuple(cur),
                "source": urllib.parse.urlsplit(j["_base"]).netloc})
    return out


def _self_paths():
    """需要一起覆盖的程序文件 (只认正式安装位置; 临时副本只改它自己)"""
    here = os.path.realpath(__file__)
    known = ("/opt/tesla-sentry/sentry-viewer.py", "/root/sentry-viewer.py")
    paths = [here]
    if here not in known:
        return paths
    for p in known:
        try:
            rp = os.path.realpath(p)
        except OSError:
            continue
        if os.path.isfile(rp) and rp not in paths:
            paths.append(rp)
    return paths


def _write_each(paths, data):
    for p in paths:
        d = os.path.dirname(p) or "."
        tmp = os.path.join(d, ".ota-tmp.py")
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, p)          # 原子替换, 不会出现半个文件


def _ota_restart():
    """优先交给 procd 的 init 脚本重启 (找不到就原地 exec)"""
    here = os.path.realpath(__file__)
    d = os.path.dirname(here)
    cands = []
    for name in ("tesla-viewer", "sentry-viewer"):
        p = "/etc/init.d/" + name
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            txt = ""
        score = 2 if here in txt else (1 if d and d in txt else 0)
        cands.append((score, p))
    cands.sort(reverse=True)
    if cands and cands[0][0] > 0:
        try:
            subprocess.Popen([cands[0][1], "restart"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except Exception:
            pass
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        os._exit(1)


def ota_apply():
    """下载并安装最新版; 返回 (成功?, 提示/错误)"""
    try:
        j = ota_latest(True)
    except Exception as ex:
        return False, "检查更新失败: %s" % ex
    latest = str(j.get("version"))
    if not _vtuple(latest) > _vtuple(APP_VERSION):
        return False, "当前已是最新版本 (%s)" % APP_VERSION
    # 下载也走多源竞速: 清单来源的镜像突然挂了, 其它源照样能补位
    try:
        data, base = _race_get("/sentry-viewer.py?ts=%d" % int(time.time()), timeout=60, limit=8 << 20,
                               validate=lambda d: len(d) > 50000 and b"APP_VERSION" in d)
    except Exception as ex:
        return False, "下载失败: %s" % ex
    # 1) 完整性校验: 大小 + 特征串 + 内嵌版本号必须与清单一致
    if len(data) < 50000:
        return False, "下载不完整 (%d 字节)" % len(data)
    if b"APP_VERSION" not in data or b"--ota-check" not in data or b"/api/events" not in data:
        return False, "文件校验失败: 不是有效的查看器程序"
    m = re.search(rb'APP_VERSION\s*=\s*"([^"]+)"', data)
    if not m or m.group(1).decode() != latest:
        return False, "版本校验失败 (文件 %s / 清单 %s)" % (m.group(1).decode() if m else "?", latest)
    # 2) 语法 + 启动自检: 新程序自己声明的 --ota-check 只做模块加载, 不启服务
    tmp = "/tmp/tsf-ota-new.py"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        r = subprocess.run([sys.executable or "python3", "-m", "py_compile", tmp],
                           capture_output=True, timeout=90)
        if r.returncode != 0:
            return False, "语法校验失败: " + r.stderr.decode("utf-8", "replace")[-160:]
        r = subprocess.run([sys.executable or "python3", tmp, "--ota-check"],
                           capture_output=True, timeout=90)
        if r.returncode != 0 or b"ok" not in (r.stdout or b"").lower():
            return False, "启动自检失败: " + (r.stderr or r.stdout).decode("utf-8", "replace")[-160:]
        os.remove(tmp)
    except Exception as ex:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "校验异常: %s" % ex
    # 3) 备份旧版 (回滚用) 后逐份原子替换
    here = os.path.realpath(__file__)
    try:
        os.makedirs(os.path.dirname(OTA_BACKUP) or ".", exist_ok=True)
        shutil.copy2(here, OTA_BACKUP)
    except Exception as ex:
        return False, "备份旧版本失败: %s" % ex
    prev = APP_VERSION
    try:
        _write_each(_self_paths(), data)
    except Exception as ex:
        return False, "写入失败: %s" % ex
    try:
        with open(OTA_STATE, "w", encoding="utf-8") as f:
            json.dump({"applied": latest, "prev": prev, "at": int(time.time())}, f)
    except OSError:
        pass
    threading.Timer(1.2, _ota_restart).start()
    return True, "已升级到 %s, 服务正在重启…" % latest


def ota_rollback():
    if not os.path.isfile(OTA_BACKUP):
        return False, "没有可用的备份 (还没升级过)"
    try:
        with open(OTA_BACKUP, "rb") as f:
            data = f.read()
    except OSError as ex:
        return False, "读取备份失败: %s" % ex
    if len(data) < 50000:
        return False, "备份文件不完整, 已放弃回滚"
    try:
        _write_each(_self_paths(), data)
    except Exception as ex:
        return False, "写入失败: %s" % ex
    threading.Timer(1.0, _ota_restart).start()
    return True, "已回滚到上一版本, 服务正在重启…"

HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>特斯拉哨兵查看器</title>
<style id="base">
:root {
  color-scheme: dark;
  --bg:#0e1216; --headbg:#171d24; --panel:#1c232c; --panel2:#232b35; --chipbg:#1e252e;
  --line:#232b34; --line2:#2a333d; --text:#e8eaed; --muted:#8d99a5; --muted2:#6f7a85;
  --accent:#e8443f; --blue:#2f6ee0; --shadow:0 4px 14px #00000055; --headline:#ffffff0d;
  /* 播放器子页面专用 */
  --stage:#0d1117;                        /* 视频区域始终深色, 两种主题都不变 */
  --amberbg:rgba(245,158,11,.14); --amber:#eab063; --amberb:#f7c67e; --amberline:rgba(245,158,11,.22);
}
html[data-theme="light"] {
  color-scheme: light;
  --bg:#f2f4f7; --headbg:#ffffff; --panel:#ffffff; --panel2:#eef1f5; --chipbg:#ffffff;
  --line:#e4e8ee; --line2:#d9dfe6; --text:#17191c; --muted:#6b7683; --muted2:#9aa4af;
  --accent:#e8443f; --blue:#2f6ee0; --shadow:0 2px 10px #0f172a14; --headline:#0f172a0d;
  --amberbg:rgba(245,158,11,.16); --amber:#9a5b00; --amberb:#875000; --amberline:rgba(245,158,11,.34);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  transition:background .25s,color .25s; }
header { padding:12px 16px; background:var(--headbg); display:flex; flex-wrap:wrap; align-items:center; gap:10px; position:sticky; top:0; z-index:10; box-shadow:0 1px 0 var(--headline); }
header h1 { font-size:16px; font-weight:700; white-space:nowrap; letter-spacing:.5px; }
header .sub { font-size:12px; color:var(--muted); white-space:nowrap; }
#live { margin-left:auto; }
.tabs { display:flex; gap:6px; overflow-x:auto; scrollbar-width:none; }
.tabs::-webkit-scrollbar { display:none; }
.tab { padding:7px 14px; border-radius:18px; background:var(--panel2); color:var(--muted); cursor:pointer; font-size:12.5px; font-weight:600; border:1px solid transparent; white-space:nowrap; flex:0 0 auto; transition:background .15s,color .15s; }
.tab:hover { color:var(--text); }
.tab.active { background:var(--text); color:var(--bg); box-shadow:none; }
html[data-theme="light"] .tab.active { background:#17191c; color:#fff; }
.filters { display:none; gap:6px; overflow-x:auto; padding:10px 16px 0; scrollbar-width:none; align-items:center; }
.filters.show { display:flex; }
.filters + .filters { padding-top:8px; }
.filters::-webkit-scrollbar { display:none; }
.flab { font-size:11.5px; color:var(--muted2); flex:0 0 auto; letter-spacing:.5px; }
.chip.d.on { background:#b4761c; border-color:#b4761c; }
.chip.pick { border-style:dashed; background:transparent; color:var(--muted); }
.chip.pick:hover { color:var(--text); }
/* 原生日期选择器本体: 藏起来只用来唤起系统日历 */
.dpick { position:absolute; left:0; top:0; width:1px; height:1px; opacity:0; border:0; padding:0;
  pointer-events:none; }
.chip { padding:5px 12px; border-radius:14px; background:var(--chipbg); color:var(--muted); border:1px solid var(--line2);
  font-size:12px; cursor:pointer; white-space:nowrap; flex:0 0 auto; transition:background .15s,color .15s; }
.chip:hover { color:var(--text); border-color:var(--muted2); }
.chip.on { background:var(--blue); border-color:var(--blue); color:#fff; }
.chip .n { opacity:.62; margin-left:4px; font-variant-numeric:tabular-nums; }
/* 车载U盘存储条 (参照官方App车辆卡片) */
.storwrap { padding:10px 16px 0; }
.storcard { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 14px; box-shadow:var(--shadow); }
.storrow { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px; }
.storlab { font-size:13px; color:var(--text); font-weight:600; }
.storval { font-size:12.5px; color:var(--muted); font-variant-numeric:tabular-nums; }
.storbar { height:4px; border-radius:2px; background:var(--line2); overflow:hidden; }
.storbar i { display:block; height:100%; width:0; background:var(--blue); border-radius:2px; transition:width .4s; }
/* 主题切换按钮: 自动/浅色/深色 循环 */
#theme { background:var(--panel2); border:none; color:var(--muted); font-size:15px; cursor:pointer; border-radius:10px; width:36px; height:32px; }
#theme:hover { color:var(--text); }
@media (max-width:640px){
  header .sub { display:none; } .tabs { order:10; width:100%; margin-left:0; } #theme { order:4; margin-left:0; } #gear { order:5; margin-left:0; } #out { order:6; margin-left:0; }
  .filters { padding-left:12px; padding-right:12px; }
  /* 播放器手机端紧凑化: 面板/卡片占高更小, 视频尽量多露 */
  .pscroll { padding:2px 8px 10px; gap:8px; }
  .tpanel { padding:10px 12px 8px; }
  .tp-spd { font-size:26px; }
  .tp-date { font-size:12px; } .tp-loc { font-size:11.5px; }
  .tp-tag { height:20px; font-size:11px; padding:0 8px; }
  .tp-gear { width:22px; height:22px; font-size:12px; }
  .tp-c { width:28px; height:28px; } .tp-c.st svg { width:16px; height:16px; }
  .tp-badge { height:25px; font-size:11px; padding:0 8px 0 6px; } .tp-badge svg { width:13px; height:13px; }
  .infocard { padding:10px 12px 8px; }
  .ptop { padding:6px 10px; }
  /* 底部控制栏: 手机宽度紧张, 全面紧凑化 */
  .pbar { gap:8px; padding-left:10px; padding-right:10px; }
  .pp { width:38px; height:38px; font-size:14px; }
  .pbar .t { min-width:38px; font-size:12.5px; }
  .spd { padding:8px 11px; font-size:13px; }
  .qbtn { padding:8px 10px; font-size:12.5px; }
}
#gear { background:var(--panel2); border:none; color:var(--muted); font-size:18px; cursor:pointer; border-radius:10px; width:36px; height:32px; }
#gear:hover { color:var(--text); }
#out { background:var(--panel2); border:none; color:var(--muted); font-size:15px; cursor:pointer; border-radius:10px; width:36px; height:32px; }
#out:hover { color:var(--text); }
.grid-wrap { padding:14px 16px 20px; display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:10px; max-width:1120px; margin:0 auto; }
/* 事件卡片: 官方App横排列表 (封面左, 信息右) */
.card { display:flex; gap:12px; align-items:center; background:var(--panel); border-radius:14px; overflow:hidden; cursor:pointer; transition:transform .15s,box-shadow .15s; border:1px solid var(--line); box-shadow:var(--shadow); padding:9px; }
.card:hover { transform:translateY(-2px); }
.thumb { flex:0 0 132px; width:132px; height:76px; border-radius:10px; overflow:hidden; background:#000; position:relative; }
.thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.cnum { position:absolute; right:5px; bottom:5px; background:#000000b8; color:#fff; font-size:10.5px; line-height:1; padding:3px 6px; border-radius:8px; }
.thumb .ph { background:var(--panel2); height:100%; display:flex; align-items:center; justify-content:center; color:var(--muted2); font-size:20px; }
.meta { flex:1; min-width:0; }
.meta .l1 { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
.meta .rt { font-size:13.5px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.meta .tm { font-size:12px; color:var(--muted); white-space:nowrap; flex:0 0 auto; font-variant-numeric:tabular-nums; }
.meta .l2 { display:flex; justify-content:space-between; gap:8px; margin-top:4px; font-size:11.5px; color:var(--muted); white-space:nowrap; overflow:hidden; }
.meta .l2 span { overflow:hidden; text-overflow:ellipsis; }
.meta .l2 .sz { flex:0 0 auto; font-variant-numeric:tabular-nums; }
/* 日期分组小标题 (全部日期视图) */
.dhead { grid-column:1/-1; display:flex; align-items:baseline; gap:8px; padding:8px 2px 0; }
.dhead b { font-size:13px; font-weight:700; }
.dhead .n { font-size:11.5px; color:var(--muted2); }
.player { display:none; position:fixed; inset:0; height:100vh; height:100dvh; background:var(--bg); color:var(--text); z-index:100; flex-direction:column; }
/* 播放器子页面跟随全局主题: 卡片/头部/控制栏用变量, 只有视频画面区域保持恒定深色 (--stage), 看片更沉浸 */
.player.open { display:flex; }
.ptop { display:flex; align-items:center; gap:10px; padding:10px 14px; }
.ptop .pdl { margin-left:auto; }
.pback { background:none; border:none; color:var(--text); font-size:16.5px; cursor:pointer; padding:4px 2px; white-space:nowrap; }
.pdl { background:none; border:none; color:var(--text); font-size:19px; cursor:pointer; padding:4px 6px; }
.pscroll { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch; padding:2px 14px 14px; display:flex; flex-direction:column; gap:12px; }
.pscroll > * { flex:0 0 auto; }
.vwrap { position:relative; border-radius:14px; overflow:hidden; max-width:980px; width:100%; margin:0 auto; background:var(--stage); }
/* 行车遥测面板 (视频上方, Tesla App 样式, 不遮挡画面) */
.tpanel { display:none; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px 18px 10px; max-width:980px; width:100%; margin:0 auto; }
.tpanel.show { display:block; }
.tpanel.folded .tp-rows { display:none; }
/* 无遥测事件(哨兵/停车): 顶部只保留 时间 + 城市·位置 一行, 不留空白块 */
.tpanel.plain { padding:12px 18px; }
.tpanel.plain .tp-mid,
.tpanel.plain .tp-fold,
.tpanel.plain .tp-rows,
.tpanel.plain .tp-tr { display:none; }
.infocard.plain .ic-time { display:none; }
.infocard.plain .ic-badge { margin-left:0; }
.tp-top { display:flex; justify-content:space-between; align-items:center; }
.tp-l { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.tp-date { font-size:13.5px; color:var(--text); font-variant-numeric:tabular-nums; }
.tp-loc { font-size:12.5px; color:var(--muted); }
/* 哨兵/纪念事件: 触发画面摄像头 (来自 event.json 的 camera 字段) */
.tp-tag { display:none; align-items:center; gap:4px; height:22px; padding:0 9px; border-radius:11px;
  background:var(--amberbg); color:var(--amber); font-size:11.5px; font-weight:600;
  border:1px solid var(--amberline); white-space:nowrap; align-self:center; }
.tp-tag.show { display:inline-flex; }
.tp-tag b { color:var(--amberb); font-weight:800; font-variant-numeric:tabular-nums; }
.tp-tr { display:flex; align-items:center; gap:9px; }
.tp-sig { font-size:16px; color:var(--muted2); line-height:1; }
.tp-sig.on { color:#f59e0b; }
.tp-gear { background:#34c759; color:#fff; font-weight:800; font-size:14px; width:26px; height:26px;
  border-radius:50%; display:inline-flex; align-items:center; justify-content:center; }
.tp-mid { display:flex; align-items:center; margin-top:8px; }
.tp-spd { font-size:34px; font-weight:800; color:var(--text); line-height:1.1; font-variant-numeric:tabular-nums; }
.tp-unit { font-size:13px; color:var(--muted); font-weight:600; margin-left:6px; }
.tp-badges { display:flex; align-items:center; gap:10px; margin-left:auto; }
.tp-c { width:34px; height:34px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:11.5px; font-weight:700; flex:none; }
.tp-c.t { background:var(--panel2); color:var(--text); font-size:15px; }
.tp-badge { display:inline-flex; align-items:center; gap:5px; height:30px; padding:0 11px 0 8px;
  border-radius:15px; font-size:12.5px; font-weight:700; font-variant-numeric:tabular-nums; flex:none; }
.tp-badge svg { width:16px; height:16px; flex:none; }
.tp-badge.thr { background:#1f7ae0; color:#fff; }
.tp-badge.brk { background:var(--panel2); color:var(--muted); }
.tp-badge.brk.on { background:#ff453a; color:#fff; box-shadow:0 0 9px #ff453a66; }
.tp-c.ap { background:linear-gradient(135deg,#f59e0b,#ef4444); color:#fff; font-size:10.5px; }
.tp-c.st { background:var(--panel2); color:var(--muted); }
.tp-c.st svg { width:20px; height:20px; }
.tp-fold { display:block; margin:8px 0 0 auto; background:none; border:none; color:var(--muted);
  font-size:12px; cursor:pointer; padding:4px 2px; }
.tp-rows { border-top:1px solid var(--line); margin-top:2px; padding-top:8px; display:grid;
  grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:1px 20px; }
.tp-rows div { color:var(--text); font-size:13px; padding:3px 0; font-variant-numeric:tabular-nums; }
.tp-rows .k { color:var(--muted); margin-right:9px; }
.vgrid { display:grid; gap:3px; background:var(--stage); }
.vcell { position:relative; overflow:hidden; background:var(--stage); cursor:zoom-in; aspect-ratio:16/9; }
.vcell.focused { grid-column:1/-1; grid-row:1/-1; cursor:zoom-out; }
.vcell video { width:100%; height:100%; background:var(--stage); object-fit:cover; display:block; }
/* 以下是叠在视频画面上的浮层, 永远在深色画面上, 用固定颜色 */
.startmask { position:absolute; inset:0; z-index:5; background:rgba(0,0,0,.55); color:#fff; font-size:16px; display:none; align-items:center; justify-content:center; cursor:pointer; letter-spacing:1px; }
.startmask.show { display:flex; }
.vcell .clabel { position:absolute; right:7px; bottom:7px; font-size:11px; color:#e8eef4; background:rgba(20,24,29,.72); border-radius:6px; padding:2px 7px; pointer-events:none; }
.vcell .cload { position:absolute; left:0; top:0; right:0; bottom:0; display:flex; align-items:center; justify-content:center; gap:7px; font-size:12.5px; color:#c6cdd4; background:rgba(10,13,18,.6); pointer-events:none; }
.vcell .cload.err { color:#ff6b66; }
.vcell .cload .spin { width:14px; height:14px; border:2px solid #3a4450; border-top-color:#10b981; border-radius:50%; animation:cspin .8s linear infinite; }
@keyframes cspin { to { transform:rotate(360deg); } }
.infocard { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:14px 16px 10px; max-width:980px; width:100%; margin:0 auto; box-shadow:var(--shadow); }
.ic-top { display:flex; align-items:center; gap:10px; }
.ic-time { font-size:14px; color:var(--text); }
.ic-badge { margin-left:auto; background:#3b82f6; color:#fff; font-size:12px; font-weight:600; border-radius:14px; padding:3px 12px; }
.ic-mid { display:flex; align-items:baseline; gap:12px; margin:10px 0 2px; flex-wrap:wrap; }
.ic-big { font-size:30px; font-weight:700; letter-spacing:.5px; color:var(--text); }
.ic-sub { font-size:12.5px; color:var(--muted); }
.clipbar { display:none; gap:6px; flex-wrap:wrap; margin-top:9px; }
.clipbar.show { display:flex; }
.clipbar button { background:var(--panel2); color:var(--muted); border:1px solid var(--line2); border-radius:8px;
  font-size:12px; padding:4px 9px; cursor:pointer; font-variant-numeric:tabular-nums; }
.clipbar button.on { background:var(--blue); border-color:var(--blue); color:#fff; }
.ic-rows { display:none; font-size:12.5px; line-height:1.9; color:var(--muted); word-break:break-all; border-top:1px solid var(--line); margin-top:8px; padding-top:8px; }
.ic-rows.open { display:block; }
.ick { display:inline-block; min-width:64px; color:var(--muted); }
.ic-fold { background:none; border:none; color:var(--muted); font-size:12px; cursor:pointer; padding:4px 0 2px; margin-left:auto; display:block; }
.pbar { display:flex; align-items:center; gap:12px; padding:10px 16px calc(12px + env(safe-area-inset-bottom,0px)); max-width:1008px; margin:0 auto; width:100%; box-sizing:border-box; }
.pp { width:44px; height:44px; border-radius:50%; background:var(--text); color:var(--panel); border:none; font-size:16px; cursor:pointer; flex:0 0 auto; }
.pbar input[type=range] { flex:1; accent-color:#3b82f6; min-width:80px; }
.pbar .t { font-size:13.5px; color:var(--text); min-width:44px; text-align:center; }
.spd { background:#3b82f6; color:#fff; border:none; border-radius:10px; padding:9px 15px; font-size:14px; font-weight:600; cursor:pointer; flex:0 0 auto; }
.qsel { position:relative; flex:0 0 auto; }
.qbtn { background:var(--panel2); color:var(--muted); border:1px solid var(--line2); border-radius:10px; padding:9px 12px; font-size:13px; font-weight:600; cursor:pointer; white-space:nowrap; }
.qbtn.on { background:#10b981; border-color:#10b981; color:#fff; }
.qmenu { display:none; position:absolute; right:0; bottom:calc(100% + 6px); background:var(--panel); border:1px solid var(--line2); border-radius:10px; padding:4px; min-width:96px; box-shadow:0 8px 22px #00000040; z-index:30; }
.qsel.open .qmenu { display:block; }
.qmenu button { display:block; width:100%; text-align:left; background:none; border:none; color:var(--text); font-size:13px; padding:9px 12px; border-radius:7px; cursor:pointer; white-space:nowrap; }
.qmenu button.on { background:#10b981; color:#fff; }
.qhint { font-size:12px; color:var(--amber); flex:0 0 100%; text-align:left; display:none; }
.empty { text-align:center; padding:80px 20px; color:var(--muted2); }
.empty .big { font-size:44px; margin-bottom:14px; }
/* 设置面板 */
.mask { display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:200; }
.mask.open { display:block; }
.panel { position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); width:min(560px,94vw); max-height:86vh; overflow:auto; background:var(--panel); border-radius:14px; z-index:201; padding:18px; display:none; box-shadow:0 12px 40px #00000040; }
.panel.open { display:block; }
.panel h2 { font-size:15px; margin-bottom:12px; display:flex; align-items:center; }
.panel h2 .x { margin-left:auto; background:none; border:none; color:var(--muted); font-size:20px; cursor:pointer; }
.stabs { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.stab { flex:1 1 0; min-width:0; padding:8px 2px; border-radius:10px; background:var(--panel2); color:var(--muted); text-align:center; cursor:pointer; font-size:12.5px; border:none; white-space:nowrap; }
.stab.active { background:var(--accent); color:#fff; }
.hint { font-size:11.5px; color:var(--muted2); line-height:1.6; margin-bottom:10px; }
/* 系统更新 (OTA) */
.updrow { display:flex; align-items:baseline; gap:8px; padding:7px 0; font-size:13px; }
.updk { color:var(--muted); min-width:64px; flex:0 0 auto; }
.updtag { font-size:11px; padding:1px 8px; border-radius:9px; background:var(--amberbg); color:var(--amber); border:1px solid var(--amberline); white-space:nowrap; }
.updtag.new { background:rgba(232,68,63,.14); color:var(--accent); border-color:rgba(232,68,63,.32); }
.updnotes { display:none; background:var(--panel2); border-radius:10px; padding:9px 12px; font-size:12.5px; line-height:1.85; color:var(--muted); margin:6px 0 2px; }
.updbtns { display:flex; gap:8px; margin:10px 0 8px; }
.updbtns .mini { flex:1; padding:9px 0; border-radius:9px; background:var(--panel2); border:none; color:var(--text); font-size:13px; cursor:pointer; }
.updbtns .mini.primary { background:var(--accent); color:#fff; }
.updbtns .mini:disabled { opacity:.45; cursor:default; }
#gear { position:relative; }
#gear.hasupd::after { content:''; position:absolute; top:4px; right:4px; width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 2px var(--headbg); }
.upbtn { display:block; width:100%; padding:10px; border:1px dashed var(--line2); border-radius:10px; color:var(--muted); text-align:center; cursor:pointer; font-size:13px; margin-bottom:12px; background:var(--panel2); }
.upbtn:hover { border-color:var(--accent); color:var(--text); }
.item { display:flex; align-items:center; gap:10px; background:var(--panel2); border-radius:10px; padding:9px 12px; margin-bottom:8px; font-size:13px; }
.item .n { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.item button { background:var(--panel2); border:1px solid var(--line2); color:var(--muted); border-radius:7px; padding:5px 10px; font-size:12px; cursor:pointer; }
.item button.danger:hover { background:var(--accent); border-color:var(--accent); color:#fff; }
.item button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.item .tag { font-size:10.5px; color:#4cc38a; }
.toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:var(--accent); color:#fff; padding:9px 20px; border-radius:20px; font-size:13px; z-index:300; display:none; }
.expp { display:none; position:fixed; right:14px; bottom:70px; width:min(320px,92vw); background:var(--panel); border:1px solid var(--line2); border-radius:14px; padding:14px; z-index:150; box-shadow:0 10px 30px #00000060; }
.expp.open { display:block; }
.expp .expt { font-size:13.5px; font-weight:600; margin-bottom:10px; display:flex; align-items:center; }
.expp .expt .x { margin-left:auto; background:none; border:none; color:var(--muted); font-size:17px; cursor:pointer; }
.expitem { display:flex; align-items:center; gap:8px; padding:7px 4px; font-size:13px; color:var(--text); cursor:pointer; }
.expitem input { accent-color:var(--accent); width:16px; height:16px; }
.expr { display:flex; gap:8px; margin-top:10px; }
.expr .mini { flex:1; padding:7px 0; border-radius:9px; background:var(--panel2); border:none; color:var(--text); font-size:12.5px; cursor:pointer; }
.expr .mini.primary { background:var(--accent); color:#fff; }
.stitch { display:none; position:fixed; inset:0; background:rgba(0,0,0,.72); z-index:260; align-items:center; justify-content:center; }
.stitch.open { display:flex; }
.stbox { width:min(340px,88vw); background:var(--panel); border-radius:16px; padding:20px 20px 16px; box-shadow:0 12px 40px #000c; text-align:center; }
.stt { font-size:14.5px; font-weight:600; margin-bottom:14px; }
.stbar { height:8px; border-radius:5px; background:var(--line2); overflow:hidden; }
.stfill { height:100%; width:0; background:linear-gradient(90deg,#3b82f6,#60a5fa); border-radius:5px; transition:width .4s; }
.stpct { font-size:13px; color:var(--muted); margin-top:8px; }
.sthint { font-size:11.5px; color:var(--muted2); margin-top:4px; line-height:1.5; }
</style></head><body>
<header><h1>🚗 特斯拉哨兵查看器</h1><span class="sub" id="stat"></span>
<span id="live" title="车机在线只读同步状态" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:4px 10px;white-space:nowrap"><span id="livedot" style="width:7px;height:7px;border-radius:50%;background:#5f6b76"></span><span id="livetxt">同步状态</span></span>
<nav class="tabs" id="tabs"></nav><button id="theme" title="主题: 自动/浅色/深色" onclick="cycleTheme()">🌗</button><button id="gear" title="皮肤与锁车音">⚙</button><button id="out" title="退出登录" onclick="location.href='/logout'">⏏</button></header>
<nav class="storwrap" id="storwrap" style="display:none">
  <div class="storcard">
    <div class="storrow"><span class="storlab" id="storlab">车载U盘存储</span><span class="storval" id="storval"></span></div>
    <div class="storbar"><i id="storfill"></i></div>
  </div>
</nav>
<nav class="filters" id="filters"></nav>
<nav class="filters" id="dfilters"></nav>
<input type="date" id="dpick" class="dpick" onchange="if(this.value) setDate(this.value)">
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
          <span class="tp-l"><span class="tp-date" id="tp-date">—</span><span class="tp-loc" id="tp-loc"></span><span class="tp-tag" id="tp-tag"></span></span>
          <span class="tp-tr"><span class="tp-sig" id="tp-bl">⬅</span><span class="tp-sig" id="tp-br">➡</span><span class="tp-gear" id="tp-gear">D</span></span>
        </div>
        <div class="tp-mid">
          <span class="tp-spd" id="tp-spd">0</span><span class="tp-unit">km/h</span>
          <span class="tp-badges">
            <span class="tp-badge brk" id="tp-brk" title="刹车 (数据源只有踩下/未踩, 无踏板开度)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="7.6"/><circle cx="12" cy="12" r="2.5"/><path d="M17.4 6.6l2.7-2.7"/></svg><b>刹车</b></span>
            <span class="tp-badge thr" id="tp-thr" title="油门"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M9.4 3.6h5.2a1.7 1.7 0 0 1 1.68 1.93l-1.35 12.3a1.8 1.8 0 0 1-1.79 1.6h-2.28a1.8 1.8 0 0 1-1.79-1.6L7.72 5.53A1.7 1.7 0 0 1 9.4 3.6z"/><path d="M8.6 15.1h6.8"/></svg><b id="tp-thr-n">0%</b></span>
            <span class="tp-c st" id="tp-st" title="方向盘角度"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.6"/><path d="M3.2 10.5h6M14.8 10.5h6M12 14.6V21"/></svg></span>
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
    <div class="infocard" id="infocard">
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
    <div class="qsel" id="qsel"></div>
    <div class="qhint" id="qhint"></div>
  </div>
</div>

<div class="expp" id="expp">
  <div class="expt">选择要导出的画面<button class="x" onclick="closeExport()">✕</button></div>
  <div id="expchks"></div>
  <label class="expitem" id="expwm-row" style="border-top:1px solid var(--line2);margin-top:6px;align-items:flex-start">
    <input type="checkbox" id="expwm" style="margin-top:2px">
    <span style="line-height:1.45">叠加行车信息水印<small style="display:block;color:#7d8894;font-size:11px;margin-top:2px">半透明单行: 时间/位置/车速/挡位/油门/刹车, 基本不挡画面</small></span>
  </label>
  <div class="expitem" id="expq-row" style="border-top:1px solid var(--line2);margin-top:6px">
    <span style="flex:0 0 auto;color:#c6cdd4;cursor:default">下载清晰度</span>
    <div class="qsel" id="expq" style="flex:1;display:flex;justify-content:flex-end"></div>
  </div>
  <div class="expr">
    <button class="mini" onclick="expChk(true)">全选</button>
    <button class="mini" onclick="expChk(false)">清空</button>
    <button class="mini" onclick="doExportZip()">打包zip</button>
    <button class="mini primary" onclick="doExport()">合成视频</button>
  </div>
  <div class="hint" style="margin-top:8px;margin-bottom:0">单选直接下载原文件(加水印时走本机合成); 多选合成 <b>一个多画面视频</b>。<b>下载/打包zip</b> 按所选清晰度导出 (首次需等转码); 水印合成始终原画</div>
</div>

<div class="mask" id="mask" onclick="closePanel()"></div>
<div class="panel" id="panel">
  <h2>⚙ 个性化 <button class="x" onclick="closePanel()">✕</button></h2>
  <div class="stabs">
    <button class="stab active" id="st-skin" onclick="stab('skin')">🎨 页面皮肤</button>
    <button class="stab" id="st-audio" onclick="stab('audio')">🔊 锁车语音</button>
    <button class="stab" id="st-files" onclick="stab('files')">📂 文件</button>
    <button class="stab" id="st-update" onclick="stab('update')">🔄 更新</button>
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
  <div id="tab-update" style="display:none">
    <div class="updrow"><span class="updk">当前版本</span><b id="upd-cur">—</b></div>
    <div class="updrow"><span class="updk">最新版本</span><b id="upd-new">—</b><span class="updtag" id="upd-tag">未检查</span></div>
    <div class="updnotes" id="upd-notes"></div>
    <div class="updbtns">
      <button class="mini" id="upd-check" onclick="otaCheck(true)">检查更新</button>
      <button class="mini primary" id="upd-go" onclick="otaApply()" disabled>立即升级</button>
    </div>
    <div class="hint" id="upd-msg">升级会从 GitHub 及多个镜像<b>并发竞速</b>拉取最新程序 (哪个源快用哪个, 被墙自动切换), 覆盖本机文件并自动重启服务 (约 5 秒); 配置、录像、登录信息不受影响。</div>
    <div class="updbtns"><button class="mini" id="upd-rb" onclick="otaRollback()" style="display:none">↩ 回滚到上一版本</button></div>
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
let data = {}, cur = null, syncTimer = null, currentCat = 'SentryClips', lastSig = '', reasonFilter = 'all', dateFilter = 'all';
let curClips = [], curClipIx = 0, TEL = null;
/* ---------- 主题: 自动(跟系统) / 浅色 / 深色, 手动循环切换并记住 ---------- */
const THEME_ICONS = { auto: '🌗', light: '☀️', dark: '🌙' };
const THEME_NEXT = { auto: 'light', light: 'dark', dark: 'auto' };
function applyTheme(mode) {
  let eff = mode;
  if (mode === 'auto') {
    eff = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.dataset.theme = eff;
  const b = document.getElementById('theme');
  if (b) { b.textContent = THEME_ICONS[mode] || '🌗'; b.title = '主题: ' + (mode === 'auto' ? '自动' : mode === 'light' ? '浅色' : '深色'); }
}
function cycleTheme() {
  const cur = localStorage.getItem('tsf_theme') || 'auto';
  const nxt = THEME_NEXT[cur] || 'auto';
  localStorage.setItem('tsf_theme', nxt);
  applyTheme(nxt);
}
applyTheme(localStorage.getItem('tsf_theme') || 'auto');
if (window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('tsf_theme') || 'auto') === 'auto') applyTheme('auto');
});
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
const CATS_NAME = __CAT_NAMES__;
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
  if (REASON_LABEL[r]) return REASON_LABEL[r];
  for (const k in REASON_LABEL) if (r.indexOf(k) === 0) return REASON_LABEL[k];
  return r;
}
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
    b.onclick = () => { document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); b.classList.add('active'); currentCat = c; reasonFilter = 'all'; dateFilter = 'all'; render(c); };
    tabs.appendChild(b);
  }
  document.getElementById('stat').textContent = `归档 ${Object.values(data).reduce((a, v) => a + v.length, 0)} 个事件`;
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
    dot.style.background = '#4cc38a';
    if (live.pending > 0) txt.textContent = `实时同步 · ${live.pending} 个待确认`;
    else if (live.ago !== null && live.ago !== undefined) txt.textContent = `实时同步 · ${live.ago}s 前`;
    else txt.textContent = '实时同步中';
  } else {
    dot.style.background = '#e8443f';
    txt.textContent = '同步服务未运行';
  }
  // 车载U盘存储条 (镜像未挂载时回落显示归档盘)
  const sw = document.getElementById('storwrap');
  if (live.disk && live.disk.total > 0) {
    const GB = 1073741824, d = live.disk;
    sw.style.display = '';
    document.getElementById('storlab').textContent = d.src === 'usb' ? '车载U盘存储' : '盒子归档存储';
    document.getElementById('storval').textContent =
      (d.used / GB).toFixed(1) + 'GB / ' + (d.total / GB).toFixed(1) + 'GB';
    const pct = Math.min(100, d.used / d.total * 100);
    const fill = document.getElementById('storfill');
    fill.style.width = pct.toFixed(1) + '%';
    fill.style.background = pct >= 90 ? '#e8443f' : '#2f6ee0';
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
      `<button class="chip${k === reasonFilter ? ' on' : ''}" onclick="setReason('${k}')">${label}<span class="n">${cnt[k] || 0}</span></button>`
    ).join('');
  } else {
    const rcnt = {};
    byDate.forEach(ev => { const r = reasonOf(ev); rcnt[r] = (rcnt[r] || 0) + 1; });
    const rkeys = Object.keys(rcnt).sort((a, b) => rcnt[b] - rcnt[a]);
    if (rkeys.length < 2) { rbox.className = 'filters'; rbox.innerHTML = ''; }
    else {
      rbox.className = 'filters show';
      const items = [['all', '全部', byDate.length]].concat(
        rkeys.map(k => [k, reasonLabel(k) !== k ? reasonLabel(k) : '未标注原因', rcnt[k]]));
      rbox.innerHTML = '<span class="flab">原因</span>' + items.map(([k, label, n]) =>
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
  const items = [['all', '全部日期', byReason.length]].concat(
    days.map(d => [d, d.slice(5).replace('-', '/'), dcnt[d] || 0]));
  dbox.innerHTML = '<span class="flab">日期</span>' + items.map(([k, label, n]) =>
    `<button class="chip d${k === dateFilter ? ' on' : ''}" onclick="setDate('${k}')">${label}<span class="n">${n}</span></button>`
  ).join('') + '<button class="chip pick" onclick="pickDate()" title="选择任意日期">选择日期…</button>';
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
/* 缩略图失败多半是临时的 (磁盘满/片段刚同步完还没落稳), 自动重试两次再显示占位图 */
function thumbErr(img) {
  const n = +(img.dataset.r || 0);
  if (n < 2) {
    img.dataset.r = n + 1;
    setTimeout(() => { img.src = img.src.split('?')[0] + '?r=' + (n + 1); }, n ? 8000 : 2500);
    return;
  }
  img.parentNode.innerHTML = '<div class="ph">🎞</div>';
}
/* 日期分组小标题: 今天/昨天/M月D日 */
function dheadLabel(d) {
  const t = new Date(), today = t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0');
  const y = new Date(t.getTime() - 864e5), yd = y.getFullYear() + '-' + String(y.getMonth() + 1).padStart(2, '0') + '-' + String(y.getDate()).padStart(2, '0');
  const m = d.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return d;
  if (d === today) return '今天';
  if (d === yd) return '昨天';
  return (+m[2]) + '月' + (+m[3]) + '日';
}
function render(cat) {
  const g = document.getElementById('grid'), e = document.getElementById('empty');
  g.innerHTML = '';
  renderFilters(cat);
  let evs = data[cat] || [];
  evs = evs.filter(ev => matchReason(cat, ev));
  if (dateFilter !== 'all') evs = evs.filter(ev => dateOf(ev) === dateFilter);
  e.style.display = evs.length ? 'none' : 'block';
  if (!evs.length) {
    e.innerHTML = (reasonFilter !== 'all' || dateFilter !== 'all')
      ? '<div class="big">🔍</div><div>该筛选下暂无事件<br><small style="font-size:12px">换个日期或点「全部」查看本分类所有事件</small></div>'
      : EMPTY_HTML;
  }
  let prevDay = null;
  for (const ev of evs) {
    const day = dateOf(ev);
    if (day && day !== prevDay) {           // 日期分组小标题 (像官方App的"下午 14:00-17:59 · 3条")
      const n = evs.filter(x => dateOf(x) === day).length;
      const h = document.createElement('div'); h.className = 'dhead';
      h.innerHTML = '<b>' + dheadLabel(day) + '</b><span class="n">' + n + ' 条</span>';
      g.appendChild(h);
      prevDay = day;
    }
    const card = document.createElement('div'); card.className = 'card';
    const groups = groupVideos(ev.videos);
    const vids = camSort(groups.length ? groups[groups.length - 1].vids : []);
    let th;
    if (vids.length) {
      /* 哨兵事件: 用触发摄像头在触发那一帧当封面; 其他/回落: 前摄像头第1秒 */
      const src = sentryThumbSrc(ev, groups) ||
        (vids.find(v => v.cam === 'front') || vids[0]).url.replace('/media/', '/thumb/');
      th = `<div class="thumb"><img loading="lazy" decoding="async" src="${src}" alt="" onerror="thumbErr(this)"><span class="cnum">${vids.length} 路</span></div>`;
    } else {
      th = `<div class="thumb"><div class="ph">🎞</div></div>`;
    }
    const mm = ev.meta || {};
    /* 标题: 触发原因·触发摄像头 (哨兵), 否则分类名 */
    let title = CATS_NAME[cat] || cat;
    if (mm.reason) {
      title = reasonLabel(mm.reason) || mm.reason;
      if (isSentryEv(mm) && mm.camera) {
        const ck = CAM_IDX[+mm.camera];
        if (ck) title += ' · ' + camLabel(ck);
      }
    }
    const tm = (ev.name.match(/_(\d{2}-\d{2})/) || [,''])[1].replace('-', ':');
    const tmTxt = tm ? tm : fmtName(ev.name);
    const extra = (mm.city ? ' · ' + mm.city : '') + (mm.street ? ' ' + mm.street : '');
    const dayTxt = dateFilter === 'all' && day ? day.slice(5).replace('-', '/') + ' · ' : '';
    const mb = ev.size / 1048576;
    const szTxt = mb >= 1 ? mb.toFixed(1) + ' MB' : (mb * 1024).toFixed(0) + ' KB';
    card.innerHTML = th + `<div class="meta"><div class="l1"><span class="rt" title="${title}">${title}</span><span class="tm">${tmTxt}${extra}</span></div><div class="l2"><span>${dayTxt}${vids.length} 路画面</span><span class="sz">${szTxt}</span></div></div>`;
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
    vid.src = qUrl(v.url); vid.muted = true; vid.defaultMuted = true; vid.preload = 'auto';
    /* 每格加载状态: 转码/缓冲时显示提示, 避免黑屏让人以为坏了 */
    const cl = document.createElement('div'); cl.className = 'cload';
    cl.innerHTML = '<span class="spin"></span><span>缓冲中…</span>';
    const clDone = () => { cl.style.display = 'none'; };
    vid.addEventListener('loadeddata', clDone);
    vid.addEventListener('playing', clDone);
    vid.addEventListener('error', () => {
      cl.className = 'cload err';
      cl.innerHTML = '<span>加载失败, 点其他片段再试</span>';
    });
    vid.addEventListener('waiting', () => { cl.style.display = ''; });
    /* 低清晰度首次播放需要后台转码, 缓冲时给出提示 */
    vid.addEventListener('waiting', () => {
      if (curQ !== 'orig') {
        const h = document.getElementById('qhint');
        h.textContent = '正在缓冲 (首次播放新片段需后台转码十几秒, 转好一次后秒开)…';
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
  document.getElementById('ic-cat').textContent = CATS_NAME[currentCat] || currentCat;
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

/* ---------- 导出 ---------- */
let expQ = 'orig';
function renderExpQ() {
  const curLabel = (Q_LIST.find(x => x[0] === expQ) || Q_LIST[0])[1];
  document.getElementById('expq').innerHTML =
    `<button class="qbtn ${expQ !== 'orig' ? 'on' : ''}" onclick="toggleExpQ(event)">${curLabel} ⌃</button>` +
    '<div class="qmenu">' + Q_LIST.map(([k, label]) =>
      `<button class="${k === expQ ? 'on' : ''}" onclick="expQ='${k}';renderExpQ();closeQ()">${label}</button>`).join('') + '</div>';
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
  const pp = document.getElementById('ppbtn'); if (pp) pp.textContent = '⏸';
}
function pauseAll() {
  document.querySelectorAll('#vgrid video').forEach(v => v.pause());
  const pp = document.getElementById('ppbtn'); if (pp) pp.textContent = '▶';
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
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2500);
}
function stab(which) {
  ['skin', 'audio', 'files', 'update'].forEach(k => {
    const b = document.getElementById('st-' + k), t = document.getElementById('tab-' + k);
    if (b) b.classList.toggle('active', k === which);
    if (t) t.style.display = k === which ? '' : 'none';
  });
  if (which === 'update') otaCheck(false);
}

/* ---------- 系统更新 (OTA) ---------- */
let otaInfo = null;
function esc(s) { return String(s == null ? '' : s).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])); }
async function otaCheck(force) {
  const tag = document.getElementById('upd-tag'), msg = document.getElementById('upd-msg');
  tag.className = 'updtag'; tag.textContent = '检查中…';
  msg.textContent = '正在连接 GitHub…';
  try {
    const j = await (await fetch('/api/ota/check' + (force ? '?force=1' : ''), { cache: 'no-store' })).json();
    otaInfo = j;
    document.getElementById('upd-cur').textContent = 'v' + j.current;
    document.getElementById('upd-new').textContent = j.latest ? 'v' + j.latest : '—';
    tag.className = 'updtag' + (j.has_update ? ' new' : '');
    tag.textContent = j.error ? '检查失败' : (j.has_update ? '有新版本' : '已是最新');
    const n = document.getElementById('upd-notes');
    if (j.notes && j.notes.length) {
      n.innerHTML = j.notes.map(x => '· ' + esc(x)).join('<br>');
      n.style.display = 'block';
    } else { n.style.display = 'none'; n.innerHTML = ''; }
    document.getElementById('upd-go').disabled = !j.has_update;
    document.getElementById('upd-rb').style.display = j.rollback_ready ? '' : 'none';
    msg.textContent = j.error
      ? '连不上 GitHub: ' + j.error + ' (可稍后重试)'
      : ('来源 ' + (j.source || '-') + (j.date ? ' · ' + j.date : '') + (j.has_update ? '' : ' · 已是最新'));
    document.getElementById('gear').classList.toggle('hasupd', !!j.has_update);
  } catch (e) {
    tag.className = 'updtag'; tag.textContent = '检查失败';
    msg.textContent = '检查失败: ' + e;
  }
}
/* 服务重启期间轮询, 起来后自动刷新页面 */
async function otaWaitBack() {
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const r = await fetch('/api/live', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (e) {}
  }
  location.reload();
}
async function otaApply() {
  if (!otaInfo || !otaInfo.has_update) return;
  if (!confirm('升级到 v' + otaInfo.latest + ' ?\n程序文件会被覆盖, 服务自动重启约 5 秒, 期间页面会短暂打不开。')) return;
  const btn = document.getElementById('upd-go'), msg = document.getElementById('upd-msg');
  btn.disabled = true;
  msg.textContent = '正在下载并校验新版本 (约 10~30 秒)…';
  try {
    const j = await (await fetch('/api/ota/apply', { method: 'POST' })).json();
    msg.textContent = j.msg;
    if (!j.ok) { btn.disabled = false; return; }
    otaWaitBack();
  } catch (e) {
    msg.textContent = '升级失败: ' + e;
    btn.disabled = false;
  }
}
async function otaRollback() {
  if (!confirm('回滚到升级前的版本?\n适合新版本出现问题时使用, 服务会自动重启。')) return;
  const msg = document.getElementById('upd-msg');
  msg.textContent = '正在回滚…';
  try {
    const j = await (await fetch('/api/ota/rollback', { method: 'POST' })).json();
    msg.textContent = j.msg;
    if (j.ok) otaWaitBack();
  } catch (e) { msg.textContent = '回滚失败: ' + e; }
}
/* 打开首页后静默检查一次 (结果缓存 10 分钟), 有新版本时齿轮上亮红点 */
setTimeout(() => {
  fetch('/api/ota/check', { cache: 'no-store' }).then(r => r.json()).then(j => {
    if (j && j.has_update) {
      const g = document.getElementById('gear');
      g.classList.add('hasupd');
      g.title = '有新版本 v' + j.latest + ' · 点开设置可升级';
    }
  }).catch(() => {});
}, 5000);
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


def disk_usage():
    """存储条数据: 优先取车载U盘 (镜像内部 exFAT, 挂载时); 未挂载回落到归档盘 (loop 镜像所在)"""
    for p, usb in ((MNT, True), (ARCHIVE, False)):
        try:
            st = os.statvfs(p)
            if st.f_blocks <= 0:
                continue
            fr = st.f_frsize
            return {"src": "usb" if usb else "loop",
                    "used": (st.f_blocks - st.f_bavail) * fr, "total": st.f_blocks * fr}
        except OSError:
            continue
    return None


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
    return {"running": running, "pending": pending, "ago": ago, "last": last, "disk": disk_usage()}


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
# ---------- 在线清晰度: 按需转码 + eMMC 缓存 (设备端单任务串行, 防止重转码打崩内核) ----------
# 源 1280x960 (4:3); 用固定目标码率: 公网 5G 上行只有 ~120KB/s, CRF 模式码率不可控会传不完
# 8 核 T760 实测: -threads 8 + superfast 比 2 线程快一倍 (20s->10s/分钟片段), 内存峰值充足 (720p 后仍余 ~980MB)
RES_SCALE = {"360p": (480, 360, 500), "480p": (640, 480, 800), "720p": (960, 720, 1500)}  # 宽,高,kbps
RES_DIR = os.environ.get("TSF_CACHE", "/opt/mu300") + "/res-cache"
try:
    os.makedirs(RES_DIR, exist_ok=True)
except OSError:
    pass
Q_SEM = threading.BoundedSemaphore(int(os.environ.get("TSF_TR_CODES", "2")))  # 并发转码上限: 8核允许2个×4线程, 多路点开排队时间减半
RES_MAX = 2 * 1024 * 1024 * 1024  # 清晰度缓存总量上限 2GB, 超了从最旧开始删


def res_cleanup():
    try:
        now = time.time()
        for f in os.listdir(RES_DIR):  # 清理残留转码临时文件 (进程被杀会留下 0 字节 tmp)
            if f.endswith(".tmp") and now - os.path.getmtime(os.path.join(RES_DIR, f)) > 1800:
                try:
                    os.unlink(os.path.join(RES_DIR, f))
                except OSError:
                    pass
        files = [os.path.join(RES_DIR, f) for f in os.listdir(RES_DIR)
                 if f.endswith(".mp4")]
        total = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
        if total <= RES_MAX:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files:
            if total <= RES_MAX:
                break
            sz = os.path.getsize(f)
            os.unlink(f)
            total -= sz
    except Exception:
        pass


# ---------- 归档滚动窗口 + 磁盘空间兜底 ----------
# 车机 U 盘里的 RecentClips 本身就是"滚动缓冲": 只留最近约 1 小时, 更旧的由车机自己覆盖。
# 但盒子这边过去是每来一段就抄一份、永久留着, 而哨兵开着时车整夜不睡, 4 路约 140MB/分钟
# (8.4GB/小时), 一晚上就能吃掉几十 GB —— 这才是盒子镜像被撑爆的真正原因。
# 现在让盒子副本按同样窗口滚动: 只留最近 ROLL_MIN 分钟, 更旧的从盒子归档里删掉。
# 车机 U 盘那份完全不碰 (同步脚本只读 U 盘, 靠 exFAT 解析器读, 从不删源文件);
# SentryClips / SavedClips 属于哨兵事件 + 用户主动保存的录像, 由下方 BOX_KEEP_DAYS /
# BOX_MAX_GB 定期清理管理 (只删盒子归档副本, 车机 U 盘那份同样不碰)。
RECENT_ROLL_MIN = int(os.environ.get("TSF_RECENT_ROLL_MIN", "60"))    # 归档滚动窗口(分钟), 0=关闭滚动
FREE_MIN_MB = int(os.environ.get("TSF_FREE_MIN_MB", "4096"))          # 剩余低于此值开始兜底清理
FREE_TARGET_MB = int(os.environ.get("TSF_FREE_TARGET_MB", "10240"))   # 兜底清理到恢复到该值为止
RECENT_KEEP_MIN = int(os.environ.get("TSF_RECENT_KEEP_MIN", "60"))    # 兜底清理时最近 N 分钟必留
# 盒子侧 SentryClips / SavedClips 定期清理 (方案B):
# 哨兵开着时 4 路约 8.4GB/小时, 哨兵事件 + 手动保存的录像只增不删, 盘迟早被吃满。
# 车机 U 盘那份永远不碰, 这里只清盒子归档副本。两个维度独立生效:
#   1) 按天数: 只保留最新事件往前 N 天内的归档 (锚点是最新事件而不是当前时间,
#      停车几天没新事件不会被误清空);
#   2) 按体积: 单个分类超过 MAX_GB 就从最旧的开始删, 直到回到上限以内。
#      teslacam.img 高水位约 170G, loop 盘 207.7G, 盒子归档预算约 12G/类比较安全。
# 同步进行中的目录 (1 小时内有改动) 跳过, 非时间戳命名的目录一律不动。
# 清掉的分类/事件会记入 /archive/.pruned 墓碑文件, 同步端读到就跳过 —— 否则车机 U 盘上
# 还留着的旧事件会被当成新片段重抄回来, 形成 删除→重抄 的死循环 (和 RecentClips 同理)。
BOX_KEEP_DAYS = int(os.environ.get("TSF_BOX_KEEP_DAYS", "14"))        # 盒子侧哨兵/手动保存保留天数, 0=不按天数清
BOX_MAX_GB = int(os.environ.get("TSF_BOX_MAX_GB", "12"))              # 盒子侧单个分类体积上限(GB), 0=不按体积清
PRUNED_FILE = os.path.join(ARCHIVE, ".pruned")                        # 墓碑: 每行 "分类/事件名", 同步端跳过


def free_mb(p):
    try:
        st = os.statvfs(p)
        return st.f_bavail * st.f_frsize // 1048576
    except OSError:
        return -1


def _ev_dt(name):
    """事件目录名就是片段起始时间 (2026-11-05_08-00-23)。
    用文件名里的车机时间来比较, 不受盒子时区设错的影响"""
    try:
        return datetime.datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def roll_recent():
    """盒子侧 RecentClips 按滚动窗口自动淘汰 (只删盒子归档, 不碰车机 U 盘)"""
    rd = os.path.join(ARCHIVE, "RecentClips")
    if RECENT_ROLL_MIN <= 0 or not os.path.isdir(rd):
        return 0
    evs = []
    for e in os.listdir(rd):
        dt = _ev_dt(e)
        if dt:                       # 非时间戳命名的目录一律不动
            evs.append((dt, e))
    if len(evs) < 2:
        return 0
    evs.sort()
    # 以"最新片段"为锚点而不是当前时间: 车休眠/哨兵关闭期间没有新片段, 就不会被误清空
    cutoff = evs[-1][0] - datetime.timedelta(minutes=RECENT_ROLL_MIN)
    n = 0
    for dt, e in evs:
        if dt < cutoff:
            shutil.rmtree(os.path.join(rd, e), ignore_errors=True)
            n += 1
    return n


def _dir_size(p):
    sz = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                sz += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return sz


def _tombstone(cat, ev):
    """记录已清理的事件, 同步端读到就跳过, 防止 删除→重抄 死循环"""
    try:
        with open(PRUNED_FILE, "a", encoding="utf-8") as f:
            f.write("%s/%s\n" % (cat, ev))
    except OSError:
        pass


def roll_box():
    """盒子侧 SentryClips / SavedClips 定期清理 (按天数 + 按体积), 只删盒子归档, 不碰车机 U 盘"""
    n = 0
    for cat in ("SentryClips", "SavedClips"):
        base = os.path.join(ARCHIVE, cat)
        if not os.path.isdir(base):
            continue
        evs = []
        for e in os.listdir(base):
            dt = _ev_dt(e)
            if dt:                       # 非时间戳命名的目录一律不动
                evs.append((dt, e))
        if not evs:
            continue
        evs.sort()
        # 目录统计 (1 小时内有改动的跳过: 可能还在同步写入)
        sized = []
        for dt, e in evs:
            p = os.path.join(base, e)
            try:
                if time.time() - os.path.getmtime(p) < 3600:
                    continue
                sized.append((dt, e, p, _dir_size(p)))
            except OSError:
                continue
        # 1) 按天数: 锚点 = 该分类最新事件, 只留最近 BOX_KEEP_DAYS 天
        if BOX_KEEP_DAYS > 0:
            cutoff = evs[-1][0] - datetime.timedelta(days=BOX_KEEP_DAYS)
            for dt, e, p, _sz in sized:
                if dt < cutoff and os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    if not os.path.isdir(p):
                        _tombstone(cat, e)
                        n += 1
        # 2) 按体积: 超出 BOX_MAX_GB 从最旧开始删
        if BOX_MAX_GB > 0:
            left = [(dt, e, p, sz) for dt, e, p, sz in sized if os.path.isdir(p)]
            left.sort()
            total = sum(sz for _, _, _, sz in left)
            budget = BOX_MAX_GB * (1 << 30)
            for dt, e, p, sz in left:
                if total <= budget:
                    break
                shutil.rmtree(p, ignore_errors=True)
                total -= sz
                if not os.path.isdir(p):
                    _tombstone(cat, e)
                    n += 1
    return n


def disk_guard():
    rd = os.path.join(ARCHIVE, "RecentClips")
    while True:
        try:
            roll_recent()
        except Exception:
            pass
        try:   # 盒子侧哨兵/手动保存归档定期清理 (方案B)
            roll_box()
        except Exception:
            pass
        try:   # 兜底: 万一别的数据把盘吃满, 再按空间淘汰最旧的
            if free_mb(ARCHIVE) < FREE_MIN_MB and os.path.isdir(rd):
                cutoff = time.time() - RECENT_KEEP_MIN * 60
                for e in sorted(os.listdir(rd)):   # 目录名即时间戳, 天然按时间排序
                    if free_mb(ARCHIVE) >= FREE_TARGET_MB:
                        break
                    p = os.path.join(rd, e)
                    try:
                        if not os.path.isdir(p) or os.path.getmtime(p) > cutoff:
                            continue
                    except OSError:
                        continue
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        time.sleep(300)


def get_variant(fp, qv):
    """返回 qv 清晰度的缓存文件路径; 没有就排队转码一份 (最多2个并发), 失败回退原文件"""
    try:
        st = os.stat(fp)
    except OSError:
        return fp
    key = hashlib.md5(("%s|%d|%d|%s" % (fp, st.st_size, st.st_mtime, qv)).encode()).hexdigest()
    out = os.path.join(RES_DIR, key + ".mp4")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    if not os.path.isfile(FFMPEG_BIN):
        return fp
    # 排队等空闲转码槽 (最多 10 分钟, 超时回退原文件)
    if not Q_SEM.acquire(timeout=600):
        return fp
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0:  # 别人刚转完
            return out
        try:  # 内存保护: 可用 <180MB 不转, 回退原文件
            with open("/proc/meminfo") as mf:
                for line in mf:
                    if line.startswith("MemAvailable"):
                        if int(line.split()[1]) < 180000:
                            return fp
                        break
        except Exception:
            pass
        w, h, kbps = RES_SCALE[qv]
        tmp = out + ".%d.tmp" % os.getpid()
        # 子进程把 oom_score_adj 调回正数 (父进程是 -300 会被继承), OOM 时优先杀转码而不是主服务
        cmd = ["sh", "-c",
               'echo 300 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
               FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-threads", "4", "-i", fp,
               "-vf", "scale=%d:%d" % (w, h),
               "-c:v", "libx264", "-preset", "superfast",
               "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * 3 // 2),
               "-bufsize", "%dk" % (kbps * 3),
               "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                res_cleanup()
                return out
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return fp
    finally:
        Q_SEM.release()
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


# ---------- 访问认证 ----------
# 默认关闭; 只要配了密码 (auth.conf 里的 pass= 或 sha256=) 就全站开启。
# 除登录页外, 页面/媒体流/下载/接口全部要过认证; 用 HMAC 签名 cookie (默认 30 天) 记住登录,
# 这样 <video>、缩略图、zip 下载这些子请求不会反复弹窗; 同时兼容 HTTP Basic 方便 curl。
# 配置文件: /opt/tesla-sentry/auth.conf  (user= / pass= / sha256= / secret= / ttl=)
AUTH_FILE = os.environ.get("TSF_AUTH_FILE", "/opt/tesla-sentry/auth.conf")


def _auth_conf():
    d = {}
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return d


_AC = _auth_conf()
AUTH_USER = os.environ.get("TSF_AUTH_USER") or _AC.get("user") or "admin"
AUTH_PASS = os.environ.get("TSF_AUTH_PASS") or _AC.get("pass") or ""
AUTH_SHA = (os.environ.get("TSF_AUTH_SHA256") or _AC.get("sha256") or "").strip().lower()
AUTH_SECRET = (os.environ.get("TSF_AUTH_SECRET") or _AC.get("secret") or "") or ("tsf-v1|" + (AUTH_SHA or AUTH_PASS))
AUTH_TTL = int(os.environ.get("TSF_AUTH_TTL") or _AC.get("ttl") or 30 * 86400)
AUTH_ON = bool(AUTH_SHA or AUTH_PASS)
FAILS = {}                       # ip -> [失败次数, 首次失败时间]
FAIL_LOCK = threading.Lock()


def pw_ok(pw):
    if AUTH_SHA:
        return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), AUTH_SHA)
    return hmac.compare_digest((pw or "").encode(), AUTH_PASS.encode())


def sess_token(exp):
    raw = "%s|%d" % (AUTH_USER, exp)
    sig = hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return "%d.%s" % (exp, sig)


def sess_ok(tok):
    try:
        exp = int(tok.split(".", 1)[0])
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(tok, sess_token(exp))


def fail_check(ip, inc):
    """同一 IP 10 分钟内连错 8 次就锁 5 分钟, 防爆破"""
    now = time.time()
    with FAIL_LOCK:
        cnt, t0 = FAILS.get(ip, [0, now])
        if now - t0 > 600:
            cnt, t0 = 0, now
        if inc:
            cnt += 1
        FAILS[ip] = [cnt, t0]
        return cnt >= 8


LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · 特斯拉哨兵云盘</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#0d1219;color:#e8eef4;font:15px/1.5 -apple-system,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;
 padding:24px}
.card{width:100%;max-width:340px;background:#161d26;border:1px solid #232c36;border-radius:18px;
 padding:26px 22px 22px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.dot{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#10b981,#059669);
 display:flex;align-items:center;justify-content:center;font-size:17px}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}
p.sub{margin:2px 0 0;font-size:12px;color:#7d8894}
label{display:block;font-size:12px;color:#97a3af;margin:12px 0 6px}
input{width:100%;padding:11px 12px;border-radius:11px;border:1px solid #2b3540;background:#0f151d;
 color:#e8eef4;font-size:15px;outline:none}
input:focus{border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.15)}
button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:11px;cursor:pointer;
 background:#10b981;color:#fff;font-size:15px;font-weight:600}
button:active{background:#0ea472}
.err{margin-top:12px;font-size:13px;color:#f87171;min-height:18px}
.foot{margin-top:16px;font-size:11px;color:#5f6b78;text-align:center}
</style></head><body>
<form class="card" method="post" action="/login">
  <div class="logo"><div class="dot">🚗</div>
    <div><h1>特斯拉哨兵云盘</h1><p class="sub">Tesla Sentry Drive</p></div></div>
  <label>用户名</label>
  <input name="u" autocomplete="username" autocapitalize="none" autocorrect="off" value="__USER__">
  <label>密码</label>
  <input name="p" type="password" autocomplete="current-password" autofocus>
  <input type="hidden" name="next" value="__NEXT__">
  <button type="submit">登录</button>
  <div class="err">__MSG__</div>
  <div class="foot">仅限本人访问 · 登录状态保留 __DAYS__ 天</div>
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ---- 认证 ----
    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _authed(self):
        if not AUTH_ON:
            return True
        if sess_ok(self._cookie("tsf_sess")):
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode("utf-8", "replace").partition(":")
            except Exception:
                return False
            return u == AUTH_USER and pw_ok(p)
        return False

    def _login_page(self, msg="", nxt="/", code=200):
        html = (LOGIN_HTML.replace("__USER__", AUTH_USER)
                .replace("__NEXT__", nxt.replace('"', "&quot;"))
                .replace("__MSG__", msg)
                .replace("__DAYS__", str(max(1, AUTH_TTL // 86400))))
        self._send(code, html.encode())

    def _do_login(self):
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            f = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            f = {}
        nxt = (f.get("next", ["/"])[0] or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        ip = self.client_address[0] if self.client_address else "?"
        if fail_check(ip, False):
            self._login_page("尝试次数过多，请 5 分钟后再试", nxt, 429)
            return
        if f.get("u", [""])[0] == AUTH_USER and pw_ok(f.get("p", [""])[0]):
            fail_check(ip, False)
            with FAIL_LOCK:
                FAILS.pop(ip, None)
            exp = int(time.time()) + AUTH_TTL
            self.send_response(302)
            self.send_header("Location", nxt)
            self.send_header("Set-Cookie",
                             "tsf_sess=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (sess_token(exp), AUTH_TTL))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fail_check(ip, True)
        time.sleep(0.4)          # 轻微延迟, 拖慢暴力破解
        self._login_page("用户名或密码不对", nxt, 401)

    def _gate(self):
        """未登录: 页面跳登录页, 媒体/接口直接 401。返回 True 表示可继续"""
        if not AUTH_ON:
            return True
        p = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if p in ("/login", "/logout"):
            return True
        if self._authed():
            return True
        if p in ("/", "/index.html") or "text/html" in (self.headers.get("Accept") or ""):
            self.send_response(302)
            self.send_header("Location", "/login?next=" + urllib.parse.quote(self.path or "/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(401, "unauthorized".encode(), "text/plain; charset=utf-8")
        return False

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
        if path == "/login":
            if not AUTH_ON:
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._login_page("", q.get("next", ["/"])[0])
            return
        if path == "/logout":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "tsf_sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._gate():
            return
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
        elif path == "/api/ota/check":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._json(ota_check(force=q.get("force", ["0"])[0] == "1"))
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
            out = fp
            qv = q.get("q", [""])[0]
            if qv in RES_SCALE:  # 下载也支持清晰度: 转码缓存副本, 失败回退原文件
                out = get_variant(fp, qv)
            serve_file(self, out)
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
            qv = q2.get("q", [""])[0]  # 打包zip也支持清晰度: 每路先取转码副本 (串行转码), 失败回退原文件
            if qv not in RES_SCALE:
                qv = ""
            tmp = tempfile.NamedTemporaryFile(prefix="exp_", suffix=".zip", delete=False)
            tmp.close()
            try:
                with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
                    for f in vids:
                        ffp = os.path.join(evdir, f)
                        if qv:
                            ffp = get_variant(ffp, qv)
                        z.write(ffp, arcname=evname + "/" + f)
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
            # 清晰度: ?q=360p/480p/720p 时返回按需转码的缓存副本 (失败回退原文件)
            qv = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", ["orig"])[0]
            if qv in RES_SCALE and qv != "orig":
                fp = get_variant(fp, qv)
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
        """列表缩略图: 用 ffmpeg 抽指定秒数一帧 (?t=秒, 默认 1), 结果按 (路径+大小+mtime+t) 哈希缓存在 eMMC"""
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
        try:
            ss_t = max(0.0, min(float(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t", ["1"])[0]), 3600.0))
        except ValueError:
            ss_t = 1.0
        key = hashlib.md5(("%s|%d|%d|%g" % (fp, st.st_size, int(st.st_mtime), ss_t)).encode()).hexdigest()
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
                        # 指定秒数可能落在片段末尾之外(触发帧偏移略超时长), 逐级回落
                        cands = []
                        for s in (("%.1f" % ss_t), "1", "0"):
                            if s not in cands:
                                cands.append(s)
                        for ss in cands:
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
        p = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if p == "/login" and AUTH_ON:
            self._do_login()
            return
        if not self._gate():
            return
        path = urllib.parse.unquote(self.path)
        if path == "/api/ota/apply":
            ok, msg = ota_apply()
            self._json({"ok": ok, "msg": msg})
            return
        if path == "/api/ota/rollback":
            ok, msg = ota_rollback()
            self._json({"ok": ok, "msg": msg})
            return
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
    # OTA 自检: 只验证模块能完整加载 (常量/函数定义无误), 不启动服务、不碰硬件
    if "--ota-check" in sys.argv:
        print("ok %s" % APP_VERSION)
        sys.exit(0)
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
    threading.Thread(target=disk_guard, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sentry viewer on 0.0.0.0:{PORT}")
    print("auth: %s (%s)" % ("ON, user=" + AUTH_USER if AUTH_ON else "OFF - 公网可直接访问!", AUTH_FILE))
    srv.serve_forever()
