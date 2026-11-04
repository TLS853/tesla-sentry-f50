#!/bin/sh
# Tesla Sentry F50 - 一键安装脚本
# 用法: sudo ./install.sh [选项]
#   --size GB        虚拟U盘容量, 默认 170
#   --port PORT      网页端口, 默认 8080
#   --archive DIR    归档区路径, 默认 /archive
#   --dir DIR        安装目录, 默认 /opt/tesla-sentry
#   -h, --help       显示帮助
set -e

SIZE_GB=170
PORT=8080
ARCHIVE=/archive
TSF_DIR=/opt/tesla-sentry

while [ $# -gt 0 ]; do
    case "$1" in
        --size)    SIZE_GB="$2"; shift 2 ;;
        --port)    PORT="$2"; shift 2 ;;
        --archive) ARCHIVE="$2"; shift 2 ;;
        --dir)     TSF_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "未知参数: $1 (用 --help 查看)"; exit 1 ;;
    esac
done

IMG="$TSF_DIR/teslacam.img"
CONF=/etc/tesla-sentry.conf

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------- 0. 权限与环境 ----------------
[ "$(id -u)" = "0" ] || die "请用 root 运行 (sudo ./install.sh)"
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$SRC_DIR/sentry-viewer.py" ] || die "找不到 sentry-viewer.py, 请在项目根目录运行"

PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || die "需要 python3 (OpenWrt: opkg install python3-light)"

say "检查 USB Gadget 支持..."
GADGET=""
for d in /sys/kernel/config/usb_gadget/*/; do
    [ -d "$d/configs" ] && { GADGET="${d%/}"; break; }
done
[ -n "$GADGET" ] || die "未发现 USB gadget (configfs 未挂载或设备不支持 Device 模式)"
[ -n "$(ls /sys/class/udc/ 2>/dev/null | head -1)" ] || die "未发现 UDC 控制器"

if ! grep -qw exfat /proc/filesystems 2>/dev/null; then
    modprobe exfat 2>/dev/null || true
    grep -qw exfat /proc/filesystems 2>/dev/null || die "内核不支持 exFAT (需要 CONFIG_EXFAT_FS)"
fi

if [ -r /proc/config.gz ]; then
    zcat /proc/config.gz 2>/dev/null | grep -q "CONFIG_USB_F_MASS_STORAGE=y" \
        || echo "  [警告] 内核配置未见 CONFIG_USB_F_MASS_STORAGE, 仍会尝试 (见 README 硬件要求)"
fi

FREE_MB=$(df -Pm "$(dirname "$TSF_DIR")" 2>/dev/null | awk 'NR==2{print $4}')
[ -z "$FREE_MB" ] && FREE_MB="?"
say "存储空闲: ${FREE_MB}MB, 计划镜像 ${SIZE_GB}G (稀疏文件, 按实际写入占用)"

# ---------------- 1. 目录与文件 ----------------
say "安装到 $TSF_DIR ..."
mkdir -p "$TSF_DIR"
cp "$SRC_DIR/sentry-viewer.py"      "$TSF_DIR/sentry-viewer.py"
cp "$SRC_DIR/scripts/tesla-gadget.sh" "$TSF_DIR/tesla-gadget.sh"
cp "$SRC_DIR/scripts/tesla-sync.sh"   "$TSF_DIR/tesla-sync.sh"
cp "$SRC_DIR/scripts/usb-selfheal.sh" "$TSF_DIR/usb-selfheal.sh"
chmod +x "$TSF_DIR"/*.sh "$TSF_DIR/sentry-viewer.py"

mkdir -p "$ARCHIVE"/SentryClips "$ARCHIVE"/SavedClips "$ARCHIVE"/RecentClips \
         "$ARCHIVE"/.skins "$ARCHIVE"/boombox-queue "$ARCHIVE"/boombox-done "$ARCHIVE"/files

# ---------------- 2. 镜像 ----------------
if [ -f "$IMG" ]; then
    say "镜像已存在, 跳过创建: $IMG"
else
    say "创建 ${SIZE_GB}G 稀疏镜像 ..."
    dd if=/dev/zero of="$IMG" bs=1M count=1 seek=$((SIZE_GB * 1024 - 1)) conv=notrunc 2>/dev/null
    sync
fi
ls -lh "$IMG"

# ---------------- 3. 配置 ----------------
say "写入配置 $CONF ..."
cat > "$CONF" <<EOF
# Tesla Sentry F50 配置 (由 install.sh 生成)
TSF_DIR=$TSF_DIR
TSF_IMG=$IMG
TSF_ARCHIVE=$ARCHIVE
TSF_MNT=/tmp/teslamnt
TSF_LOOP=/dev/loop3
TSF_PORT=$PORT
TSF_IDLE=600
TSF_USB_TIMEOUT=300
TSF_LOSETUP=
TSF_GADGET=$GADGET
EOF

# ---------------- 4. 服务 ----------------
if [ -f /etc/rc.common ]; then
    say "安装 OpenWrt procd 服务 ..."
    cat > /etc/init.d/tesla-gadget <<'EOF'
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
    [ -f /etc/tesla-sentry.conf ] && . /etc/tesla-sentry.conf
    setsid /bin/sh "$TSF_DIR/tesla-gadget.sh" on </dev/null >/dev/null 2>&1 &
}
stop_service() {
    [ -f /etc/tesla-sentry.conf ] && . /etc/tesla-sentry.conf
    setsid /bin/sh "$TSF_DIR/tesla-gadget.sh" off </dev/null >/dev/null 2>&1 &
}
EOF
    cat > /etc/init.d/sentry-viewer <<EOF
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
    [ -f $CONF ] && . $CONF
    export TSF_DIR TSF_IMG TSF_ARCHIVE TSF_MNT TSF_LOOP TSF_PORT TSF_IDLE TSF_USB_TIMEOUT
    procd_open_instance
    procd_set_param command $PYTHON "\$TSF_DIR/sentry-viewer.py"
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF
    chmod +x /etc/init.d/tesla-gadget /etc/init.d/sentry-viewer
    /etc/init.d/tesla-gadget enable
    /etc/init.d/sentry-viewer enable

    # 归档定时任务 + U盘自愈(主机 eject 后自动重新导出)
    mkdir -p /etc/crontabs
    touch /etc/crontabs/root
    sed -i '/tesla-sync.sh/d;/usb-selfheal.sh/d' /etc/crontabs/root 2>/dev/null || true
    echo "*/10 * * * * /bin/sh $TSF_DIR/tesla-sync.sh >/dev/null 2>&1" >> /etc/crontabs/root
    echo "*/2 * * * * /bin/sh $TSF_DIR/usb-selfheal.sh >/dev/null 2>&1" >> /etc/crontabs/root
    /etc/init.d/cron enable 2>/dev/null || true
    /etc/init.d/cron restart 2>/dev/null || true

elif [ -d /etc/systemd/system ]; then
    say "安装 systemd 服务 ..."
    cat > /etc/systemd/system/tesla-gadget.service <<EOF
[Unit]
Description=Tesla Sentry virtual USB drive
After=local-fs.target
[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-$CONF
ExecStart=/bin/sh $TSF_DIR/tesla-gadget.sh on
ExecStop=/bin/sh $TSF_DIR/tesla-gadget.sh off
[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/sentry-viewer.service <<EOF
[Unit]
Description=Tesla Sentry web viewer
After=network.target tesla-gadget.service
[Service]
Type=simple
EnvironmentFile=-$CONF
ExecStart=$PYTHON $TSF_DIR/sentry-viewer.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
    mkdir -p /etc/cron.d
    cat > /etc/cron.d/tesla-sentry <<EOF
*/10 * * * * root /bin/sh $TSF_DIR/tesla-sync.sh >/dev/null 2>&1
*/2 * * * * root /bin/sh $TSF_DIR/usb-selfheal.sh >/dev/null 2>&1
EOF
    systemctl daemon-reload
    systemctl enable tesla-gadget.service sentry-viewer.service >/dev/null 2>&1
else
    die "未识别的 init 系统 (既没有 /etc/rc.common 也没有 systemd)"
fi

# ---------------- 5. 启动 ----------------
say "启动虚拟U盘 ..."
sh "$TSF_DIR/tesla-gadget.sh" on || echo "  [警告] 虚拟U盘启动失败, 见 $TSF_DIR/tesla-gadget.log"

say "启动查看器 ..."
if [ -f /etc/rc.common ]; then
    /etc/init.d/sentry-viewer restart >/dev/null 2>&1 || true
else
    systemctl restart sentry-viewer.service || true
fi
sleep 2

IP=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')
say "完成!"
echo
echo "  虚拟U盘镜像 : $IMG ($(du -sh "$IMG" 2>/dev/null | cut -f1) 实际占用)"
echo "  归档区      : $ARCHIVE"
echo "  查看器      : http://<设备IP>:$PORT"
[ -n "$IP" ] && echo "  当前网关    : $IP"
echo
echo "  下一步: 把设备插到车上 -> 车机提示格式化USB -> 确认 -> 哨兵正常录制"
echo "  状态检查: sh $TSF_DIR/tesla-gadget.sh status"
echo "  归档日志: $TSF_DIR/tesla-sync.log"
