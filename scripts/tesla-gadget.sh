#!/bin/sh
# tesla-gadget.sh - 把镜像文件导出为 USB 虚拟U盘 (mass_storage)
# 用法: tesla-gadget.sh on|off|status|restore
#
# 设计要点:
#  - 只在现有 gadget 里"追加" mass_storage 功能, 不动 ncm/acm, 所以 USB 网卡和串口不中断
#  - 绑定失败自动回滚, 避免设备从 USB 上消失
#  - 所有设备路径自动探测, 不写死

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
: "${TSF_LOG_DIR:=$TSF_DIR}"
IMG="$TSF_IMG"
LOG="$TSF_LOG_DIR/tesla-gadget.log"
CONF_NAME="${TSF_GADGET_CONFIG:-c.1}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# --- 自动探测 ---
find_gadget() {
    if [ -n "$TSF_GADGET" ] && [ -d "$TSF_GADGET" ]; then echo "$TSF_GADGET"; return 0; fi
    for d in /sys/kernel/config/usb_gadget/*/; do
        [ -d "$d/configs" ] && { echo "${d%/}"; return 0; }
    done
    return 1
}

GADGET=$(find_gadget) || { echo "ERROR: 找不到 USB gadget (configfs 未挂载?)"; exit 1; }
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)
[ -z "$UDC" ] && { echo "ERROR: 没有 USB Device 控制器 (UDC)"; exit 1; }

# configfs 里的配置名可能不叫 c.1
[ -d "$GADGET/configs/$CONF_NAME" ] || CONF_NAME=$(ls "$GADGET/configs" 2>/dev/null | head -1)

FUNC_DIR="$GADGET/functions/mass_storage.0"
LINK="$GADGET/configs/$CONF_NAME/massstorage"

state() { cat "/sys/class/udc/$UDC/state" 2>/dev/null; }
bind()   { echo "$UDC" > "$GADGET/UDC"; }
unbind() { echo "" > "$GADGET/UDC"; }

status() {
    echo "gadget : $GADGET (config=$CONF_NAME)"
    echo "udc    : $UDC  state=$(state)"
    echo "lun    : $(cat "$FUNC_DIR/lun.0/file" 2>/dev/null || echo '未挂载')"
    echo "image  : $IMG ($(du -sh "$IMG" 2>/dev/null | cut -f1) 实际占用)"
    echo "--- gadget 功能 ---"
    ls "$GADGET/configs/$CONF_NAME/" 2>/dev/null | grep -v '^max_speed$'
}

do_on() {
    grep -qs " ${TSF_MNT:-/tmp/teslamnt} " /proc/mounts && { echo "BUSY: image mounted"; exit 75; }
    [ -f "$IMG" ] || { echo "ERROR: 镜像不存在: $IMG"; exit 1; }
    if [ -e "$FUNC_DIR" ] && [ "$(cat "$FUNC_DIR/lun.0/file" 2>/dev/null)" = "$IMG" ]; then
        echo "ALREADY-ON"; exit 0
    fi
    log "=== ON: exporting $IMG ==="
    unbind; sleep 1

    mkdir -p "$FUNC_DIR"
    echo 1 > "$FUNC_DIR/lun.0/removable"
    echo "$IMG" > "$FUNC_DIR/lun.0/file"
    # 链接已存在(含悬空链接)时不要再 ln, configfs 下会报 Operation not permitted
    if [ ! -L "$LINK" ] && [ ! -e "$LINK" ]; then ln -sf "$FUNC_DIR" "$LINK"; fi

    bind; sleep 3
    st=$(state)
    log "state after bind: $st"
    if [ "$st" != "configured" ] && [ "$st" != "addressed" ]; then
        log "BIND FAILED, rolling back"
        unbind 2>/dev/null
        rm -f "$LINK" 2>/dev/null
        rmdir "$FUNC_DIR" 2>/dev/null
        sleep 1
        bind; sleep 2
        echo "ERROR: 绑定失败已回滚, state=$(state)"; exit 1
    fi
    echo "TESLA-GADGET-ON (state=$st)"
}

do_off() {
    log "=== OFF: removing mass_storage ==="
    unbind; sleep 1
    rm -f "$LINK" 2>/dev/null
    rmdir "$FUNC_DIR" 2>/dev/null
    bind; sleep 2
    log "state: $(state)"
    echo "TESLA-GADGET-OFF (state=$(state))"
}

# 只重新绑定 UDC (镜像管理模式结束后用)
do_restore() {
    grep -qs " ${TSF_MNT:-/tmp/teslamnt} " /proc/mounts && exit 75
    bind; sleep 2
    log "restore bind, state=$(state)"
    echo "RESTORED (state=$(state))"
}

case "$1" in
    on)      do_on ;;
    off)     do_off ;;
    restore) do_restore ;;
    status)  status ;;
    *) echo "用法: $0 on|off|restore|status"; exit 2 ;;
esac
