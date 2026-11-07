"""Transcode service."""
import hashlib
import os
import subprocess
import threading
import time
from . import archive, config, media

def get_variant(fp, qv):
    """返回 qv 清晰度的缓存文件路径; 没有就排队转码一份 (最多2个并发), 失败回退原文件"""
    try:
        os.makedirs(media.RES_DIR, exist_ok=True)
        st = os.stat(fp)
    except OSError:
        return fp
    key = hashlib.md5(("%s|%d|%d|%s" % (fp, st.st_size, st.st_mtime, qv)).encode()).hexdigest()
    out = os.path.join(media.RES_DIR, key + ".mp4")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    if not os.path.isfile(media.FFMPEG_BIN):
        return fp
    # 排队等空闲转码槽 (最多 10 分钟, 超时回退原文件)
    if not media.Q_SEM.acquire(timeout=600):
        return fp
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0:  # 别人刚转完
            return out
        try:  # 内存保护: 可用 <180MB 不转, 回退原文件
            with open("/proc/meminfo") as mf:
                for line in mf:
                    if line.startswith("MemAvailable"):
                        if int(line.split()[1]) < 180000:
                            return fp
                        break
        except Exception:
            pass
        w, h, kbps = media.RES_SCALE[qv]
        tmp = out + ".%d.%d.tmp" % (os.getpid(), threading.get_ident())
        # 子进程把 oom_score_adj 调回正数 (父进程是 -300 会被继承), OOM 时优先杀转码而不是主服务
        cmd = ["sh", "-c",
               'echo 300 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
               media.FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-threads", "4", "-i", fp,
               "-vf", "scale=%d:%d" % (w, h),
               "-c:v", "libx264", "-preset", "superfast",
               "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * 3 // 2),
               "-bufsize", "%dk" % (kbps * 3),
               "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                media.res_cleanup()
                return out
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return fp
    finally:
        media.Q_SEM.release()
CAM_ORDER6 = ["left_pillar", "front", "right_pillar", "left_repeater", "back", "right_repeater"]
CAM_ORDER4 = ["front", "back", "left_repeater", "right_repeater"]


def cam_of(f):
    return f.rsplit("-", 1)[-1][:-4] if "-" in f else f


def clip_key(f):
    """文件名里的片段组标识: '2026-11-01_12-41-49-front.mp4' -> '2026-11-01_12-41-49'
    一个事件目录里常有 10 分钟的多组片段 (每组 4-6 路), 需要按组区分"""
    base = f[:-4] if f.lower().endswith(".mp4") else f
    return base.rsplit("-", 1)[0] if "-" in base else base


def latest_group(names):
    """取时间戳最新的一组片段 (与事件时间最接近的一组)"""
    if not names:
        return []
    groups = {}
    for f in names:
        groups.setdefault(clip_key(f), []).append(f)
    return groups[max(groups)]


def order_files(names):
    """按方位顺序排列 mp4: 6 路左右分列/前后居中, 4 路前后一排、左右B柱一排 (与前端 camSort 一致)"""
    order = CAM_ORDER6 if len(names) >= 6 else CAM_ORDER4
    return sorted(names, key=lambda f: (order.index(cam_of(f)) if cam_of(f) in order else 99, f))


def probe_duration(path):
    try:
        out = subprocess.run([media.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, timeout=20).stdout.decode().strip()
        return float(out)
    except Exception:
        return 0.0


def mem_available_mb():
    """读取 MemAvailable, 用于合成前的内存兜底检查"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def _stitch_worker(job_id, paths, out_path, total):
    n = len(paths)
    cols = 1 if n == 1 else (2 if n <= 4 else 3)
    # 每格分辨率: 默认 640x360 (4 路整幅 1280x720)。设备只有 1.4G 内存,
    # 输出 1080p 时 4 路 1080p 解码 + x264 帧缓存会把内存吃光导致整机失联。
    # 需要更高画质时用环境变量 TSF_CELL=800x450 之类覆盖。
    cell = config.get("TSF_CELL", "").lower()
    if "x" in cell:
        try:
            W, H = [int(x) for x in cell.split("x", 1)]
        except ValueError:
            W, H = 640, 360
    elif n == 2:
        W, H = 960, 540      # 两路时整幅 1920x540, 负载可控
    else:
        W, H = 640, 360
    cmd = [media.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
           "-filter_threads", "1"]
    for f in paths:
        cmd += ["-i", f]
    fc = []
    for i in range(n):
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=15[v{i}]")
    layout = "|".join("%d_%d" % ((i % cols) * W, (i // cols) * H) for i in range(n))
    fc.append("".join(f"[v{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[vout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-threads", "2", "-x264-params", "ref=2:bframes=0",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # 万一仍然吃爆内存, 让内核优先杀 ffmpeg, 保住 init/viewer 不被 OOM 带走
        try:
            with open("/proc/%d/oom_score_adj" % proc.pid, "w") as f:
                f.write("800")
        except OSError:
            pass
        deadline = time.time() + max(600, total * 30)  # 超时保护, 防止任务永久卡住
        timed_out = False
        for raw in proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("out_time_us=") and total > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1000000.0
                    pct = max(0, min(99, int(done / total * 100)))
                    with media.STITCH_LOCK:
                        if job_id in media.STITCH_JOBS:
                            media.STITCH_JOBS[job_id]["pct"] = pct
                except ValueError:
                    pass
            if time.time() > deadline:
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
        err = proc.stderr.read().decode("utf-8", "ignore")
        code = proc.wait()
        with media.STITCH_LOCK:
            job = media.STITCH_JOBS.get(job_id)
            if not job:
                return
            if timed_out:
                job["state"] = "error"
                job["err"] = "合成超时(超过 %d 分钟), 请少选几路再试" % int(max(600, total * 30) // 60)
                return
            if code == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                job["state"] = "done"
                job["pct"] = 100
                job["path"] = out_path
                job["size"] = os.path.getsize(out_path)
            else:
                job["state"] = "error"
                tail = [l for l in (err or "").strip().splitlines() if l.strip()]
                job["err"] = (tail[-1] if tail else "ffmpeg 退出码 %d" % code)[:300]
    except Exception as e:
        with media.STITCH_LOCK:
            job = media.STITCH_JOBS.get(job_id)
            if job:
                job["state"] = "error"
                job["err"] = str(e)[:300]


def start_stitch(cat, name, cams):
    """创建合成任务, 返回 (job_id, 错误信息)"""
    # 本机 (F50) 整个系统跑在 loop 镜像上, 实测 ffmpeg 多路 1080p 解码+编码会触发
    # 内核 soft/hard lockup -> panic=5 自动重启, 已用 2/4 路真实素材复现两次。
    # 前端默认走浏览器本机合成, 这里仅保留 TSF_STITCH=1 的实验开关。
    if config.get("TSF_STITCH", "0") != "1":
        return None, "设备端合成已停用(该负载会触发设备内核重启), 请使用浏览器合成或打包下载"
    if not os.path.isfile(media.FFMPEG_BIN):
        return None, "设备上未安装 ffmpeg (预期 %s)" % media.FFMPEG_BIN
    evdir = archive.safe_archive_dir(os.path.join(config.settings.archive, cat, name))
    if not evdir or not os.path.isdir(evdir):
        return None, "事件不存在"
    files = [f for f in os.listdir(evdir) if f.lower().endswith(".mp4")]
    files = latest_group(files)  # 只取最新一组片段, 24 个片段全拼会直接压垮设备
    if cams:
        files = [f for f in files if cam_of(f) in cams]
    files = order_files(files)
    if len(files) < 2:
        return None, "至少需要选择两路画面"
    paths = [os.path.join(evdir, f) for f in files]
    os.makedirs(media.STITCH_DIR, exist_ok=True)
    # 内存与并发保护: 设备只有 1.4G, 同时跑两个合成会直接 OOM 拖垮整机
    with media.STITCH_LOCK:
        running = [j for j in media.STITCH_JOBS.values() if j.get("state") == "running"]
    if running:
        return None, "已有合成任务在进行, 请等待完成后再试"
    avail = mem_available_mb()
    if 0 < avail < 300:
        return None, "设备可用内存不足(%dMB), 暂停合成以免影响录像同步" % avail
    now = time.time()
    for old_f in os.listdir(media.STITCH_DIR):
        fp_old = os.path.join(media.STITCH_DIR, old_f)
        try:
            if os.path.isfile(fp_old) and now - os.path.getmtime(fp_old) > 7200:
                os.unlink(fp_old)
        except OSError:
            pass
    job_id = "%s-%d" % (name.replace(":", "-"), int(time.time() * 1000) % 1000000)
    out_path = os.path.join(media.STITCH_DIR, job_id + ".mp4")
    total = probe_duration(paths[0])
    with media.STITCH_LOCK:
        media.STITCH_JOBS[job_id] = {"state": "running", "pct": 0, "path": None,
                               "name": "%s_%d路.mp4" % (name, len(files)), "total": total}
    threading.Thread(target=_stitch_worker, args=(job_id, paths, out_path, total), daemon=True).start()
    return job_id, None


# ---------- 行车遥测: 解码视频 SEI 里的 SeiMetadata protobuf ----------
# Tesla 在车辆行驶中录制的片段里, 每帧塞一条 SEI(type 6) NAL,
# payload 以 magic 0x42 0x42 0x69 开头, 后跟 protobuf:
#   1 version, 2 gear(0P/1D/2R/3N), 3 frame_seq, 4 speed_mps, 5 油门%,
#   6 方向盘角度, 7/8 左右转向灯, 9 刹车, 10 AP状态, 11/12 经纬度,
#   13 航向, 14/15/16 三轴加速度
