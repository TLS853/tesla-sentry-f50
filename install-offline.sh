#!/bin/sh
# F50 不需要连接 GitHub; 使用包内已经构建好的程序。
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo "需要先安装 Python 3.7+" >&2; exit 1; }
python3 "$SCRIPT_DIR/tools/verify_package.py" "$SCRIPT_DIR"
exec /bin/sh "$SCRIPT_DIR/install.sh" --offline "$@"
