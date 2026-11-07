"""Media service."""
import hashlib
import json
import os
import subprocess
import threading
import time
import shutil
from . import config, usb as usb_service

LIVE_SCRIPT = "tesla-live-sync.py"
LIVE_LOG = config.get("TSF_LIVE_LOG", os.path.join(config.settings.install_dir, "tesla-live.log"))
LIVE_STATE = config.get("TSF_LIVE_STATE", os.path.join(config.settings.install_dir, ".tesla-live-state.json"))


def _proc_running(needle):
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if needle.encode() in f.read():
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def disk_usage():
    """存储条数据: 优先取车载U盘 (镜像内部 exFAT, 挂载时); 未挂载回落到归档盘 (loop 镜像所在)"""
    for p, usb in ((usb_service.MNT, True), (config.settings.archive, False)):
        if usb and not os.path.ismount(usb_service.MNT):
            # MNT 目录本身常驻 (tmpfs), 未挂载镜像时 statvfs 会读到 tmpfs 的
            # 小容量 (如 0.7G), 必须跳过回落到归档盘
            continue
        try:
            st = os.statvfs(p)
            if st.f_blocks <= 0:
                continue
            fr = st.f_frsize
            return {"src": "usb" if usb else "loop",
                    "used": (st.f_blocks - st.f_bavail) * fr, "total": st.f_blocks * fr}
        except OSError:
            continue
    return None


def live_status():
    """车机在线只读同步的健康状态 (给页面显示心跳)"""
    running = _proc_running(LIVE_SCRIPT)
    pending = 0
    try:
        with open(LIVE_STATE) as f:
            pending = len(json.load(f).get("__pending__", {}))
    except Exception:
        pass
    ago, last = None, ""
    try:
        st = os.stat(LIVE_LOG)
        ago = int(time.time() - st.st_mtime)
        with open(LIVE_LOG, "rb") as f:
            f.seek(max(0, st.st_size - 2048))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        for line in reversed(lines):
            if "已同步" in line:
                last = line.split("] ", 1)[-1]
                break
        if not last and lines:
            last = lines[-1].split("] ", 1)[-1]
    except Exception:
        pass
    return {"running": running, "pending": pending, "ago": ago, "last": last, "disk": disk_usage()}


def active_skin():
    ap = os.path.join(config.settings.skins_dir, "active.json")
    try:
        with open(ap) as f:
            return json.load(f).get("skin")
    except Exception:
        return None


# ---------------- 多路合成导出 (ffmpeg) ----------------
FFMPEG_BIN = config.get("TSF_FFMPEG") or (
    "/opt/mu300/ffmpeg/ffmpeg" if os.path.exists("/opt/mu300/ffmpeg/ffmpeg") else "ffmpeg")
FFPROBE_BIN = config.get("TSF_FFPROBE") or (
    "/opt/mu300/ffmpeg/ffprobe" if os.path.exists("/opt/mu300/ffmpeg/ffprobe") else "ffprobe")
FFMPEG_BIN = shutil.which(FFMPEG_BIN) or FFMPEG_BIN
FFPROBE_BIN = shutil.which(FFPROBE_BIN) or FFPROBE_BIN
STITCH_DIR = config.settings.cache_dir + "/stitch-tmp"  # 不用 /tmp: tmpfs 占内存, 大视频会挤爆 RAM
STITCH_JOBS = {}
STITCH_LOCK = threading.Lock()
# 视频流并发上限: 事件多的时候浏览器会一口气请求上百个预览, 不限流会拖死设备
MEDIA_SEM = threading.BoundedSemaphore(int(config.get("TSF_MEDIA_CONN", "8")))
THUMB_DIR = config.settings.cache_dir + "/thumbs"
THUMB_SEM = threading.BoundedSemaphore(2)
THUMB_W = 400


def thumb_file(fp, ss_t=1.0):
    """抽指定秒数那一帧并缓存在 eMMC, 返回 jpg 路径; 抽不出来返回 None。
    列表缩略图 (/thumb) 和飞书推送封面共用。"""
    try:
        st = os.stat(fp)
    except OSError:
        return None
    key = hashlib.md5(("%s|%d|%d|%g" % (fp, st.st_size, int(st.st_mtime), ss_t)).encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key + ".jpg")
    try:                               # 0 字节是上次 ffmpeg 失败留下的残骸, 视为无效缓存
        if os.path.getsize(out) == 0:
            os.remove(out)
    except OSError:
        pass
    if os.path.isfile(out):
        return out
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except OSError:
        pass
    tmp = os.path.join(THUMB_DIR, "%s.%d.tmp.jpg" % (key, threading.get_ident()))
    if not THUMB_SEM.acquire(timeout=12):
        return None
    try:
        if os.path.isfile(out):
            return out
        # 指定秒数可能落在片段末尾之外(触发帧偏移略超时长), 逐级回落
        for ss in ("%.1f" % ss_t, "1", "0"):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
                subprocess.run([FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                                "-ss", ss, "-i", fp, "-frames:v", "1",
                                "-vf", "scale=%d:-2" % THUMB_W, "-q:v", "7", tmp],
                               capture_output=True, timeout=30)
                if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                    os.replace(tmp, out)
                    return out
            except Exception:
                pass
        return None
    finally:
        THUMB_SEM.release()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
# ---------- 在线清晰度: 按需转码 + eMMC 缓存 (设备端单任务串行, 防止重转码打崩内核) ----------
# 源 1280x960 (4:3); 用固定目标码率: 公网 5G 上行只有 ~120KB/s, CRF 模式码率不可控会传不完
# 8 核 T760 实测: -threads 8 + superfast 比 2 线程快一倍 (20s->10s/分钟片段), 内存峰值充足 (720p 后仍余 ~980MB)
RES_SCALE = {"360p": (480, 360, 500), "480p": (640, 480, 800), "720p": (960, 720, 1500)}  # 宽,高,kbps
RES_DIR = config.settings.cache_dir + "/res-cache"
Q_SEM = threading.BoundedSemaphore(int(config.get("TSF_TR_CODES", "2")))  # 并发转码上限: 8核允许2个×4线程, 多路点开排队时间减半
RES_MAX = 2 * 1024 * 1024 * 1024  # 清晰度缓存总量上限 2GB, 超了从最旧开始删


def res_cleanup():
    try:
        now = time.time()
        for f in os.listdir(RES_DIR):  # 清理残留转码临时文件 (进程被杀会留下 0 字节 tmp)
            if f.endswith(".tmp") and now - os.path.getmtime(os.path.join(RES_DIR, f)) > 1800:
                try:
                    os.unlink(os.path.join(RES_DIR, f))
                except OSError:
                    pass
        files = [os.path.join(RES_DIR, f) for f in os.listdir(RES_DIR)
                 if f.endswith(".mp4")]
        total = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
        if total <= RES_MAX:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files:
            if total <= RES_MAX:
                break
            sz = os.path.getsize(f)
            os.unlink(f)
            total -= sz
    except Exception:
        pass


# ---------- 归档滚动窗口 + 磁盘空间兜底 ----------
# 车机 U 盘里的 RecentClips 本身就是"滚动缓冲": 只留最近约 1 小时, 更旧的由车机自己覆盖。
# 但盒子这边过去是每来一段就抄一份、永久留着, 而哨兵开着时车整夜不睡, 4 路约 140MB/分钟
# (8.4GB/小时), 一晚上就能吃掉几十 GB —— 这才是盒子镜像被撑爆的真正原因。
# 现在让盒子副本按同样窗口滚动: 只留最近 ROLL_MIN 分钟, 更旧的从盒子归档里删掉。
# 车机 U 盘那份完全不碰 (同步脚本只读 U 盘, 靠 exFAT 解析器读, 从不删源文件);
# SentryClips / SavedClips 属于哨兵事件 + 用户主动保存的录像, 由下方 BOX_KEEP_DAYS /
# BOX_MAX_GB 定期清理管理 (只删盒子归档副本, 车机 U 盘那份同样不碰)。
