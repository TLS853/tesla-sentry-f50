"""Usb service."""
import errno
import functools
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse
from . import config, exfat, locking

# ---------- 车机U盘(镜像)临时管理 ----------
IMG = config.settings.image
MNT = config.settings.mount
LOSETUP = config.get("TSF_LOSETUP") or (
    "/opt/mu300/busybox-bin/losetup"
    if os.path.exists("/opt/mu300/busybox-bin/losetup")
    else "losetup"
)
USB_AUTO_DETACH = config.settings.usb_timeout
LOG = logging.getLogger(__name__)

usb_lock = threading.RLock()
usb_attached = False
usb_lease = None
usb_volume = None
usb_timer = None
usb_generation = 0


def _command(label, *args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip()[-200:]
        raise RuntimeError(label + ("：" + detail if detail else ""))
    return result.stdout.strip()


def _lun():
    configured = config.get("TSF_GADGET")
    root = Path("/sys/kernel/config/usb_gadget")
    if configured:
        root = Path(configured).parent
    relative = "functions/mass_storage.0/lun.0/file"
    if configured:
        preferred = Path(configured) / relative
        if preferred.is_file():
            return preferred
    candidates = sorted(root.glob("*/" + relative))
    matches = [path for path in candidates if _backing_image(path) == IMG]
    if len(matches) == 1:
        return matches[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError("找不到唯一的车机 USB 存储设备")


def _backing_image(path):
    value = path.read_text().strip()
    return os.path.realpath(value) if value else ""


def _stop_export(lun):
    try:
        lun.write_text("\n")
    except OSError as error:
        if error.errno != errno.EBUSY:
            raise
        forced_eject = lun.with_name("forced_eject")
        if not forced_eject.is_file():
            raise RuntimeError("主机锁定了车机 U 盘，请在主机端弹出后重试") from error
        if usb_img_age() < 10:
            raise RuntimeError("车机 U 盘仍有写入，请暂停录制后重试") from error
        LOG.info("USB host locked an idle medium; ejecting the storage LUN")
        forced_eject.write_text("1\n")


def _mounted_loop():
    """Recover an interrupted session only when the mount belongs to this image."""
    with open("/proc/mounts") as mounts:
        for row in mounts:
            source, target, filesystem, *_ = row.split()
            target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), target)
            if target != MNT:
                continue
            backing = Path("/sys/class/block") / os.path.basename(source) / "loop/backing_file"
            if (filesystem == "exfat" and re.fullmatch(r"/dev/loop[0-9]+", source)
                    and _backing_image(backing) == IMG):
                return source
            break
    raise RuntimeError("挂载点被其它文件系统占用")


class UsbVolume:
    """One read/write mount with the USB process lease already held by the caller."""

    def __init__(self):
        self.lun = None
        self.loop = None
        self.restore = False

    def open(self):
        if os.path.ismount(MNT):
            raise RuntimeError("已有残留挂载，请先恢复车机 U 盘")
        with exfat.ExFat(IMG) as volume:
            offset = volume.volume_offset
            end = volume.heap_byte + volume.cluster_count * volume.cluster_size
            if end > volume.vol_len * volume.bytes_per_sector:
                raise ValueError("exFAT 簇区超出卷的范围")
        lun = _lun()
        backing = _backing_image(lun)
        if backing and backing != IMG:
            raise RuntimeError("车机导出的 U 盘与当前镜像配置不一致")
        os.makedirs(MNT, exist_ok=True)
        if backing:
            _stop_export(lun)
        self.lun = lun
        self.restore = True
        if _backing_image(lun):
            raise RuntimeError("车机 U 盘未能停止导出")
        loop = _command("找不到空闲的 loop 设备", LOSETUP, "-f")
        if not re.fullmatch(r"/dev/loop[0-9]+", loop):
            raise RuntimeError("losetup 未返回有效的 loop 设备")
        args = ["-o", str(offset)] if offset else []
        _command("连接 U 盘镜像失败", LOSETUP, *args, loop, IMG)
        self.loop = loop
        _command("挂载 exFAT 镜像失败", "mount", "-t", "exfat", "-o", "rw", loop, MNT)

    def close(self):
        if os.path.ismount(MNT):
            if not self.loop:
                self.loop = _mounted_loop()
            if not self.lun:
                self.lun = _lun()
            if _backing_image(self.lun):
                raise RuntimeError("镜像仍在对车机导出，无法恢复残留挂载")
            self.restore = True
            _command("卸载失败，车机 U 盘暂未恢复", "umount", MNT)
        if self.loop:
            _command("释放 loop 设备失败", LOSETUP, "-d", self.loop)
            self.loop = None
        if self.restore:
            self.restore_export()
            self.restore = False

    def restore_export(self):
        self.lun = self.lun or _lun()
        backing = _backing_image(self.lun)
        if backing and backing != IMG:
            raise RuntimeError("车机正在使用其它镜像，无法恢复导出")
        if not backing:
            self.lun.write_text(IMG + "\n")
            if _backing_image(self.lun) != IMG:
                raise RuntimeError("恢复车机 U 盘失败")
        # Recover devices left unbound by older versions.
        udc = self.lun.parents[3] / "UDC"
        if not udc.read_text().strip():
            controller = next(iter(sorted(Path("/sys/class/udc").iterdir())), None)
            if controller is None:
                raise RuntimeError("找不到 USB 控制器")
            udc.write_text(controller.name + "\n")


def _arm_usb_timer():
    global usb_timer, usb_generation
    usb_generation += 1
    if usb_timer:
        usb_timer.cancel()
    usb_timer = threading.Timer(USB_AUTO_DETACH, usb_detach_safe, args=(usb_generation,))
    usb_timer.daemon = True
    usb_timer.start()


def _acquire_usb():
    global usb_lease, usb_volume
    if usb_lease is None:
        lease = locking.ProcessLock(locking.usb_lock_path())
        if not lease.acquire(blocking=False):
            return False
        usb_lease, usb_volume = lease, UsbVolume()
    return True


def _release_usb():
    global usb_attached, usb_lease, usb_volume
    usb_attached = False
    usb_lease.release()
    usb_lease, usb_volume = None, None


def usb_attach():
    """Hold the process lock for the entire management session, including uploads."""
    global usb_attached
    with usb_lock:
        if usb_attached and os.path.ismount(MNT):
            _arm_usb_timer()
            return True, "already"
        if usb_lease is not None:
            return False, "请先恢复车机 U 盘导出"
        if not _acquire_usb():
            return False, "镜像正由同步服务使用，请稍后重试"
        try:
            usb_volume.open()
        except (OSError, RuntimeError, ValueError) as error:
            if usb_volume.restore:
                ok, message = usb_detach()
                if not ok:
                    return False, str(error) + "；" + message
            else:
                _release_usb()
            return False, str(error)
        usb_attached = True
        _arm_usb_timer()
        return True, "attached"


def usb_detach():
    """Release the lease only after unmount and export restoration succeed."""
    global usb_attached, usb_timer, usb_generation
    with usb_lock:
        recovering = usb_lease is not None or os.path.ismount(MNT)
        if not _acquire_usb():
            return False, "镜像正由同步服务使用，请稍后重试"
        usb_generation += 1
        if usb_timer:
            usb_timer.cancel()
            usb_timer = None
        try:
            usb_volume.restore = True
            usb_volume.close()
        except (OSError, RuntimeError) as error:
            usb_attached = False
            if recovering or usb_volume.lun is not None:
                _arm_usb_timer()
                return False, str(error) + "；系统会自动重试恢复"
            _release_usb()
            return False, str(error)
        _release_usb()
        return True, "detached"


def write_to_usb(write):
    """Perform an explicit write, using an open session or restoring export afterward."""
    with usb_lock:
        already = usb_attached and os.path.ismount(MNT)
        ok, message = usb_attach()
        if not ok:
            return {"ok": False, "msg": message}
        try:
            write(MNT)
            result = {"ok": True, "written": True, "msg": "已写入车机 U 盘"}
        except (OSError, RuntimeError, ValueError) as error:
            result = {"ok": False, "msg": "写入失败：" + str(error)}
        finally:
            if not already:
                restored, message = usb_detach()
        if already:
            if result["ok"]:
                result["msg"] += "，请完成管理并恢复车机导出"
        elif not restored:
            result["ok"] = False
            result["msg"] += "；" + message
        return result


def usb_detach_safe(generation=None):
    with usb_lock:
        if generation is not None and generation != usb_generation:
            return
        ok, msg = usb_detach()
        LOG.info("automatic USB restore: %s (%s)", ok, msg)


def usb_operation(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        # Prevent the idle timer from unmounting between path validation and file I/O.
        with usb_lock:
            if not usb_attached or not os.path.ismount(MNT):
                self._json({"ok": False, "attached": False,
                            "restore": usb_lease is not None or os.path.ismount(MNT),
                            "msg": "车机 U 盘未连接"}, 400)
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
