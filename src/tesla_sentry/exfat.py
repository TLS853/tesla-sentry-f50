"""Exfat service."""
import os
import struct
import time

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
        try:
            stat = os.fstat(self.fd)
            self.fd_ino = stat.st_ino
            self.fd_dev = stat.st_dev
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
            if not 9 <= vbr[108] <= 12 or vbr[108] + vbr[109] > 25:
                raise ValueError("invalid exFAT sector/cluster size")
            self.bytes_per_sector = 1 << vbr[108]
            self.cluster_size = self.bytes_per_sector << vbr[109]
            self.num_fats = vbr[110]
            # 多 FAT 时由 VolumeFlags bit0 指定活动 FAT
            self.active_fat = self.fat_offset
            if self.num_fats > 1 and (self.vol_flags & 0x1):
                self.active_fat += self.fat_len
            self.heap_byte = self.heap_offset * self.bytes_per_sector

            if not self.cluster_count or not 2 <= self.root_cluster < self.cluster_count + 2:
                raise ValueError("invalid exFAT root cluster")
            if self.vol_len * self.bytes_per_sector > stat.st_size:
                raise ValueError("truncated exFAT volume")
        except BaseException:
            self.close()
            raise

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
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
