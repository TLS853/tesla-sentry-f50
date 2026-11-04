#!/bin/sh
# usb-selfheal.sh - 虚拟U盘自愈
#
# 场景: 主机(特斯拉车机 / Windows)对 U 盘执行"弹出"时, mass_storage 会收到
#       SCSI eject, 内核随即清空 lun.0/file, U 盘从此不再出现, 直到重新绑定镜像。
# 本脚本检测到 LUN 为空时自动重新导出, 建议由 cron 每 1~2 分钟执行一次。
#
# 安全: 网页"U盘管理"模式(镜像已挂载)期间不干预, 避免抢 loop/UDC。

[ -f /etc/tesla-sentry.conf ] && . /etc/tesla-sentry.conf

: "${TSF_DIR:=/opt/tesla-sentry}"
: "${TSF_IMG:=$TSF_DIR/teslacam.img}"
: "${TSF_MNT:=/tmp/teslamnt}"
: "${TSF_LOG_DIR:=$TSF_DIR}"

IMG="$TSF_IMG"
MNT="$TSF_MNT"
LOG="$TSF_LOG_DIR/tesla-gadget.log"
GADGET_SH="$TSF_DIR/tesla-gadget.sh"

find_gadget() {
    if [ -n "$TSF_GADGET" ] && [ -d "$TSF_GADGET" ]; then echo "$TSF_GADGET"; return 0; fi
    for d in /sys/kernel/config/usb_gadget/*/; do
        [ -d "$d/configs" ] && { echo "${d%/}"; return 0; }
    done
    return 1
}

[ -f "$IMG" ] || exit 0
GADGET=$(find_gadget) || exit 0
LUN="$GADGET/functions/mass_storage.0/lun.0/file"

# 网页正在管理(镜像已挂载) → 不干预
grep -q " $MNT " /proc/mounts && exit 0
# LUN 已绑定 → 无需处理
[ -n "$(cat "$LUN" 2>/dev/null)" ] && exit 0

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] selfheal: LUN empty (host ejected?), re-exporting" >> "$LOG"
setsid sh "$GADGET_SH" on </dev/null >>"$LOG" 2>&1 &
