#!/bin/sh
# Tesla Sentry F50 - 卸载脚本
# 用法: sudo ./uninstall.sh [--purge]
#   默认只停服务、删程序, 保留镜像和归档数据
#   --purge 连镜像、归档区一起删除 (不可恢复!)
set -e

PURGE=0
[ "$1" = "--purge" ] && PURGE=1

[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }
. /etc/tesla-sentry.conf 2>/dev/null || true
: "${TSF_DIR:=/opt/tesla-sentry}"
: "${TSF_IMG:=$TSF_DIR/teslacam.img}"
: "${TSF_ARCHIVE:=/archive}"

echo "==> 停止服务"
if [ -f /etc/rc.common ]; then
    [ -x /etc/init.d/sentry-viewer ] && /etc/init.d/sentry-viewer stop 2>/dev/null || true
    [ -x /etc/init.d/sentry-viewer ] && /etc/init.d/sentry-viewer disable 2>/dev/null || true
    [ -x /etc/init.d/tesla-gadget ] && /etc/init.d/tesla-gadget stop 2>/dev/null || true
    [ -x /etc/init.d/tesla-gadget ] && /etc/init.d/tesla-gadget disable 2>/dev/null || true
    sed -i '/tesla-sync.sh/d;/usb-selfheal.sh/d' /etc/crontabs/root 2>/dev/null || true
else
    systemctl disable --now sentry-viewer.service 2>/dev/null || true
    systemctl disable --now tesla-gadget.service 2>/dev/null || true
    rm -f /etc/cron.d/tesla-sentry
fi

if [ -f /etc/rc.common ]; then
    /etc/init.d/tesla-live stop 2>/dev/null || true
    /etc/init.d/tesla-live disable 2>/dev/null || true
else
    systemctl disable --now tesla-live.service 2>/dev/null || true
fi

echo "==> 恢复 USB 导出 (移除 mass_storage)"
# 停掉服务后手工收尾: 卸载残留挂载 + 删除函数 + 重新绑定
[ -f "$TSF_DIR/tesla-gadget.sh" ] && sh "$TSF_DIR/tesla-gadget.sh" off 2>/dev/null || true
umount "$TSF_MNT" 2>/dev/null || true

echo "==> 删除程序文件"
rm -f /etc/init.d/tesla-gadget /etc/init.d/sentry-viewer /etc/init.d/tesla-live
rm -f /etc/systemd/system/tesla-gadget.service /etc/systemd/system/sentry-viewer.service /etc/systemd/system/tesla-live.service
[ ! -d /run/systemd/system ] || systemctl daemon-reload || true
for name in sentry-viewer.py tesla-live-sync.py tesla-gadget.sh tesla-sync.sh usb-selfheal.sh tesla-img.sh tsf-check.sh; do
    rm -f "$TSF_DIR/$name"
done
rm -f "$TSF_DIR/current" "$TSF_DIR/previous-release.json"
rm -rf "$TSF_DIR/releases"
rm -f /etc/tesla-sentry.conf

if [ "$PURGE" = "1" ]; then
    echo "==> [--purge] 删除镜像与归档 (不可恢复)"
    rm -f "$TSF_IMG"
    rm -rf "$TSF_ARCHIVE"
    rmdir "$TSF_DIR" 2>/dev/null || true
else
    echo "==> 保留数据: $TSF_IMG , $TSF_ARCHIVE"
    echo "    需要彻底清除请加 --purge"
fi

echo "==> 卸载完成"
