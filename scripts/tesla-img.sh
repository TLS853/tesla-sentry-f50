#!/bin/sh
# tesla-img.sh - 在「虚拟U盘镜像」上安全地执行操作
#
# 用法:
#   tesla-img.sh exec '<shell 命令>'   断开主机 -> 挂载镜像 -> 执行 -> 卸载 -> 重新挂回
#   tesla-img.sh detach                仅断开 LUN（电脑侧相当于弹出）
#   tesla-img.sh attach                仅挂回 LUN
#
# 为什么需要它:
#   镜像被 mass_storage 导出给主机（电脑/车机）时，设备侧同时写同一个文件，
#   主机的 FAT / 目录缓存不会刷新，会出现「新文件看不见」「可用空间不变」，
#   严重时目录项互相覆盖。任何设备侧写操作都必须先 detach。
#
# 可用环境变量覆盖: TSF_IMG / TSF_GADGET / TSF_LOOP / TSF_MNT

BB=/opt/mu300/busybox-bin
LOSETUP=$BB/losetup
[ -x "$LOSETUP" ] || LOSETUP=losetup

find_gadget() {
    for d in /sys/kernel/config/usb_gadget/*/; do
        [ -e "$d/UDC" ] && { echo "${d%/}"; return 0; }
    done
    return 1
}

GADGET="${TSF_GADGET:-$(find_gadget)}"
[ -n "$GADGET" ] || { echo "ERROR: 找不到 USB gadget (configfs 未挂载?)"; exit 1; }

LUN="$GADGET/functions/mass_storage.0/lun.0"
[ -d "$LUN" ] || { echo "ERROR: 找不到 mass_storage LUN"; exit 1; }

IMG="${TSF_IMG:-$(cat "$LUN/file" 2>/dev/null)}"
[ -n "$IMG" ] || IMG=/root/teslacam.img
[ -f "$IMG" ] || { echo "ERROR: 镜像不存在: $IMG"; exit 1; }

LOOP="${TSF_LOOP:-/dev/loop7}"
MNT="${TSF_MNT:-/tmp/imgrw}"

detach() { echo '' > "$LUN/file" 2>/dev/null; sleep 1; }
attach() { echo "$IMG" > "$LUN/file" 2>/dev/null; sleep 1; }

case "$1" in
detach)
    detach
    echo "DETACHED"
    ;;
attach)
    attach
    echo "ATTACHED"
    ;;
exec)
    [ -z "$2" ] && { echo "usage: $0 exec '<cmd>'"; exit 1; }
    detach
    mkdir -p "$MNT"
    $LOSETUP "$LOOP" "$IMG" 2>/dev/null
    if ! mount -t exfat -o rw "$LOOP" "$MNT" 2>/dev/null; then
        if ! mount -t vfat -o rw "$LOOP" "$MNT" 2>/dev/null; then
            echo "MOUNT-FAILED"
            $LOSETUP -d "$LOOP" 2>/dev/null
            attach
            exit 1
        fi
    fi
    sh -c "$2"
    RC=$?
    sync
    umount "$MNT" 2>/dev/null
    $LOSETUP -d "$LOOP" 2>/dev/null
    attach
    echo "DONE rc=$RC"
    exit $RC
    ;;
*)
    echo "usage: $0 exec '<cmd>' | detach | attach"
    exit 1
    ;;
esac
