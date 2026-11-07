#!/bin/sh
# Tesla Sentry F50 - 一键安装脚本
# 用法: sudo ./install.sh [选项]
#   --size GB        虚拟U盘容量, 默认 64
#   --port PORT      网页端口, 默认 8080
#   --archive DIR    归档区路径, 默认 /archive
#   --dir DIR        安装目录, 默认 /opt/tesla-sentry
#   --offline        不下载字体, 关闭在线检查更新
#   -h, --help       显示帮助
set -e

SIZE_GB=64
OFFLINE=0
PORT=8080
ARCHIVE=/archive
TSF_DIR=/opt/tesla-sentry
CONF=/etc/tesla-sentry.conf
# Reinstall preserves configured paths and optional policy settings; flags still override paths.
set -a
[ ! -f "$CONF" ] || . "$CONF"
set +a
PORT=${TSF_PORT:-$PORT}
ARCHIVE=${TSF_ARCHIVE:-$ARCHIVE}
OTA_ENABLED=${TSF_OTA_ENABLED:-1}

while [ $# -gt 0 ]; do
    case "$1" in
        --size)    SIZE_GB="$2"; shift 2 ;;
        --port)    PORT="$2"; shift 2 ;;
        --archive) ARCHIVE="$2"; shift 2 ;;
        --dir)     TSF_DIR="$2"; shift 2 ;;
        --offline) OFFLINE=1; OTA_ENABLED=0; shift ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "未知参数: $1 (用 --help 查看)"; exit 1 ;;
    esac
done

IMG="${TSF_IMG:-$TSF_DIR/teslacam.img}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------- 0. 权限与环境 ----------------
[ "$(id -u)" = "0" ] || die "请用 root 运行 (sudo ./install.sh)"
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$SRC_DIR/sentry-viewer.py" ] || die "找不到 sentry-viewer.py, 请在项目根目录运行"

PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || die "需要 python3 (OpenWrt: opkg install python3-light)"
"$PYTHON" "$SRC_DIR/sentry-viewer.py" --ota-check || die "Python 依赖不完整, 请先补齐自检提示的模块"

say "检查 USB Gadget 支持..."
GADGET=""
for d in /sys/kernel/config/usb_gadget/*/; do
    [ -d "$d/configs" ] && { GADGET="${d%/}"; break; }
done
[ -n "$GADGET" ] || die "未发现 USB gadget (configfs 未挂载或设备不支持 Device 模式)"
[ -n "$(ls /sys/class/udc/ 2>/dev/null | head -1)" ] || die "未发现 UDC 控制器"

CUR_EXPORT=""
for f in "$GADGET"/functions/mass_storage*/lun.0/file; do
    [ -f "$f" ] || continue
    CUR_EXPORT=$(cat "$f" 2>/dev/null)
    [ -n "$CUR_EXPORT" ] && break
done
if [ -n "$CUR_EXPORT" ] && [ -f "$CUR_EXPORT" ]; then
    IMG="$CUR_EXPORT"
    say "检测到已在导出的镜像, 沿用它: $IMG"
fi


"$PYTHON" "$SRC_DIR/tools/check_storage.py" "$IMG" "$SIZE_GB" || die "存储位置不合适, 安装已停止"

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
say "存储空闲: ${FREE_MB}MB, 新镜像默认 ${SIZE_GB}G (已有镜像保留原容量)"

# 新镜像已由 check_storage.py 检查完整容量及 20GiB 归档余量。

# 缓存目录 (缩略图/遥测/合成临时文件): F50 沿用 /opt/mu300, 其它设备放安装目录下
if [ -d /opt/mu300 ]; then CACHE_DIR=/opt/mu300; else CACHE_DIR="$TSF_DIR/cache"; fi
mkdir -p "$CACHE_DIR/thumbs" "$CACHE_DIR/tel-cache" 2>/dev/null || true

# ffmpeg 探测: 缩略图与部分导出功能依赖, 缺了只是没有缩略图
FFMPEG="$(command -v ffmpeg 2>/dev/null || echo /opt/mu300/ffmpeg/ffmpeg)"
FFPROBE="$(command -v ffprobe 2>/dev/null || echo /opt/mu300/ffmpeg/ffprobe)"
if [ -x "$FFMPEG" ]; then say "ffmpeg: $FFMPEG"
else echo "  [提示] 未找到 ffmpeg, 列表缩略图会缺失 (装一个即可: opkg/apt install ffmpeg)"; fi

# ---------------- 1. 目录与文件 ----------------
say "安装到 $TSF_DIR ..."
mkdir -p "$TSF_DIR"
TSF_DIR="$TSF_DIR" "$PYTHON" "$SRC_DIR/sentry-viewer.py" --install-release "$SRC_DIR/dist/release.json"

mkdir -p "$ARCHIVE"/SentryClips "$ARCHIVE"/SavedClips "$ARCHIVE"/RecentClips \
         "$ARCHIVE"/.skins "$ARCHIVE"/boombox-queue "$ARCHIVE"/boombox-done "$ARCHIVE"/files

# ---------------- 2. 镜像 ----------------
# 已经在跑的设备: 复用当前正在导出的镜像, 避免重装后车机看到一块空盘

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
CONF_TMP=$(mktemp "$CONF.XXXXXX")
trap 'rm -f "$CONF_TMP"' EXIT
cat > "$CONF_TMP" <<EOF
# Tesla Sentry F50 配置 (由 install.sh 生成)
TSF_DIR=$TSF_DIR
TSF_IMG=$IMG
TSF_ARCHIVE=$ARCHIVE
TSF_MNT=/tmp/teslamnt
TSF_LOOP=/dev/loop3
TSF_PORT=$PORT
TSF_IDLE=600
TSF_USB_TIMEOUT=300
TSF_RECENT_ROLL_MIN=60
TSF_BOX_KEEP_DAYS=14
TSF_BOX_MAX_GB=12
TSF_OTA_REPO=TLS853/tesla-sentry-f50
TSF_OTA_REF=codex/modular-runtime
TSF_OTA_ENABLED=$OTA_ENABLED
TSF_LOSETUP=
TSF_GADGET=$GADGET
TSF_CACHE=$CACHE_DIR
TSF_FFMPEG=$FFMPEG
TSF_FFPROBE=$FFPROBE
EOF
"$PYTHON" "$SRC_DIR/tools/merge_config.py" "$CONF" "$CONF_TMP"
trap - EXIT

# ---------------- 3.5 访问认证 ----------------
AUTH_CONF="$TSF_DIR/auth.conf"
if [ -f "$AUTH_CONF" ]; then
    say "认证配置已存在, 保持不动: $AUTH_CONF"
else
    AUTH_PASS=$(head -c 96 /dev/urandom | tr -dc 'a-z2-9' | head -c 10)
    cat > "$AUTH_CONF" <<EOF
# 网页登录凭据 (改完执行 /etc/init.d/sentry-viewer restart 生效)
# 删掉本文件 = 关闭认证, 只有内网使用时才建议这么干
user=admin
pass=$AUTH_PASS
ttl=2592000
EOF
    chmod 600 "$AUTH_CONF"
    say "已生成网页登录账号 -> admin / $AUTH_PASS   (已写入 $AUTH_CONF, 请记好)"
fi

# ---------------- 4. 服务 ----------------
if [ -f /etc/rc.common ]; then
    say "安装 OpenWrt procd 服务 ..."
    cat > /etc/init.d/tesla-gadget <<'EOF'
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
    set -a
    [ -f /etc/tesla-sentry.conf ] && . /etc/tesla-sentry.conf
    set +a
    setsid /bin/sh "$TSF_DIR/tesla-gadget.sh" on </dev/null >/dev/null 2>&1 &
}
stop_service() {
    set -a
    [ -f /etc/tesla-sentry.conf ] && . /etc/tesla-sentry.conf
    set +a
    setsid /bin/sh "$TSF_DIR/tesla-gadget.sh" off </dev/null >/dev/null 2>&1 &
}
EOF
    cat > /etc/init.d/sentry-viewer <<EOF
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
    set -a
    [ -f $CONF ] && . $CONF
    set +a
    export TSF_DIR TSF_IMG TSF_ARCHIVE TSF_MNT TSF_LOOP TSF_PORT TSF_IDLE TSF_USB_TIMEOUT
    export TSF_GADGET TSF_LOSETUP TSF_CACHE TSF_FFMPEG TSF_FFPROBE
    procd_open_instance
    procd_set_param command /bin/sh -c 'set -a; [ ! -f "\$1" ] || . "\$1"; exec "\$2" "\$3"' sh "$CONF" "$PYTHON" "\$TSF_DIR/sentry-viewer.py"
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF
    chmod +x /etc/init.d/tesla-gadget /etc/init.d/sentry-viewer
    /etc/init.d/tesla-gadget enable
    /etc/init.d/sentry-viewer enable

    # 车机在线只读同步 (不挂载镜像, 车机无感, 新片段秒级进网页)
    cat > /etc/init.d/tesla-live <<EOF
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
    set -a
    [ -f $CONF ] && . $CONF
    set +a
    export TSF_DIR TSF_IMG TSF_ARCHIVE TSF_MNT TSF_LIVE_INTERVAL
    procd_open_instance
    procd_set_param command /bin/sh -c 'set -a; [ ! -f "\$1" ] || . "\$1"; exec "\$2" "\$3"' sh "$CONF" "$PYTHON" "\$TSF_DIR/tesla-live-sync.py"
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF
    chmod +x /etc/init.d/tesla-live
    /etc/init.d/tesla-live enable

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
    cat > /etc/systemd/system/tesla-live.service <<EOF
[Unit]
Description=Tesla Sentry live read-only sync (no mount)
After=tesla-gadget.service
[Service]
Type=simple
EnvironmentFile=-$CONF
ExecStart=$PYTHON $TSF_DIR/tesla-live-sync.py
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
    systemctl enable tesla-gadget.service sentry-viewer.service tesla-live.service >/dev/null 2>&1
else
    # 通用分支: OpenRC / BusyBox init / runit / s6 等, 用 rc.local 拉起 + crontab 定时
    say "未识别的 init 系统, 安装为通用启动脚本 (rc.local + crontab) ..."
    cat > "$TSF_DIR/tsf-start.sh" <<EOF
#!/bin/sh
# 通用启动入口 (rc.local / 手动 / supervisor 均可调用)
set -a
    [ -f $CONF ] && . $CONF
    set +a
export TSF_DIR TSF_IMG TSF_ARCHIVE TSF_MNT TSF_LOOP TSF_PORT TSF_GADGET TSF_LOSETUP TSF_CACHE TSF_FFMPEG TSF_FFPROBE
sh "\$TSF_DIR/tesla-gadget.sh" on >/dev/null 2>&1
pgrep -f tesla-live-sync.py >/dev/null 2>&1 || setsid $PYTHON "\$TSF_DIR/tesla-live-sync.py" </dev/null >>"\$TSF_DIR/tesla-live.log" 2>&1 &
sleep 1
pgrep -f sentry-viewer.py >/dev/null 2>&1 || setsid $PYTHON "\$TSF_DIR/sentry-viewer.py" </dev/null >>"\$TSF_DIR/sentry-viewer.log" 2>&1 &
EOF
    chmod +x "$TSF_DIR/tsf-start.sh"

    # rc.local
    if [ -f /etc/rc.local ]; then
        grep -q 'tsf-start.sh' /etc/rc.local || \
            sed -i "/^exit 0/i $TSF_DIR/tsf-start.sh" /etc/rc.local
    else
        cat > /etc/rc.local <<EOF
#!/bin/sh -e
$TSF_DIR/tsf-start.sh
exit 0
EOF
        chmod +x /etc/rc.local
    fi

    # 定时任务: BusyBox crontab (/etc/crontabs/root) 优先, 否则系统 /etc/crontab
    if [ -d /etc/crontabs ]; then
        CRONF=/etc/crontabs/root
        touch "$CRONF"
        sed -i '/tesla-sync.sh/d;/usb-selfheal.sh/d' "$CRONF" 2>/dev/null || true
        echo "*/10 * * * * /bin/sh $TSF_DIR/tesla-sync.sh >/dev/null 2>&1" >> "$CRONF"
        echo "*/2 * * * * /bin/sh $TSF_DIR/usb-selfheal.sh >/dev/null 2>&1" >> "$CRONF"
        (rc-service cron restart || /etc/init.d/cron restart) >/dev/null 2>&1 || true
    elif [ -f /etc/crontab ]; then
        sed -i '/tesla-sync.sh/d;/usb-selfheal.sh/d' /etc/crontab 2>/dev/null || true
        echo "*/10 * * * * root /bin/sh $TSF_DIR/tesla-sync.sh >/dev/null 2>&1" >> /etc/crontab
        echo "*/2 * * * * root /bin/sh $TSF_DIR/usb-selfheal.sh >/dev/null 2>&1" >> /etc/crontab
        (rc-service crond restart || service cron restart) >/dev/null 2>&1 || true
    else
        echo "  [提示] 没有找到 cron, 请自行把下面两行加到定时任务:"
        echo "    */10 * * * * /bin/sh $TSF_DIR/tesla-sync.sh"
        echo "    */2  * * * * /bin/sh $TSF_DIR/usb-selfheal.sh"
    fi
    echo "  [提示] 已写入 /etc/rc.local 与 crontab; 若系统用 OpenRC/runit," \
         "可把 $TSF_DIR/tsf-start.sh 包一层 service 以获得开机自启与守护"
fi

# ---------------- 5. 启动 ----------------
say "启动虚拟U盘 ..."
sh "$TSF_DIR/tesla-gadget.sh" on || echo "  [警告] 虚拟U盘启动失败, 见 $TSF_DIR/tesla-gadget.log"

# 中文字体: 飞书推送视频烧录行车信息水印时由 libass 使用; 缺失只影响水印, 不影响其它功能
if [ "$OFFLINE" = 1 ]; then
    say "离线模式: 跳过字体下载, 在线检查更新已关闭"
elif ! ls "$TSF_DIR"/fonts/*.ttf >/dev/null 2>&1; then
    say "下载中文字体 (水印用, 约 17MB) ..."
    mkdir -p "$TSF_DIR/fonts"
    curl -sL --max-time 600 -o "$TSF_DIR/fonts/NotoSansSC.ttf" -C - \
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf" \
        || echo "  [提示] 字体下载失败, 不影响使用; 需要水印时可手动补: 见 README"
fi

say "启动查看器 ..."
if [ -f /etc/rc.common ]; then
    /etc/init.d/sentry-viewer restart >/dev/null 2>&1 || true
elif [ -d /etc/systemd/system ]; then
    systemctl restart sentry-viewer.service || true
fi

say "启动实时同步 (车机无感读取) ..."
if [ -f /etc/rc.common ]; then
    /etc/init.d/tesla-live restart >/dev/null 2>&1 || true
elif [ -d /etc/systemd/system ]; then
    systemctl restart tesla-live.service || true
else
    sh "$TSF_DIR/tsf-start.sh"    # 通用分支: 一次拉起全部
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
