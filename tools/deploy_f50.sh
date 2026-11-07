#!/bin/sh
# 在 Mac 上执行; 通过 SSH 上传离线包, 不使用设备端 GitHub、SFTP 或 SCP。
set -eu
if [ "$#" -lt 1 ]; then
    echo "用法: sh tools/deploy_f50.sh root@设备IP [安装选项]" >&2
    echo "例: sh tools/deploy_f50.sh root@192.168.0.1 --size 64 --port 8080" >&2
    exit 2
fi
DEST=$1
shift
case "$DEST" in
    ''|-*|*[!a-zA-Z0-9._@-]*) echo "请填写 user@IPv4地址 或 user@主机名" >&2; exit 2 ;;
esac
SSH_PORT=${SSH_PORT:-22}
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v python3 >/dev/null 2>&1 || { echo "Mac 需要 Python 3.7+" >&2; exit 1; }
command -v ssh >/dev/null 2>&1 || { echo "未找到 SSH 客户端" >&2; exit 1; }
PACKAGE=$(python3 "$ROOT/tools/package_release.py")
PREFIX=$(python3 -c 'import json,sys; print("tesla-sentry-f50-" + json.load(open(sys.argv[1]))["version"])' "$ROOT/version.json")
REMOTE_COMMAND=$(python3 - "$PREFIX" "$@" <<'PY'
import shlex
import sys
script = r'''
set -eu
prefix=$1
shift
command -v python3 >/dev/null 2>&1 || { echo "F50 缺少 Python 3.7+" >&2; exit 1; }
command -v setsid >/dev/null 2>&1 || { echo "F50 缺少 setsid" >&2; exit 1; }
umask 077
stage=$(mktemp -d /tmp/tesla-sentry-deploy.XXXXXX)
cat > "$stage/package.tar.gz"
tar -xzf "$stage/package.tar.gz" -C "$stage"
cd "$stage/$prefix"
setsid sh ./install-offline.sh "$@" > "$stage/install.log" 2>&1 < /dev/null &
echo "安装包已上传, 已启动后台离线安装。请检查日志确认最终结果。"
echo "INSTALL_LOG=$stage/install.log"
echo "查看进度: tail -n 80 $stage/install.log"
'''
print("sh -c " + shlex.quote(script) + " sh " + " ".join(shlex.quote(value) for value in sys.argv[1:]))
PY
)
echo "正在上传并启动离线安装: $DEST"
ssh -p "$SSH_PORT" "$DEST" "$REMOTE_COMMAND" < "$PACKAGE"
echo "重新连接设备查看上方 INSTALL_LOG; 默认网页端口为 8080。"
