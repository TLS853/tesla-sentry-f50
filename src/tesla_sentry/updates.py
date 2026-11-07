"""Explicit URL-to-ZIP updates. Status reads never contact a remote service."""
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config, deployment, update_package

APP_VERSION = __version__
# Retained only for migration from legacy single-file installations.
OTA_BACKUP = config.get("TSF_OTA_BACKUP", os.path.join(config.settings.install_dir, "sentry-viewer.bak.py"))
OTA_STATE = config.get("TSF_OTA_STATE", os.path.join(config.settings.install_dir, "ota-state.json"))
OTA_LOCK = threading.Lock()
_RESTART_PENDING = False
LOG = logging.getLogger(__name__)


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("请输入有效的 ZIP 下载 URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or parsed.fragment or port == 0):
            raise ValueError()
    except ValueError:
        raise ValueError("更新 URL 须为 HTTP / HTTPS 直链, 不支持账号密码或片段标记")
    return value


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("更新下载拒绝从 HTTPS 跳转到 HTTP")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_zip(url):
    url = _url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": "tsf-update/" + APP_VERSION, "Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + 120
    try:
        with urllib.request.build_opener(_Redirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError("下载未返回完整文件 (HTTP %s)" % response.status)
            length = response.headers.get("Content-Length")
            try:
                length = int(length) if length is not None else None
            except ValueError:
                raise ValueError("下载响应的文件长度无效")
            if length is not None and not 0 < length <= update_package.MAX_DOWNLOAD:
                raise ValueError("更新 ZIP 为空或超过 16 MiB")
            parts, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("下载超时, 请检查链接或网络后重试")
                block = response.read(min(262144, update_package.MAX_DOWNLOAD - size + 1))
                if not block:
                    break
                size += len(block)
                if size > update_package.MAX_DOWNLOAD:
                    raise ValueError("更新 ZIP 超过 16 MiB")
                parts.append(block)
            if not size or (length is not None and length != size):
                raise ValueError("ZIP 下载不完整, 请重试")
            return b"".join(parts)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ValueError("ZIP 下载失败 (HTTP %s), 请检查对象权限或签名链接是否过期" % status)
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        # Do not echo signed URLs or query strings into status responses or logs.
        raise ValueError("ZIP 下载失败, 请检查 URL、HTTPS 证书及设备网络")


def ota_status():
    root = os.path.realpath(config.settings.install_dir)
    ready = False
    try:
        with open(os.path.join(root, "previous-release.json"), encoding="utf-8") as stream:
            previous = json.load(stream).get("path")
        if isinstance(previous, str):
            previous = os.path.realpath(previous)
            store = os.path.realpath(os.path.join(root, "releases")) + os.sep
            ready = previous.startswith(store) and all(os.path.isfile(os.path.join(previous, n)) for n in deployment.RUNTIME_FILES)
    except (OSError, ValueError, AttributeError):
        pass
    return {"ok": True, "current": APP_VERSION, "mode": "manual_zip", "rollback_ready": ready,
            "busy": OTA_LOCK.locked() or _RESTART_PENDING}


def ota_check(force=False):
    """Compatibility for cached older clients: local status ONLY, regardless of force."""
    return ota_status()


def _ota_restart():
    if os.path.isfile("/etc/init.d/sentry-viewer"):
        subprocess.Popen(["/bin/sh", "-c", "/etc/init.d/tesla-live restart; /etc/init.d/sentry-viewer restart"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    if os.path.isdir("/run/systemd/system"):
        subprocess.Popen(["systemctl", "restart", "tesla-live.service", "sentry-viewer.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _restart_later():
    global _RESTART_PENDING
    _RESTART_PENDING = True
    timer = threading.Timer(1.2, _ota_restart)
    timer.daemon = True
    timer.start()


def ota_apply(url="", sha256=""):
    if not OTA_LOCK.acquire(blocking=False):
        return False, "已有升级或回滚进行中"
    try:
        if _RESTART_PENDING:
            return False, "服务正在重启, 请稍后刷新页面"
        _url(url)
        if not isinstance(sha256, str) or (sha256 and not re.fullmatch(r"[a-fA-F0-9]{64}", sha256)):
            return False, "SHA-256 须为 64 位十六进制, 也可留空"
        data = _download_zip(url)
        digest = hashlib.sha256(data).hexdigest()
        if sha256 and digest != sha256.lower():
            return False, "ZIP SHA-256 不匹配, 更新已停止"
        manifest, release = update_package.read_zip(data)
        latest = manifest["version"]
        if update_package.version_key(latest) <= update_package.version_key(APP_VERSION):
            return False, "更新包版本 %s 必须高于当前版本 %s; 如需降级请使用回滚" % (latest, APP_VERSION)
        deployment.install_release(release, config.settings.install_dir, latest)
        try:
            deployment.atomic_json(OTA_STATE, {"applied": latest, "prev": APP_VERSION,
                                   "sha256": digest, "at": int(time.time())})
        except OSError:
            LOG.warning("Release activated; optional update history could not be saved")
        _restart_later()
        return True, "已从 ZIP 升级到 %s, 服务正在重启…" % latest
    except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError) as error:
        return False, "升级失败: %s" % str(error)[:240]
    finally:
        OTA_LOCK.release()


def ota_rollback():
    if not OTA_LOCK.acquire(blocking=False):
        return False, "已有升级或回滚进行中"
    try:
        if _RESTART_PENDING:
            return False, "服务正在重启, 请稍后刷新页面"
        if not ota_status()["rollback_ready"]:
            return False, "没有可回滚的完整上一版本"
        deployment.rollback_release(config.settings.install_dir)
        _restart_later()
        return True, "已回滚完整程序包, 服务正在重启…"
    except (OSError, ValueError, RuntimeError) as error:
        return False, "回滚失败: %s" % str(error)[:240]
    finally:
        OTA_LOCK.release()
