#!/bin/sh
# tesla-sync.sh - 归档哨兵视频 + 把锁车音/音效写入车机U盘
#
# 安全机制:
#  1. 镜像超过 TSF_IDLE 秒无写入才动手 (避免打断行车录制)
#  2. 归档期间虚拟U盘短暂消失(几秒), 结束后立即恢复导出
#  3. 网页处于"U盘管理"模式(镜像已被挂载)时自动跳过, 不抢 loop/UDC
#  4. 全程日志

set -a
[ ! -f "${TSF_CONF:-/etc/tesla-sentry.conf}" ] || . "${TSF_CONF:-/etc/tesla-sentry.conf}"
set +a

if [ -z "${TSF_USB_LOCK_FD:-}" ]; then
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    VIEWER="${TSF_DIR:-/opt/tesla-sentry}/sentry-viewer.py"
    [ -f "$VIEWER" ] || VIEWER="$SCRIPT_DIR/../sentry-viewer.py"
    exec "${TSF_PYTHON:-python3}" "$VIEWER" --usb-lock-exec /bin/sh "$0" "$@"
fi


: "${TSF_DIR:=/opt/tesla-sentry}"
: "${TSF_IMG:=$TSF_DIR/teslacam.img}"
: "${TSF_ARCHIVE:=/archive}"
: "${TSF_MNT:=/tmp/teslamnt}"
: "${TSF_IDLE:=600}"
: "${TSF_LOG_DIR:=$TSF_DIR}"

IMG="$TSF_IMG"
ARCHIVE="$TSF_ARCHIVE"
AUDIO_Q="$ARCHIVE/boombox-queue"
AUDIO_DONE="$ARCHIVE/boombox-done"
MNT="$TSF_MNT"
IDLE="$TSF_IDLE"
LOG="$TSF_LOG_DIR/tesla-sync.log"

# --- 自动探测 ---
find_gadget() {
    if [ -n "$TSF_GADGET" ] && [ -d "$TSF_GADGET" ]; then echo "$TSF_GADGET"; return 0; fi
    for d in /sys/kernel/config/usb_gadget/*/; do
        [ -d "$d/configs" ] && { echo "${d%/}"; return 0; }
    done
    return 1
}
GADGET=$(find_gadget) || { echo "ERROR: gadget not found"; exit 1; }
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)

# losetup: 优先系统自带, 兼容 busybox 精简系统
if [ -n "$TSF_LOSETUP" ]; then BB="$TSF_LOSETUP"
elif command -v losetup >/dev/null 2>&1; then BB=$(command -v losetup)
elif [ -x /opt/mu300/busybox-bin/losetup ]; then BB=/opt/mu300/busybox-bin/losetup
else BB="busybox losetup"; fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }
restore() {
    [ -n "$UDC" ] || return 0
    echo "$UDC" > "$GADGET/UDC"
    log "export restored"
}
LB=""
DETACHED=0
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    if [ "$DETACHED" = 1 ]; then
        if grep -qs " $MNT " /proc/mounts; then
            sync
            if ! umount "$MNT" 2>>"$LOG"; then
                log "ERROR: umount failed; USB export stays detached"
                exit 1
            fi
        fi
        [ -z "$LB" ] || $BB -d "$LB" 2>/dev/null
        restore || rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# 0. 网页 U 盘管理模式占用中则跳过
if grep -qs " $MNT " /proc/mounts; then
    log "skip: mirror mounted by viewer usb-manage"
    echo "SKIP-MANAGED"
    exit 0
fi

# 1. 空闲检测 (设备可能没有 stat, 用 ls --full-time 换算)
now=$(date +%s)
lsdate=$(ls -l --full-time "$IMG" 2>/dev/null | awk '{print $6, $7}')
mtime=$(date -d "$lsdate" +%s 2>/dev/null)
if [ -z "$mtime" ]; then
    log "WARN: cannot parse mtime ($lsdate), skip"
    echo "SKIP-MTIME"
    exit 0
fi
age=$((now - mtime))
if [ "$age" -lt "$IDLE" ]; then
    echo "busy (last write ${age}s ago), skip"
    exit 0
fi

log "=== sync start (idle ${age}s) ==="

# 2. 短暂停止导出
echo "" > "$GADGET/UDC" || exit 1
DETACHED=1
sleep 1

# 3. 显式 losetup + 读写挂载 (归档要读, 音频要写)
mkdir -p "$MNT" "$AUDIO_Q" "$AUDIO_DONE"
LB=$($BB -f 2>/dev/null)
[ -z "$LB" ] && LB=/dev/loop3
if ! $BB "$LB" "$IMG" 2>>"$LOG"; then
    log "ERROR: losetup $LB failed"
    exit 1
fi
if ! mount -t exfat "$LB" "$MNT" 2>>"$LOG"; then
    log "ERROR: mount image failed (镜像不是 exFAT?)"
    $BB -d "$LB" 2>/dev/null
    exit 1
fi

# 4. 与在线同步共用墓碑/滚动窗口策略, 合并部分归档的目录
copied=$("${TSF_PYTHON:-python3}" "$TSF_DIR/sentry-viewer.py" --archive-copy "$MNT") || exit 1
log "archived $copied new files"

# 5. 把待写入的音频直接写进车机U盘根目录
#    (上传的音频全部放根目录: LockChime.wav 即锁车音, 其它文件名原样放根目录)
pushed=0
has_audio=0
for f in "$AUDIO_Q"/*.wav "$AUDIO_Q"/*.mp3; do
    [ -f "$f" ] && has_audio=1 && break
done
if [ "$has_audio" = "1" ]; then
    for f in "$AUDIO_Q"/*.wav "$AUDIO_Q"/*.mp3; do
        [ -f "$f" ] || continue
        bn=$(basename "$f")
        dest="$MNT/$bn"
        if cp "$f" "$dest" 2>>"$LOG"; then
            mv "$f" "$AUDIO_DONE/" && pushed=$((pushed+1))
            log "audio -> ${dest#$MNT/}"
        else
            log "audio copy fail: $bn"
        fi
    done
    sync
    log "pushed $pushed audio -> USB root"
fi

# cleanup trap 卸载成功后才恢复导出
echo "SYNC-DONE copied=$copied audio=$pushed"
