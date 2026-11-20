#!/bin/sh
# Archive and write queued audio after the USB image has been idle.
set -a
[ ! -f "${TSF_CONF:-/etc/tesla-sentry.conf}" ] || . "${TSF_CONF:-/etc/tesla-sentry.conf}"
set +a

: "${TSF_DIR:=/opt/tesla-sentry}"
VIEWER="$TSF_DIR/sentry-viewer.py"
if [ ! -f "$VIEWER" ]; then
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    VIEWER="$SCRIPT_DIR/../sentry-viewer.py"
fi
PYTHON="${TSF_PYTHON:-python3}"
LOG="${TSF_LOG_DIR:-$TSF_DIR}/tesla-sync.log"

if [ -n "${TSF_USB_LOCK_FD:-}" ]; then
    exec "$PYTHON" "$VIEWER" --archive-sync >>"$LOG" 2>&1
fi
exec "$PYTHON" "$VIEWER" --usb-lock-exec "$PYTHON" "$VIEWER" --archive-sync >>"$LOG" 2>&1
