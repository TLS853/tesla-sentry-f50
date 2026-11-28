#!/bin/sh
# usb-selfheal.sh - 虚拟U盘自愈
#
# 场景: 主机(特斯拉车机 / Windows)对 U 盘执行"弹出"时, mass_storage 会收到
#       SCSI eject, 内核随即清空 lun.0/file, U 盘从此不再出现, 直到重新绑定镜像。
# 本脚本检测到 LUN 为空时自动重新导出, 建议由 cron 每 1~2 分钟执行一次。
#
# 安全: 网页"U盘管理"模式(镜像已挂载)期间不干预, 避免抢 loop/UDC。

set -a
[ ! -f "${TSF_CONF:-/etc/tesla-sentry.conf}" ] || . "${TSF_CONF:-/etc/tesla-sentry.conf}"
set +a

: "${TSF_DIR:=/opt/tesla-sentry}"
[ -f "${TSF_IMG:-$TSF_DIR/teslacam.img}" ] || exit 0
# The on operation takes the USB lease, checks the mount, link, medium and UDC,
# and only repairs the missing component. A healthy export needs no log entry.
output=$(/bin/sh "$TSF_DIR/tesla-gadget.sh" on 2>&1)
result=$?
case "$result" in
    0|75) ;;
    *) echo "[$(date '+%Y-%m-%d %H:%M:%S')] selfheal: $output" >> "${TSF_LOG_DIR:-$TSF_DIR}/tesla-gadget.log" ;;
esac
exit "$result"
