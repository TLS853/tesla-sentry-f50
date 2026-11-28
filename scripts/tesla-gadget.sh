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
UDC=$(cat "$GADGET/UDC") || exit 1
[ -n "$UDC" ] || UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)
[ -z "$UDC" ] && { echo "ERROR: 没有 USB Device 控制器 (UDC)"; exit 1; }

# configfs 里的配置名可能不叫 c.1
[ -d "$GADGET/configs/$CONF_NAME" ] || CONF_NAME=$(ls "$GADGET/configs" 2>/dev/null | head -1)

FUNC_DIR="$GADGET/functions/mass_storage.0"
LINK="$GADGET/configs/$CONF_NAME/massstorage"

state() { cat "/sys/class/udc/$UDC/state" 2>/dev/null; }
bind()   { echo "$UDC" > "$GADGET/UDC"; }
unbind() { echo "" > "$GADGET/UDC"; }

# Called only when a bind is already necessary. Keep a working export online.
bind_storage() {
    if [ "${TSF_USB2_COMPAT:-0}" != 1 ]; then bind; return; fi
    previous_speed=$(cat "$GADGET/max_speed") || { bind; return; }
    if [ "$previous_speed" = high-speed ]; then bind; return; fi
    if echo high-speed > "$GADGET/max_speed"; then
        if bind; then log "USB 2.0 compatibility enabled"; return 0; fi
        echo "$previous_speed" > "$GADGET/max_speed" || return 1
    fi
    log "USB 2.0 setting unavailable; using $previous_speed"
    bind
}

status() {
    echo "gadget : $GADGET (config=$CONF_NAME)"
    echo "udc    : $UDC  state=$(state)"
    echo "speed  : $(cat "/sys/class/udc/$UDC/current_speed" 2>/dev/null) (max=$(cat "$GADGET/max_speed" 2>/dev/null))"
    echo "lun    : $(cat "$FUNC_DIR/lun.0/file" 2>/dev/null || echo '未挂载')"
    echo "image  : $IMG ($(du -sh "$IMG" 2>/dev/null | cut -f1) 实际占用)"
    echo "--- gadget 功能 ---"
    ls "$GADGET/configs/$CONF_NAME/" 2>/dev/null | grep -v '^max_speed$'
}

do_on() {
    grep -qs " ${TSF_MNT:-/tmp/teslamnt} " /proc/mounts && { echo "BUSY: image mounted"; exit 75; }
    [ -f "$IMG" ] || { echo "ERROR: 镜像不存在: $IMG"; exit 1; }
    lun="$FUNC_DIR/lun.0/file"
    linked=false
    for candidate in "$GADGET/configs/$CONF_NAME/"*; do
        if [ -d "$FUNC_DIR" ] && [ -L "$candidate" ] && [ "$(readlink -f "$candidate")" = "$(readlink -f "$FUNC_DIR")" ]; then
            linked=true
            break
        fi
    done
    current=""
    if [ -d "$FUNC_DIR" ]; then current=$(cat "$lun") || return 1; fi
    if [ -n "$current" ] && [ "$(readlink -f "$current")" != "$(readlink -f "$IMG")" ]; then
        echo "ERROR: LUN already contains another image" >&2; return 1
    fi
    bound=$(cat "$GADGET/UDC") || return 1
    if "$linked"; then
        # Reloading an ejected medium does not require re-enumerating the composite device.
        if [ -z "$current" ]; then
            echo "$IMG" > "$lun" || return 1
            log "restored ejected storage medium"
        fi
        if [ -z "$bound" ]; then bind_storage || return 1; log "restored USB controller binding"; fi
    else
        if [ -e "$LINK" ] || [ -L "$LINK" ]; then
            echo "ERROR: storage link is already in use" >&2; return 1
        fi
        mkdir -p "$FUNC_DIR" || return 1
        if [ -z "$current" ]; then
            echo 1 > "$FUNC_DIR/lun.0/removable" && echo "$IMG" > "$lun" || return 1
        fi
        if [ -n "$bound" ]; then unbind || return 1; fi
        if ! ln -s "$FUNC_DIR" "$LINK"; then
            if [ -n "$bound" ]; then bind || log "failed to restore original USB binding"; fi
            return 1
        fi
        if ! bind_storage; then
            # Restore the prior configuration after an actual controller bind error.
            rm "$LINK" || return 1
            if [ -n "$bound" ]; then bind || log "failed to restore original USB binding"; fi
            return 1
        fi
        log "added USB storage function (state=$(state))"
    fi
    # A sleeping, disconnected or slow host can enumerate later.
    [ "$(cat "$GADGET/UDC")" = "$UDC" ] && [ "$(readlink -f "$(cat "$lun")")" = "$(readlink -f "$IMG")" ] || return 1
    echo "TESLA-GADGET-ON (state=$(state))"
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
