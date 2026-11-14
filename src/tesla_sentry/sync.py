"""tesla-live-sync.py - 车机在线只读同步 (不挂载, 不打断录制)

原理
----
虚拟U盘由 gadget 独占导出给车机, Linux 侧无法同时 mount (exFAT 非日志, 双挂载会互毁)。
但内核导出时 backing 文件本身仍可被只读打开 —— 本程序直接按 exFAT 规范解析
镜像的引导扇区 / FAT / 目录项, 沿簇链把车机写好的片段拷到归档区。

车机完全无感知: 全程 os.pread 只读, 不 mount、不写镜像。

"已完成"判定
------------
exFAT 目录项里 DataLength 在文件关闭时才落最终值, 因此必须:
  1. DataLength > 0
  2. 连续两轮扫描 (DataLength, ValidDataLength, mtime) 完全一致
才认为片段写完, 避免把半截 mp4 拷出来 (缺 moov 头放不了)。

用法
----
  tesla-live-sync.py --dump            打印镜像内 TeslaCam 目录树 (调试)
  tesla-live-sync.py --once            扫一轮就退出
  tesla-live-sync.py                   常驻轮询 (默认 15 秒)
"""

from . import config

import argparse
import json
import os
import re
import sys
import time
from . import archive_policy, exfat as exfat_reader, locking


IMG = config.settings.image
ARCHIVE = config.settings.archive
MNT = config.settings.mount
LOG = config.get("TSF_LIVE_LOG", os.path.join(config.get("TSF_DIR", "/opt/tesla-sentry"), "tesla-live.log"))
STATE = config.get("TSF_LIVE_STATE", os.path.join(config.get("TSF_DIR", "/opt/tesla-sentry"), ".tesla-live-state.json"))
INTERVAL = int(config.get("TSF_LIVE_INTERVAL", "15"))
# 归档滚动窗口(分钟), 与 sentry-viewer.py 的 TSF_RECENT_ROLL_MIN 保持一致;
# 窗口外的 RecentClips 根本不再抄进归档 —— 否则清掉的片段会被当成"新片段"重抄一遍,
# 形成 删除→重抄 的死循环, 既拖慢新片段落地又白写 eMMC
ROLL_MIN = config.settings.recent_minutes
CATS = ("SentryClips", "SavedClips", "RecentClips")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def safe_dest(*parts):
    p = os.path.join(ARCHIVE, *parts)
    rp = os.path.realpath(p)
    if not rp.startswith(os.path.realpath(ARCHIVE) + os.sep):
        raise ValueError("目标越界: %s" % rp)
    return rp


def same_file(path, size):
    try:
        return os.path.getsize(path) == size
    except OSError:
        return False


def copy_entry(exfat, entry, dest_path):
    """把镜像里的文件写到归档区, 返回写入字节数"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    written = 0
    with open(tmp, "wb") as f:
        for chunk in exfat.read_file(entry):
            f.write(chunk)
            written += len(chunk)
        f.flush()
        os.fsync(f.fileno())
    if written != entry.size:
        os.unlink(tmp)
        raise IOError("读取不完整 %d/%d" % (written, entry.size))
    os.replace(tmp, dest_path)
    return written


def iter_media(exfat, dir_entry, prefix=()):
    """遍历目录, 产出 (相对路径元组, 文件 Entry)"""
    if not dir_entry or not dir_entry.is_dir:
        return
    try:
        entries = exfat.list_subdir(dir_entry)
    except Exception as e:
        log("读取目录失败 %s: %s" % (dir_entry.name, e))
        return
    for e in entries:
        if not e.valid:
            continue
        if e.is_dir:
            yield from iter_media(exfat, e, prefix + (e.name,))
        elif (e.name.lower().endswith(".mp4") or e.name.lower() == "event.json") and e.size > 0:
            yield prefix, e


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(STATE + ".tmp", STATE)
    except Exception:
        pass


def scan(exfat):
    """返回 {(分类, 事件名, 文件名): Entry}"""
    found = {}
    try:
        root = exfat.list_dir(exfat.root_cluster)
    except Exception as e:
        log("读根目录失败: %s" % e)
        return found
    cam = exfat.find(root, "TeslaCam")
    if not cam or not cam.is_dir:
        return found
    cat_list = exfat.list_subdir(cam)
    for cat in CATS:
        catdir = exfat.find(cat_list, cat)
        if not catdir or not catdir.is_dir:
            continue
        if cat == "RecentClips":
            # 平铺文件: 按文件名时间戳分组, 让网页能按事件展示
            try:
                items = exfat.list_subdir(catdir)
            except Exception:
                continue
            for e in items:
                if e.is_dir or not e.name.lower().endswith(".mp4") or e.size <= 0 or not e.valid:
                    continue
                m = TS_RE.match(e.name)
                group = m.group(1) if m else "_misc"
                found[(cat, group, e.name)] = e
        else:
            for prefix, e in iter_media(exfat, catdir):
                if not prefix:
                    continue
                found[(cat, prefix[0], e.name)] = e
    return found


def ev_dt(ev):
    return archive_policy.event_datetime(ev)


def load_pruned():
    return archive_policy.read_pruned(ARCHIVE)


@locking.archive_locked
def do_sync(exfat, state, quiet=False):
    """跑一轮; 返回 (新增文件数, 字节数)"""
    cur = scan(exfat)
    pruned = load_pruned()
    cut = archive_policy.recent_cutoff([ev for cat, ev, name in cur if cat == "RecentClips"], ROLL_MIN)
    pending_key = "__pending__"
    pending = state.get(pending_key, {})
    new_pending = {}
    copied = 0
    bytes_ = 0
    for (cat, ev, name), e in cur.items():
        if not archive_policy.archive_allowed(cat, ev, pruned, cut):
            continue
        key = "%s/%s/%s" % (cat, ev, name)
        sig = [e.size, e.vdl, e.mtime]
        dest = safe_dest(cat, ev, name)
        if same_file(dest, e.size):
            continue
        # 需要连续两轮签名一致才认为写完
        if pending.get(key) != sig:
            new_pending[key] = sig
            if not quiet:
                log("发现新片段(待确认): %s (%d bytes)" % (key, e.size))
            continue
        try:
            n = copy_entry(exfat, e, dest)
            copied += 1
            bytes_ += n
            log("已同步 %s (%d KB)" % (key, n // 1024))
        except Exception as ex:
            log("同步失败 %s: %s" % (key, ex))
            new_pending[key] = sig
    state[pending_key] = new_pending
    save_state(state)
    return copied, bytes_


def dump(exfat):
    root = exfat.list_dir(exfat.root_cluster)
    print("根目录 (%d 项):" % len(root))
    for e in root:
        print("  %s %-30s size=%-10d %s" % ("DIR " if e.is_dir else "FILE", e.name, e.size,
                                            "" if e.valid else "[checksum bad]"))
    cam = exfat.find(root, "TeslaCam")
    if not cam:
        print("! 没有 TeslaCam 目录")
        return
    for cat in CATS:
        c = exfat.find(exfat.list_subdir(cam), cat)
        if not c:
            print("%s: 不存在" % cat)
            continue
        items = exfat.list_subdir(c)
        print("%s: %d 项" % (cat, len(items)))
        for it in items[:8]:
            print("   %s %-40s size=%d" % ("DIR " if it.is_dir else "FILE", it.name, it.size))


def main(argv=None, stop_event=None):
    """Poll under the USB lease; always close the reader on exit or image replacement."""
    import signal
    import threading
    parser = argparse.ArgumentParser(description="Read-only exFAT archive sync")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=INTERVAL)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    stop = stop_event if stop_event is not None else threading.Event()
    previous_signals = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_signals[sig] = signal.signal(sig, lambda *unused: stop.set())
    volume = None
    state = load_state()
    running_source = os.path.realpath(sys.argv[0])
    try:
        while not stop.is_set():
            if os.path.realpath(sys.argv[0]) != running_source:
                if volume is not None:
                    volume.close()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            lease = locking.ProcessLock(locking.usb_lock_path())
            if not lease.acquire(blocking=False):
                if args.once or args.dump:
                    return 75
                stop.wait(args.interval)
                continue
            try:
                if os.path.ismount(MNT):
                    if args.once or args.dump:
                        return 75
                else:
                    stat = os.stat(IMG)
                    if volume is None or (stat.st_dev, stat.st_ino) != (volume.fd_dev, volume.fd_ino):
                        if volume is not None:
                            volume.close()
                            volume = None
                        volume = exfat_reader.ExFat(IMG)
                    if args.dump:
                        dump(volume)
                        return 0
                    n, size = do_sync(volume, state)
                    if args.once:
                        log("单轮完成: %d 个新片段, %.1f MB" % (n, size / 1048576))
                        return 0
            except (OSError, ValueError) as error:
                log("同步暂不可用: %s" % error)
                if args.once or args.dump:
                    return 1
            finally:
                lease.release()
            stop.wait(args.interval)
    finally:
        if volume is not None:
            volume.close()
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    return 0
