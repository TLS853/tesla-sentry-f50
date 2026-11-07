"""Video transcoding and telemetry watermarks for notification attachments."""
import datetime
import hashlib
import json
import os
import subprocess
from . import config
from . import media
from . import telemetry
from . import transcode

VID_Q = {"720p": (720, 1500), "480p": (480, 800), "360p": (360, 500)}   # 输出高度, 目标码率 kbps


FONT_DIR = config.get("TSF_FONTS") or os.path.join(config.settings.install_dir, "fonts")


def _wm_font():
    """水印用的中文字体 (libass 经 fontsdir 加载); 没有则返回空"""
    try:
        for n in sorted(os.listdir(FONT_DIR)):
            if n.lower().endswith((".ttf", ".otf", ".ttc")):
                return os.path.join(FONT_DIR, n)
    except OSError:
        pass
    return ""


def _wm_fontconf():
    """给 ffmpeg 自带的 fontconfig 一个最小配置 (OpenWrt 上没有 /etc/fonts/fonts.conf,
    会导致 libass 的字体选择整个失效, 连矢量图形都不画)。返回配置文件路径。"""
    p = os.path.join(FONT_DIR, "fonts.conf")
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        if not os.path.isfile(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0"?>\n'
                        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
                        '<fontconfig>\n  <dir>%s</dir>\n  <cachedir>/tmp/fccache</cachedir>\n'
                        '</fontconfig>\n' % FONT_DIR)
    except OSError:
        return ""
    return p


def _wm_ass(fp, ss, dur, meta):
    """生成与网页遥测面板同款的行车信息水印 ASS 字幕:
    顶部半透明色带 + 每秒一行 (左: 日期时间 城市·街道 / 右: 挡位 车速 油门, 刹车亮红)。
    返回 ass 路径; 无遥测数据或无字体时返回 None。"""
    if not _wm_font():
        return None
    tel = telemetry.telemetry_cached(fp)
    if not tel or not tel.get("frames"):
        return None
    frames = tel["frames"]
    try:
        t0 = datetime.datetime.strptime(os.path.basename(fp)[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        t0 = None
    loc = "·".join(x for x in (meta.get("city") or "", meta.get("street") or "") if x).strip()
    dur = int(dur)

    def fr_at(ct):
        """视频时间 ct (相对片段起点) 对应的遥测帧"""
        lo, hi, ans = 0, len(frames) - 1, frames[0]
        while lo <= hi:
            mid = (lo + hi) >> 1
            if frames[mid][0] <= ct:
                ans = frames[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    def esc(s):
        return s.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")

    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1280", "PlayResY: 720", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Wm,Noto Sans SC,26,&H00FFFFFF,&H00FFFFFF,&H96000000,&H7F000000,1,0,0,0,100,100,0,0,1,2,1,7,24,24,0,1",
        "Style: WmBr,Noto Sans SC,26,&H002045FF,&H002045FF,&H96000000,&H7F000000,1,0,0,0,100,100,0,0,1,2,1,8,24,24,0,1",
        "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    # 顶部色带 (整段): 模拟网页 rgba(13,18,26,.15) 的窄条
    lines.append("Dialogue: 0,0:00:00.00,%d:%02d:%02d.00,,Wm,0,0,0,,{\\an7\\pos(0,0)\\bord0\\shad0"
                 "\\1c&H1A120D&\\alpha&HD9&\\p1}m 0 0 l 1280 0 1280 62 0 62{\\p0}"
                 % (dur // 3600, dur % 3600 // 60, dur % 60))
    for n in range(dur):
        f = fr_at(ss + n)
        if t0 is not None:
            ts = (t0 + datetime.timedelta(seconds=ss + n)).strftime("%m-%d %H:%M:%S")
        else:
            ts = ""
        left = esc(" ".join(x for x in (ts, loc) if x))
        right = "%s %d km/h · 油门%d%%" % (f[1], int(round(f[2])), int(f[3]))
        ev = "0:00:%02d.000" % n
        ev2 = "0:00:%02d.000" % (n + 1) if n + 1 < dur else "%d:%02d:%02d.000" % (dur // 3600, dur % 3600 // 60, dur % 60)
        if left:
            lines.append("Dialogue: 1,%s,%s,Wm,,0,0,0,,{\\an7\\pos(24,32)}%s" % (ev, ev2, left))
        lines.append("Dialogue: 1,%s,%s,Wm,,0,0,0,,{\\an9\\pos(1256,32)}%s" % (ev, ev2, esc(right)))
        if f[5]:
            lines.append("Dialogue: 1,%s,%s,WmBr,,0,0,0,,{\\an8\\pos(640,32)}刹车" % (ev, ev2))
    p = os.path.join(media.RES_DIR, "wm-%s.ass" % hashlib.md5(("%s|%d|%d" % (fp, int(ss), dur)).encode()).hexdigest()[:16])
    with open(p, "w", encoding="utf-8") as fo:
        fo.write("\n".join(lines) + "\n")
    return p


def _make_push_video(fp, off, quality="480p", dur=30):
    """把触发摄像头那一路片段, 从触发点前 5 秒起截 dur 秒, 转成体积可控的 mp4; 返回缓存路径。
    有行车遥测(SEI)且设备上有字体时, 烧录与网页同款的行车信息水印 (手动保存的行车片段即有遥测;
    停车哨兵片段没有, 不烧)。"""
    h, kbps = VID_Q.get(quality) or VID_Q["480p"]
    dur = max(10, min(60, int(dur or 30)))
    ss = max(0.0, float(off) - 5.0)       # 触发点前 5 秒, 能看到"人走过来"的过程
    # 手动保存的片段: 触发点常在片段末尾, "前5秒起截"会只剩几秒。
    # 按实际时长钳制: 起点往前挪, 保证凑满 dur 秒且不越过片段末尾。
    try:
        clip_dur = transcode.probe_duration(fp) or 0.0
        if clip_dur > 0:
            dur = max(5, min(dur, int(clip_dur)))
            ss = max(0.0, min(ss, clip_dur - dur))
    except Exception:
        pass
    ass = ""
    try:
        if dur >= 10:
            meta = {}
            try:
                with open(os.path.join(os.path.dirname(fp), "event.json"), encoding="utf-8", errors="ignore") as f:
                    meta = json.load(f) or {}
            except Exception:
                meta = {}
            ass = _wm_ass(fp, ss, dur, meta) or ""
    except Exception:
        ass = ""
    key = hashlib.md5(("%s|%d|%.2f|%s|%d|%s" % (fp, int(off), ss, quality, dur, "wm" if ass else "-")).encode()).hexdigest()[:16]
    out = os.path.join(media.RES_DIR, "push-%s.mp4" % key)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    if not os.path.isfile(media.FFMPEG_BIN):
        raise RuntimeError("设备上没有 ffmpeg")
    tmp = out + ".%d.tmp" % os.getpid()
    vf = "scale=-2:%d" % h
    if ass:
        vf += ",subtitles='%s':fontsdir='%s'" % (ass.replace(":", "\\:").replace("'", ""), FONT_DIR.replace("'", ""))
    cmd = ["sh", "-c", 'echo 300 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
           media.FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "4",
           "-ss", "%.2f" % ss, "-t", str(dur), "-i", fp,
           "-vf", vf,
           "-c:v", "libx264", "-preset", "superfast",
           "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * 3 // 2), "-bufsize", "%dk" % (kbps * 3),
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-f", "mp4", "-y", tmp]
    try:
        env = dict(config.environment)
        if ass:
            fc = _wm_fontconf()
            if fc:
                env["FONTCONFIG_FILE"] = fc
        with media.Q_SEM:        # 与点播转码共用并发闸门: 设备上 ffmpeg 总数恒定, 叠加会触发内核 lockup 重启
            r = subprocess.run(cmd, capture_output=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("转码超时 (>5 分钟)")
    if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("转码失败: %s" % (r.stderr or b"")[:100].decode("utf-8", "ignore"))
    os.replace(tmp, out)
    try:
        media.res_cleanup()          # 推送视频也走同一个缓存目录, 超 2GB 从最旧删
    except Exception:
        pass
    return out


def _transcode_push_upload(src, quality="480p"):
    """把浏览器本机合成上传来的片段 (webm/mp4) 整段转成体积可控的 mp4, 供推飞书;
    返回 (mp4路径, 时长秒)。单路输入, 负载与推送转码同级, 设备可承受。"""
    h, kbps = VID_Q.get(quality) or VID_Q["480p"]
    try:
        st = os.stat(src)
    except OSError:
        raise RuntimeError("上传的片段不存在")
    key = hashlib.md5(("%d|%d|%s" % (st.st_size, int(st.st_mtime), quality)).encode()).hexdigest()[:16]
    out = os.path.join(media.RES_DIR, "push-manual-%s.mp4" % key)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out, transcode.probe_duration(out) or 0.0
    if not os.path.isfile(media.FFMPEG_BIN):
        raise RuntimeError("设备上没有 ffmpeg")
    tmp = out + ".%d.tmp" % os.getpid()
    cmd = ["sh", "-c", 'echo 300 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
           media.FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", src, "-vf", "scale=-2:%d" % h,
           "-c:v", "libx264", "-preset", "superfast",
           "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * 3 // 2), "-bufsize", "%dk" % (kbps * 3),
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-f", "mp4", "-y", tmp]
    try:
        with media.Q_SEM:        # 与点播转码共用并发闸门, 防止多路 ffmpeg 叠加触发内核 lockup 重启
            r = subprocess.run(cmd, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("转码超时 (>15 分钟)")
    if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("转码失败: %s" % (r.stderr or b"")[:120].decode("utf-8", "ignore"))
    os.replace(tmp, out)
    try:
        media.res_cleanup()
    except Exception:
        pass
    return out, transcode.probe_duration(out) or 0.0
