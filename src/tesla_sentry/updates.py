"""Install ZIP uploads and roll back local releases."""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

from . import __version__, config, deployment, update_package

APP_VERSION = __version__
# Retained only for migration from legacy single-file installations.
OTA_BACKUP = config.get("TSF_OTA_BACKUP", os.path.join(config.settings.install_dir, "sentry-viewer.bak.py"))
OTA_STATE = config.get("TSF_OTA_STATE", os.path.join(config.settings.install_dir, "ota-state.json"))
OTA_LOCK = threading.Lock()
_RESTART_PENDING = False
LOG = logging.getLogger(__name__)


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
    return {"ok": True, "current": APP_VERSION, "mode": "upload_zip", "rollback_ready": ready,
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


def ota_apply(stream, length, sha256=""):
    """Accept one bounded upload at a time, including while its body is being read."""
    if not OTA_LOCK.acquire(blocking=False):
        return False, "已有升级或回滚进行中"
    try:
        if _RESTART_PENDING:
            return False, "服务正在重启, 请稍后刷新页面"
        if not 0 < length <= update_package.MAX_ZIP_SIZE:
            return False, "更新 ZIP 为空或超过 16 MiB"
        if not isinstance(sha256, str) or (sha256 and not re.fullmatch(r"[a-fA-F0-9]{64}", sha256)):
            return False, "SHA-256 须为 64 位十六进制, 也可留空"
        try:
            data = stream.read(length)
        except OSError:
            return False, "读取 ZIP 上传失败, 请重试"
        if len(data) != length:
            return False, "ZIP 上传不完整, 请重试"
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
