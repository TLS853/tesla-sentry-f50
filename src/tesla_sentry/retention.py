"""Retention service."""
import datetime
import os
import shutil
import time
from . import archive_policy, config, locking

RECENT_ROLL_MIN = int(config.get("TSF_RECENT_ROLL_MIN", "60"))    # 归档滚动窗口(分钟), 0=关闭滚动
FREE_MIN_MB = int(config.get("TSF_FREE_MIN_MB", "4096"))          # 剩余低于此值开始兜底清理
FREE_TARGET_MB = int(config.get("TSF_FREE_TARGET_MB", "10240"))   # 兜底清理到恢复到该值为止
RECENT_KEEP_MIN = int(config.get("TSF_RECENT_KEEP_MIN", "60"))    # 兜底清理时最近 N 分钟必留
# 盒子侧 SentryClips / SavedClips 定期清理 (方案B):
# 哨兵开着时 4 路约 8.4GB/小时, 哨兵事件 + 手动保存的录像只增不删, 盘迟早被吃满。
# 车机 U 盘那份永远不碰, 这里只清盒子归档副本。两个维度独立生效:
#   1) 按天数: 只保留最新事件往前 N 天内的归档 (锚点是最新事件而不是当前时间,
#      停车几天没新事件不会被误清空);
#   2) 按体积: 单个分类超过 MAX_GB 就从最旧的开始删, 直到回到上限以内。
#      teslacam.img 高水位约 170G, loop 盘 207.7G, 盒子归档预算约 12G/类比较安全。
# 同步进行中的目录 (1 小时内有改动) 跳过, 非时间戳命名的目录一律不动。
# 清掉的分类/事件会记入 /archive/.pruned 墓碑文件, 同步端读到就跳过 —— 否则车机 U 盘上
# 还留着的旧事件会被当成新片段重抄回来, 形成 删除→重抄 的死循环 (和 RecentClips 同理)。
BOX_KEEP_DAYS = int(config.get("TSF_BOX_KEEP_DAYS", "14"))        # 盒子侧哨兵/手动保存保留天数, 0=不按天数清
BOX_MAX_GB = int(config.get("TSF_BOX_MAX_GB", "12"))              # 盒子侧单个分类体积上限(GB), 0=不按体积清
PRUNED_FILE = os.path.join(config.settings.archive, ".pruned")                        # 墓碑: 每行 "分类/事件名", 同步端跳过


def free_mb(p):
    try:
        st = os.statvfs(p)
        return st.f_bavail * st.f_frsize // 1048576
    except OSError:
        return -1


def _ev_dt(name):
    """事件目录名就是片段起始时间 (2026-11-05_08-00-23)。
    用文件名里的车机时间来比较, 不受盒子时区设错的影响"""
    return archive_policy.event_datetime(name)


@locking.archive_locked
def roll_recent():
    """盒子侧 RecentClips 按滚动窗口自动淘汰 (只删盒子归档, 不碰车机 U 盘)"""
    rd = os.path.join(config.settings.archive, "RecentClips")
    if RECENT_ROLL_MIN <= 0 or not os.path.isdir(rd):
        return 0
    evs = []
    for e in os.listdir(rd):
        dt = _ev_dt(e)
        if dt:                       # 非时间戳命名的目录一律不动
            evs.append((dt, e))
    if len(evs) < 2:
        return 0
    evs.sort()
    # 以"最新片段"为锚点而不是当前时间: 车休眠/哨兵关闭期间没有新片段, 就不会被误清空
    cutoff = evs[-1][0] - datetime.timedelta(minutes=RECENT_ROLL_MIN)
    n = 0
    for dt, e in evs:
        if dt < cutoff:
            shutil.rmtree(os.path.join(rd, e), ignore_errors=True)
            n += 1
    return n


def _dir_size(p):
    sz = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                sz += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return sz


def _tombstone(cat, ev):
    """记录已清理的事件, 同步端读到就跳过, 防止 删除→重抄 死循环"""
    try:
        with open(PRUNED_FILE, "a", encoding="utf-8") as f:
            f.write("%s/%s\n" % (cat, ev))
    except OSError:
        pass


@locking.archive_locked
def roll_box():
    """盒子侧 SentryClips / SavedClips 定期清理 (按天数 + 按体积), 只删盒子归档, 不碰车机 U 盘"""
    n = 0
    for cat in ("SentryClips", "SavedClips"):
        base = os.path.join(config.settings.archive, cat)
        if not os.path.isdir(base):
            continue
        evs = []
        for e in os.listdir(base):
            dt = _ev_dt(e)
            if dt:                       # 非时间戳命名的目录一律不动
                evs.append((dt, e))
        if not evs:
            continue
        evs.sort()
        # 目录统计 (1 小时内有改动的跳过: 可能还在同步写入)
        sized = []
        for dt, e in evs:
            p = os.path.join(base, e)
            try:
                if time.time() - os.path.getmtime(p) < 3600:
                    continue
                sized.append((dt, e, p, _dir_size(p)))
            except OSError:
                continue
        # 1) 按天数: 锚点 = 该分类最新事件, 只留最近 BOX_KEEP_DAYS 天
        if BOX_KEEP_DAYS > 0:
            cutoff = evs[-1][0] - datetime.timedelta(days=BOX_KEEP_DAYS)
            for dt, e, p, _sz in sized:
                if dt < cutoff and os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    if not os.path.isdir(p):
                        _tombstone(cat, e)
                        n += 1
        # 2) 按体积: 超出 BOX_MAX_GB 从最旧开始删
        if BOX_MAX_GB > 0:
            left = [(dt, e, p, sz) for dt, e, p, sz in sized if os.path.isdir(p)]
            left.sort()
            total = sum(sz for _, _, _, sz in left)
            budget = BOX_MAX_GB * (1 << 30)
            for dt, e, p, sz in left:
                if total <= budget:
                    break
                shutil.rmtree(p, ignore_errors=True)
                total -= sz
                if not os.path.isdir(p):
                    _tombstone(cat, e)
                    n += 1
    return n


def health_logger(stop_event):
    """每分钟记一条 内存/负载/ffmpeg 进程数 到持久日志 (环形, 最多 2000 行)。
    内核 lockup 重启会把 dmesg 清空, 这里是唯一能复盘"死前发生了什么"的证据。"""
    fp = os.path.join(config.settings.install_dir, "health.log")
    while not stop_event.is_set():
        try:
            mem = load = "?"
            with open("/proc/meminfo", encoding="ascii") as f:
                for ln in f:
                    if ln.startswith("MemAvailable"):
                        mem = "%dMB" % (int(ln.split()[1]) // 1024)
                        break
            with open("/proc/loadavg", encoding="ascii") as f:
                load = " ".join(f.read().split()[:3])
            ff = 0
            try:
                for pid in os.listdir("/proc"):
                    if not pid.isdigit():
                        continue
                    try:
                        with open("/proc/%s/comm" % pid, encoding="ascii") as f:
                            if f.read().strip() == "ffmpeg":
                                ff += 1
                    except OSError:
                        continue
            except OSError:
                pass
            with open(fp, "a", encoding="utf-8") as f:
                f.write("%s mem=%s load=%s ffmpeg=%d\n" % (
                    time.strftime("%m-%d %H:%M"), mem, load, ff))
            if os.path.getsize(fp) > 300 * 1024:      # 环形截断, 只留最近 ~2000 行
                try:
                    with open(fp, encoding="utf-8") as f:
                        tail = f.readlines()[-2000:]
                    with open(fp, "w", encoding="utf-8") as f:
                        f.writelines(tail)
                except OSError:
                    pass
        except Exception:
            pass
        stop_event.wait(60)


def disk_guard(stop_event):
    rd = os.path.join(config.settings.archive, "RecentClips")
    while not stop_event.is_set():
        try:
            roll_recent()
        except Exception:
            pass
        try:   # 盒子侧哨兵/手动保存归档定期清理 (方案B)
            roll_box()
        except Exception:
            pass
        try:   # 兜底: 万一别的数据把盘吃满, 再按空间淘汰最旧的
            with locking.ProcessLock(os.path.join(config.settings.archive, ".archive.lock")):
                if free_mb(config.settings.archive) < FREE_MIN_MB and os.path.isdir(rd):
                    cutoff = time.time() - RECENT_KEEP_MIN * 60
                    for e in sorted(os.listdir(rd)):   # 目录名即时间戳, 天然按时间排序
                        if free_mb(config.settings.archive) >= FREE_TARGET_MB:
                            break
                        p = os.path.join(rd, e)
                        try:
                            if not os.path.isdir(p) or os.path.getmtime(p) > cutoff:
                                continue
                        except OSError:
                            continue
                        shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        stop_event.wait(300)


# ---------- 飞书推送: 哨兵触发时把信息和触发画面推到群里 ----------
# 机器人 webhook 只能发文字卡片; 要带封面图必须配「自建应用」的 app_id/app_secret
# (应用需开机器人能力 + im:message/im:resource 权限), 由本程序换取 token 上传图片拿 img_key。
