#!/usr/bin/env python3
"""发版助手: 改版本号 + 更新说明 + 同步 version.json

用法:
    python3 tools/make_release.py 2.2.0 "新增 XXX" "修复 YYY"

做的事:
  1) 把 sentry-viewer.py 里的 __version__ 改成新版本号
  2) 重写 version.json (版本 / 日期 / 说明列表)
  3) py_compile 检查, 提示下一步 git commit + push

设备上的「设置 → 更新」会拉 version.json 比对版本号, 所以提交推送后
用户点一下就能升级, 无需重新刷固件或拷文件。
"""
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "src", "tesla_sentry", "__init__.py")
VER = os.path.join(ROOT, "version.json")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    ver = sys.argv[1].strip().lstrip("v")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", ver):
        print("版本号格式不对, 例: 2.2.0")
        return 1
    notes = [a.strip() for a in sys.argv[2:] if a.strip()]
    src = open(APP, encoding="utf-8").read()
    new_src, n = re.subn(r'^(__version__\s*=\s*)"[^"]+"', r'\1"%s"' % ver, src, count=1, flags=re.M)
    if not n:
        print("没找到 __version__ 定义")
        return 1
    open(APP, "w", encoding="utf-8", newline="\n").write(new_src)
    data = {
        "version": ver,
        "date": datetime.date.today().isoformat(),
        "notes": notes or ["常规更新"],
    }
    with open(VER, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    import subprocess
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py")], check=True)
    print("版本已更新到 v%s" % ver)
    for x in data["notes"]:
        print("  - " + x)
    print("\n下一步: git add -A && git commit -m 'release: v%s' && git push" % ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
