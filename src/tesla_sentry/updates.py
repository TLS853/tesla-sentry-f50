"""Updates service."""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from . import config, deployment

# 升级方式: 校验整包 SHA-256, 暂存完整版本, 原子切换 current 指针。
# 国内直连 raw.githubusercontent 多数能通, 同时内置镜像源兜底, 依次尝试。
from . import __version__

APP_VERSION = __version__
OTA_REPO = config.get("TSF_OTA_REPO", "TLS853/tesla-sentry-f50")
OTA_REF = config.get("TSF_OTA_REF", "codex/modular-runtime")
_OTA_DEFAULT = ",".join([
    "https://raw.githubusercontent.com/%s/%s" % (OTA_REPO, OTA_REF),
])
OTA_SOURCES = [s.strip().rstrip("/") for s in config.get("TSF_OTA_SOURCES", _OTA_DEFAULT).split(",") if s.strip()]
OTA_BACKUP = config.get("TSF_OTA_BACKUP", os.path.join(config.settings.install_dir, "sentry-viewer.bak.py"))
OTA_STATE = config.get("TSF_OTA_STATE", os.path.join(config.settings.install_dir, "ota-state.json"))
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
    if config.get("TSF_OTA_ENABLED", "1") == "0":
        raise RuntimeError("离线更新模式: 请从电脑上传安装包更新")
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


def _ota_restart():
    """Restart both services; generic live readers also detect release switches."""
    if os.path.isfile("/etc/init.d/sentry-viewer"):
        subprocess.Popen(["/bin/sh", "-c", "/etc/init.d/tesla-live restart; /etc/init.d/sentry-viewer restart"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    if os.path.isdir("/run/systemd/system"):
        subprocess.Popen(["systemctl", "restart", "tesla-live.service", "sentry-viewer.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    os.execv(sys.executable, [sys.executable] + sys.argv)


OTA_LOCK = threading.Lock()


def ota_apply():
    if not OTA_LOCK.acquire(blocking=False):
        return False, "已有升级或回滚进行中"
    try:
        j = ota_latest(True)
        latest = str(j.get("version"))
        if _vtuple(latest) <= _vtuple(APP_VERSION):
            return False, "当前已是最新版本 (%s)" % APP_VERSION
        # Only our fixed complete-bundle format is accepted. Legacy single-file updates cannot
        # leave the viewer, reader and Shell helpers running different policy versions.
        digest = j.get("sha256", "")
        if j.get("bundle") != "dist/release.json" or not re.fullmatch(r"[a-f0-9]{64}", digest):
            return False, "升级源缺少整包清单, 请先发布构建产物"
        data, _base = _race_get("/dist/release.json?ts=%d" % int(time.time()), timeout=60, limit=8 << 20,
                               validate=lambda raw: hashlib.sha256(raw).hexdigest() == digest)
        deployment.install_release(data, config.settings.install_dir, latest)
        deployment.atomic_json(OTA_STATE, {"applied": latest, "prev": APP_VERSION, "at": int(time.time())})
        threading.Timer(1.2, _ota_restart).start()
        return True, "已升级到 %s, 服务正在重启…" % latest
    except Exception as ex:
        return False, "升级失败: %s" % ex
    finally:
        OTA_LOCK.release()


def ota_rollback():
    if not OTA_LOCK.acquire(blocking=False):
        return False, "已有升级或回滚进行中"
    try:
        deployment.rollback_release(config.settings.install_dir)
        threading.Timer(1.0, _ota_restart).start()
        return True, "已回滚完整程序包, 服务正在重启…"
    except Exception as ex:
        return False, "回滚失败: %s" % ex
    finally:
        OTA_LOCK.release()
