"""Telemetry service."""

from . import config
import hashlib
import json
import os
import struct
import threading
from . import transcode

TEL_DIR = config.settings.cache_dir + "/tel-cache"
TEL_SEM = threading.BoundedSemaphore(int(config.get("TSF_TEL_CONN", "1")))


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
    dur = transcode.probe_duration(fp)
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
