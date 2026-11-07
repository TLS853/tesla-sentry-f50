"""Usb service."""
import functools
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from . import config, locking

# ---------- 车机U盘(镜像)临时管理 ----------
IMG = config.settings.image
MNT = config.settings.mount
LOOPDEV = config.get("TSF_LOOP", "/dev/loop3")
LOSETUP = config.get("TSF_LOSETUP") or (
    "/opt/mu300/busybox-bin/losetup"
    if os.path.exists("/opt/mu300/busybox-bin/losetup")
    else "losetup"  # 通用发行版走 PATH (util-linux)
)
GADGET_DIR = config.get("TSF_GADGET", "/sys/kernel/config/usb_gadget/linux")
USB_AUTO_DETACH = config.settings.usb_timeout

usb_lock = threading.RLock()
usb_attached = False
usb_lease = None
usb_timer = None
usb_generation = 0


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


def _arm_usb_timer():
    global usb_timer, usb_generation
    usb_generation += 1
    if usb_timer:
        usb_timer.cancel()
    usb_timer = threading.Timer(USB_AUTO_DETACH, usb_detach_safe, args=(usb_generation,))
    usb_timer.daemon = True
    usb_timer.start()


def usb_attach():
    """Hold the process lock for the entire management session, including uploads."""
    global usb_attached, usb_lease
    with usb_lock:
        if usb_attached and os.path.ismount(MNT):
            return True, "already"
        lease = locking.ProcessLock(locking.usb_lock_path())
        if not lease.acquire(blocking=False):
            return False, "镜像正由其它进程使用"
        usb_lease = lease
        try:
            if os.path.ismount(MNT):
                raise RuntimeError("镜像已有残留挂载, 请先恢复车机导出")
            os.makedirs(MNT, exist_ok=True)
            if not _gadget_rebind(False):
                raise RuntimeError("停止 USB 导出失败")
            time.sleep(1)
            r = subprocess.run([LOSETUP, LOOPDEV, IMG], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("losetup 失败: " + r.stderr.strip()[-120:])
            r = subprocess.run(["mount", "-t", "exfat", LOOPDEV, MNT], capture_output=True, text=True)
            if r.returncode != 0:
                subprocess.run([LOSETUP, "-d", LOOPDEV], capture_output=True)
                raise RuntimeError("挂载失败: " + r.stderr.strip()[-120:])
            usb_attached = True
            _arm_usb_timer()
            return True, "attached"
        except Exception as ex:
            # If mounting succeeded before an exception, keep ownership until cleanup succeeds.
            if os.path.ismount(MNT):
                usb_attached = True
                _arm_usb_timer()
            else:
                _gadget_rebind(True)
                usb_lease.release()
                usb_lease = None
            return False, str(ex)


def usb_detach():
    """Release ownership only after unmount and USB rebind both succeed."""
    global usb_attached, usb_timer, usb_lease, usb_generation
    with usb_lock:
        if usb_lease is None:
            lease = locking.ProcessLock(locking.usb_lock_path())
            if not lease.acquire(blocking=False):
                return False, "镜像正由其它进程使用"
            usb_lease = lease
        usb_generation += 1
        if usb_timer:
            usb_timer.cancel()
            usb_timer = None
        try:
            if os.path.ismount(MNT):
                subprocess.run(["sync"], capture_output=True)
                for _ in range(3):
                    r = subprocess.run(["umount", MNT], capture_output=True, text=True)
                    if r.returncode == 0:
                        break
                    time.sleep(2)
                else:
                    _arm_usb_timer()
                    return False, "卸载失败, 有进程占用 " + MNT
            subprocess.run([LOSETUP, "-d", LOOPDEV], capture_output=True)
            if not _gadget_rebind(True):
                _arm_usb_timer()
                return False, "恢复 USB 导出失败, 将自动重试"
            usb_attached = False
            usb_lease.release()
            usb_lease = None
            return True, "detached"
        except Exception as ex:
            _arm_usb_timer()
            return False, str(ex)


def usb_detach_safe(generation=None):
    with usb_lock:
        if generation is not None and generation != usb_generation:
            return
        ok, msg = usb_detach()
        print("auto detach:", ok, msg)


def usb_operation(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        # Prevent the idle timer from unmounting between path validation and file I/O.
        with usb_lock:
            if not usb_attached or not os.path.ismount(MNT):
                self._json({"ok": False, "msg": "车机U盘未连接"}, 400)
                return
            try:
                return fn(self, *args, **kwargs)
            finally:
                _arm_usb_timer()
    return wrapped


def usb_img_age():
    """车机最后一次写U盘镜像距今的秒数; 读不到返回 -1"""
    try:
        return time.time() - os.path.getmtime(IMG)
    except OSError:
        return -1


def live_latest():
    """归档里最新的 RecentClips 片段组与其入库时间 (秒); 没有返回 ("", 0)。
    片段由 tesla-live-sync.py 在后台持续只读同步进归档 (不打断车机), 延时约 1~2 分钟。"""
    base = os.path.join(config.settings.archive, "RecentClips")
    try:
        cands = sorted((n for n in os.listdir(base)
                        if re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", n)
                        and os.path.isdir(os.path.join(base, n))), reverse=True)
    except OSError:
        return "", 0.0
    if not cands:
        return "", 0.0
    newest = 0.0
    try:
        for f in os.listdir(os.path.join(base, cands[0])):
            newest = max(newest, os.path.getmtime(os.path.join(base, cands[0], f)))
    except OSError:
        pass
    return cands[0], newest


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
