#!/usr/bin/env python3
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
import argparse
import json
import os
import re
import struct
import sys
import time

IMG = os.environ.get("TSF_IMG", "/root/teslacam.img")
ARCHIVE = os.environ.get("TSF_ARCHIVE", "/archive")
MNT = os.environ.get("TSF_MNT", "/tmp/teslamnt")
LOG = os.environ.get("TSF_LIVE_LOG", "/root/tesla-live.log")
STATE = os.environ.get("TSF_LIVE_STATE", "/root/.tesla-live-state.json")
INTERVAL = int(os.environ.get("TSF_LIVE_INTERVAL", "15"))
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


def dos_time(raw):
    """exFAT 32bit 时间 → epoch 近似值"""
    if not raw:
        return 0
    sec = (raw & 0x1F) * 2
    minute = (raw >> 5) & 0x3F
    hour = (raw >> 11) & 0x1F
    day = (raw >> 16) & 0x1F
    month = (raw >> 21) & 0x0F
    year = ((raw >> 25) & 0x7F) + 1980
    try:
        return int(time.mktime((year, month, day, hour, minute, sec, 0, 0, -1)))
    except Exception:
        return 0


class Entry:
    __slots__ = ("name", "is_dir", "cluster", "size", "vdl", "mtime", "no_fat_chain", "valid")

    def __init__(self, name, is_dir, cluster, size, vdl, mtime, no_fat_chain, valid):
        self.name = name
        self.is_dir = is_dir
        self.cluster = cluster
        self.size = size
        self.vdl = vdl
        self.mtime = mtime
        self.no_fat_chain = no_fat_chain
        self.valid = valid

    def __repr__(self):
        return "<%s %s size=%d vdl=%d>" % ("DIR" if self.is_dir else "FILE", self.name, self.size, self.vdl)


class ExFat:
    """最小 exFAT 只读解析器 (只依赖标准库)"""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        self.fd_ino = os.fstat(self.fd).st_ino
        vbr = os.pread(self.fd, 512, 0)
        if len(vbr) < 512 or vbr[3:11] != b"EXFAT   ":
            raise ValueError("不是 exFAT 卷: %s" % path)
        self.part_offset = struct.unpack_from("<Q", vbr, 64)[0]
        self.vol_len = struct.unpack_from("<Q", vbr, 72)[0]
        self.fat_offset = struct.unpack_from("<I", vbr, 80)[0]
        self.fat_len = struct.unpack_from("<I", vbr, 84)[0]
        self.heap_offset = struct.unpack_from("<I", vbr, 88)[0]
        self.cluster_count = struct.unpack_from("<I", vbr, 92)[0]
        self.root_cluster = struct.unpack_from("<I", vbr, 96)[0]
        self.vol_flags = struct.unpack_from("<H", vbr, 106)[0]
        self.bytes_per_sector = 1 << vbr[108]
        self.cluster_size = self.bytes_per_sector << vbr[109]
        self.num_fats = vbr[110]
        # 多 FAT 时由 VolumeFlags bit0 指定活动 FAT
        self.active_fat = self.fat_offset
        if self.num_fats > 1 and (self.vol_flags & 0x1):
            self.active_fat += self.fat_len
        self.heap_byte = self.heap_offset * self.bytes_per_sector

    # ---------- 底层读取 ----------
    def pread(self, n, off):
        out = os.pread(self.fd, n, off)
        return out

    def cluster_off(self, cluster):
        return self.heap_byte + (cluster - 2) * self.cluster_size

    def read_cluster(self, cluster):
        return self.pread(self.cluster_size, self.cluster_off(cluster))

    def fat_next(self, cluster):
        ent = self.pread(4, self.active_fat * self.bytes_per_sector + cluster * 4)
        if len(ent) < 4:
            return 0xFFFFFFFF
        return struct.unpack("<I", ent)[0]

    def chain(self, first, max_clusters=None):
        """遍历簇链, 返回簇号列表"""
        if first < 2:
            return []
        limit = max_clusters or self.cluster_count
        out = []
        c = first
        while 2 <= c < (self.cluster_count + 2) and len(out) < limit:
            out.append(c)
            nxt = self.fat_next(c)
            if nxt >= 0xFFFFFFF7:  # EOC 区间
                break
            if nxt < 2 or nxt == c:
                break
            c = nxt
        return out

    def clusters_for(self, entry):
        """文件占用簇列表: NoFatChain 位表示连续分配"""
        n = (entry.size + self.cluster_size - 1) // self.cluster_size
        if n <= 0:
            return []
        if entry.no_fat_chain:
            return list(range(entry.cluster, entry.cluster + n))
        return self.chain(entry.cluster, n)

    def read_file(self, entry, limit=None):
        """按簇链读取文件内容 (可分块迭代)"""
        remain = entry.size if limit is None else min(entry.size, limit)
        for c in self.clusters_for(entry):
            if remain <= 0:
                break
            data = self.read_cluster(c)
            if not data:
                break
            chunk = data[:remain]
            remain -= len(chunk)
            yield chunk

    # ---------- 目录解析 ----------
    @staticmethod
    def _set_checksum(buf):
        cs = 0
        for i, b in enumerate(buf):
            if i in (2, 3):
                continue
            cs = ((cs << 15) | (cs >> 1)) & 0xFFFF
            cs = (cs + b) & 0xFFFF
        return cs

    def list_dir(self, cluster, size=0):
        """解析目录簇链, 返回 Entry 列表"""
        if size > 0:
            clist = self.clusters_for(Entry("", True, cluster, size, size, 0, False, True))
        else:
            clist = self.chain(cluster, 64)
        data = b"".join(self.read_cluster(c) for c in clist)
        entries = []
        i = 0
        total = len(data)
        while i + 32 <= total:
            etype = data[i]
            if etype == 0x00:  # 目录结束
                break
            if etype != 0x85:
                i += 32
                continue
            sec_count = data[i + 1]
            set_end = i + 32 * (1 + sec_count)
            if sec_count < 2 or set_end > total:
                break
            raw = data[i:set_end]
            ok = self._set_checksum(raw) == struct.unpack_from("<H", raw, 2)[0]
            attrs = struct.unpack_from("<H", raw, 4)[0]
            mtime = dos_time(struct.unpack_from("<I", raw, 12)[0])
            name = ""
            first = size_ = vdl = 0
            no_chain = False
            for j in range(sec_count):
                sec = raw[32 * (1 + j):32 * (2 + j)]
                st = sec[0]
                if st == 0xC0:
                    flags = sec[1]
                    vdl = struct.unpack_from("<Q", sec, 8)[0]
                    first = struct.unpack_from("<I", sec, 20)[0]
                    size_ = struct.unpack_from("<Q", sec, 24)[0]
                    no_chain = bool(flags & 0x02)
                elif st == 0xC1:
                    name += sec[2:32].decode("utf-16-le", "ignore")
            # 文件名在 0xC1 项里以 NUL 填充到末尾: 取第一个 NUL 之前最可靠
            # (NameLength 字段在部分实现里是字符数而非字节数, 不可全信)
            name = name.split("\x00")[0]
            if not name or name in (".", ".."):
                i = set_end
                continue
            entries.append(Entry(name, bool(attrs & 0x10), first, size_, vdl, mtime, no_chain, ok))
            i = set_end
        return entries

    def find(self, entries, name):
        low = name.lower()
        for e in entries:
            if e.name.lower() == low:
                return e
        return None

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


# ---------------- 同步逻辑 ----------------
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
        entries = exfat.list_dir(dir_entry.cluster, dir_entry.size)
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
    cat_list = exfat.list_dir(cam.cluster, cam.size)
    for cat in CATS:
        catdir = exfat.find(cat_list, cat)
        if not catdir or not catdir.is_dir:
            continue
        if cat == "RecentClips":
            # 平铺文件: 按文件名时间戳分组, 让网页能按事件展示
            try:
                items = exfat.list_dir(catdir.cluster, catdir.size)
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


def do_sync(exfat, state, quiet=False):
    """跑一轮; 返回 (新增文件数, 字节数)"""
    cur = scan(exfat)
    pending_key = "__pending__"
    pending = state.get(pending_key, {})
    new_pending = {}
    copied = 0
    bytes_ = 0
    for (cat, ev, name), e in cur.items():
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
        c = exfat.find(exfat.list_dir(cam.cluster, cam.size), cat)
        if not c:
            print("%s: 不存在" % cat)
            continue
        items = exfat.list_dir(c.cluster, c.size)
        print("%s: %d 项" % (cat, len(items)))
        for it in items[:8]:
            print("   %s %-40s size=%d" % ("DIR " if it.is_dir else "FILE", it.name, it.size))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=INTERVAL)
    args = ap.parse_args()

    if not os.path.exists(IMG):
        log("镜像不存在: %s" % IMG)
        return 1
    try:
        exfat = ExFat(IMG)
    except Exception as e:
        log("解析镜像失败: %s" % e)
        return 1

    if args.dump:
        dump(exfat)
        exfat.close()
        return 0

    log("在线同步启动: %s (簇 %dB, %d 簇)" % (IMG, exfat.cluster_size, exfat.cluster_count))
    state = load_state()
    if args.once:
        n, b = do_sync(exfat, state)
        log("单轮完成: %d 个新片段, %.1f MB" % (n, b / 1048576))
        exfat.close()
        return 0

    while True:
        try:
            # 网页正在管理U盘(已挂载)时跳过, 避免与挂载视图不一致
            if os.path.ismount(MNT):
                time.sleep(args.interval)
                continue
            # 镜像被重建/替换时 fd 会指向旧 inode, 需要重开
            try:
                st = os.stat(IMG)
                if st.st_ino != exfat.fd_ino:
                    log("镜像 inode 变化, 重新打开")
                    exfat.close()
                    exfat = ExFat(IMG)
            except OSError as ex:
                log("镜像不可读: %s" % ex)
                time.sleep(args.interval)
                continue
            do_sync(exfat, state)
        except Exception as e:
            log("轮询异常: %s" % e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
