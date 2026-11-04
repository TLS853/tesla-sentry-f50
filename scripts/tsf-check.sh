#!/bin/sh
# tsf-check.sh - 部署前自检: 判断这台设备能不能跑 Tesla Sentry F50
# 用法: sh tsf-check.sh            # 在本机执行 (最好 root)
#      sh tsf-check.sh --json      # 机器可读输出
#
# 检查的是"硬门槛"(缺了完全跑不起来) 与"软能力"(缺了只是功能降级), 最后给结论。

JSON=0
[ "$1" = "--json" ] && JSON=1

FAIL=0; WARN=0
ok()   { [ "$JSON" = "1" ] && echo "{\"item\":\"$1\",\"level\":\"ok\",\"detail\":\"$2\"}" || printf '  \033[32m✓\033[0m %-22s %s\n' "$1" "$2"; }
warn() { WARN=$((WARN+1)); [ "$JSON" = "1" ] && echo "{\"item\":\"$1\",\"level\":\"warn\",\"detail\":\"$2\"}" || printf '  \033[33m!\033[0m %-22s %s\n' "$1" "$2"; }
bad()  { FAIL=$((FAIL+1)); [ "$JSON" = "1" ] && echo "{\"item\":\"$1\",\"level\":\"fail\",\"detail\":\"$2\"}" || printf '  \033[31m✗\033[0m %-22s %s\n' "$1" "$2"; }
sec()  { [ "$JSON" = "1" ] || printf '\n\033[1m%s\033[0m\n' "$1"; }

[ "$JSON" = "1" ] || echo "Tesla Sentry F50 部署自检  ($(date '+%Y-%m-%d %H:%M:%S'))"

# ---------- 1. 平台 ----------
sec "1. 平台"
if uname -s 2>/dev/null | grep -qi linux; then
    ok "操作系统" "$(uname -s) $(uname -r) $(uname -m)"
else
    bad "操作系统" "$(uname -s) — 必须是 Linux (Windows/macOS 无法做 USB 设备)"
fi

[ "$(id -u)" = "0" ] && ok "权限" "root" || warn "权限" "非 root — 挂载镜像/操作 configfs 会失败, 请用 root 运行"

if [ -r /proc/meminfo ]; then
    MEM=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
    [ "$MEM" -ge 512 ] && ok "内存" "${MEM}MB" || warn "内存" "${MEM}MB — 建议 ≥512MB, 缩略图/遥测解码会吃紧"
fi
CORES=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo ?)
ok "CPU 核心" "$CORES"

# ---------- 2. USB Device (最关键) ----------
sec "2. USB Device / Gadget (硬门槛)"
if [ -d /sys/kernel/config/usb_gadget ]; then
    ok "configfs gadget" "已挂载"
elif [ -d /sys/kernel/config ]; then
    mount -t configfs none /sys/kernel/config 2>/dev/null && ok "configfs gadget" "已手动挂载" || bad "configfs gadget" "未挂载且挂载失败 (CONFIG_CONFIGFS_FS?)"
else
    bad "configfs gadget" "无 configfs — 内核缺少 USB Gadget 支持"
fi

UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)
if [ -n "$UDC" ]; then
    ok "UDC 控制器" "$UDC (state=$(cat /sys/class/udc/$UDC/state 2>/dev/null))"
else
    bad "UDC 控制器" "没有 /sys/class/udc/* — 该 USB 口只能当主机(Host), 不能当「U盘」被车机识别"
fi

if grep -qw mass_storage /proc/modules 2>/dev/null \
   || ls /sys/module/usb_f_mass_storage >/dev/null 2>&1 \
   || ls /sys/kernel/config/usb_gadget/*/functions/ 2>/dev/null | grep -q mass_storage \
   || ls /sys/kernel/config/usb_gadget/*/configs/*/ 2>/dev/null | grep -q massstorage; then
    ok "mass_storage 功能" "可用"
else
    modprobe usb_f_mass_storage 2>/dev/null && ok "mass_storage 功能" "已加载 (本次)" || \
    if [ -d /sys/kernel/config/usb_gadget ]; then
        warn "mass_storage 功能" "未加载, 可能是内建 (=y) 或缺失; 建 gadget 时才会暴露"
    else
        bad "mass_storage 功能" "缺失"
    fi
fi

if [ -z "$UDC" ]; then
    warn "线缆/端口" "插上数据线后 /sys/class/udc 才有内容; 部分设备需在 DTS/BIOS 里切 peripheral 模式"
fi

# ---------- 3. 文件系统与存储 ----------
sec "3. 文件系统与存储"
if grep -qw exfat /proc/filesystems 2>/dev/null; then
    ok "exFAT (内核)" "支持"
else
    modprobe exfat 2>/dev/null
    grep -qw exfat /proc/filesystems 2>/dev/null && ok "exFAT (内核)" "modprobe 后可用" || \
        bad "exFAT (内核)" "不支持 — 需 CONFIG_EXFAT_FS (Linux 5.7+) 或安装 exfat-linux 驱动"
fi

if [ -e /dev/loop-control ] || [ -b /dev/loop0 ]; then
    ok "loop 设备" "可用"
else
    warn "loop 设备" "未发现 — 归档/U盘管理需要 loop; 可 modprobe loop"
fi

LOSETUP_BIN=""
for c in "$(command -v losetup 2>/dev/null)" /sbin/losetup /usr/sbin/losetup /opt/mu300/busybox-bin/losetup; do
    [ -n "$c" ] && [ -x "$c" ] && { LOSETUP_BIN="$c"; break; }
done
if [ -n "$LOSETUP_BIN" ]; then
    ok "mount/losetup" "$LOSETUP_BIN"
elif command -v mount >/dev/null 2>&1; then
    warn "mount/losetup" "有 mount 但没找到 losetup — 归档与U盘管理需要它 (util-linux 或 busybox) "
else
    bad "mount/losetup" "缺失"
fi

AVAIL=$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$AVAIL" ]; then
    [ "$AVAIL" -ge 20480 ] && ok "根分区可用空间" "${AVAIL}MB" || warn "根分区可用空间" "${AVAIL}MB — 镜像与归档建议 ≥20GB"
fi

# ---------- 4. 运行时 ----------
sec "4. 运行时依赖"
if command -v python3 >/dev/null 2>&1; then
    PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
    PYOK=$(python3 -c 'import sys;print(1 if sys.version_info>=(3,7) else 0)' 2>/dev/null)
    if [ "$PYOK" = "1" ]; then ok "python3" "$PYV"
    else bad "python3" "$PYV — 需 3.7+ (http.server.ThreadingHTTPServer)"; fi
    python3 -c 'import http.server,json,zipfile,struct,threading' 2>/dev/null && ok "标准库" "齐全 (零第三方依赖)" || bad "标准库" "缺模块, 可能被裁剪过"
else
    bad "python3" "未安装"
fi

if command -v ffmpeg >/dev/null 2>&1; then
    ok "ffmpeg" "$(ffmpeg -version 2>/dev/null | head -1 | cut -c1-40)"
elif [ -x /opt/mu300/ffmpeg/ffmpeg ]; then
    ok "ffmpeg" "/opt/mu300/ffmpeg/ffmpeg"
else
    warn "ffmpeg" "未安装 — 列表缩略图缺失, 其它功能正常"
fi

if [ -f /etc/rc.common ] && [ -x /sbin/procd ]; then
    ok "init 系统" "OpenWrt procd (install.sh 直接支持)"
elif [ -d /etc/systemd/system ]; then
    ok "init 系统" "systemd (install.sh 直接支持)"
elif [ -x /sbin/openrc ] || command -v rc-service >/dev/null 2>&1; then
    warn "init 系统" "OpenRC — install.sh 走通用 rc.local 分支"
elif [ -x /sbin/init ] && ls -l /sbin/init 2>/dev/null | grep -q busybox; then
    warn "init 系统" "BusyBox init — 走通用 rc.local 分支"
else
    warn "init 系统" "未识别 — 走通用 rc.local 分支, 可手动调 tsf-start.sh"
fi

if [ -d /etc/crontabs ] || [ -f /etc/crontab ] || command -v systemctl >/dev/null 2>&1; then
    ok "定时任务" "可用 (归档/U盘自愈依赖)"
else
    warn "定时任务" "无 cron — 归档需手动跑 tesla-sync.sh"
fi

# ---------- 5. 结论 ----------
if [ "$JSON" != "1" ]; then
    echo
    if [ "$FAIL" -gt 0 ]; then
        printf '\033[31m结论: 不满足完整部署条件 (%d 项硬门槛未过)\033[0m\n' "$FAIL"
        echo "  → 缺 UDC 时仍可「只跑网页查看器」: 把归档目录放到本机, 用 TSF_ARCHIVE 指向它,"
        echo "    再用别的设备(或有 gadget 能力的小盒子)负责虚拟U盘与归档。"
    elif [ "$WARN" -gt 0 ]; then
        printf '\033[33m结论: 可部署, 但有 %d 项降级/需注意\033[0m\n' "$WARN"
    else
        printf '\033[32m结论: 完全满足, 直接 sudo ./install.sh\033[0m\n'
    fi
    echo
    if [ -z "$UDC" ]; then
        echo "  UDC 为空但你有把握该口支持 Device 模式时: 插上电脑再看一遍本脚本;"
        echo "  个别设备需要在 DTS 里把 usb 控制器设为 peripheral/otg 并启用 dwc2/dwc3。"
    fi
fi
exit 0
