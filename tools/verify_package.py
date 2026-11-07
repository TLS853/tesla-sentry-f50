#!/usr/bin/env python3
"""Verify the offline package manifest and complete-release digest before installation."""
import hashlib
import json
from pathlib import Path
import sys


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "DEPLOYMENT.json").read_text(encoding="utf-8"))
    for relative, digest in manifest["files"].items():
        path = (root / relative).resolve()
        if not str(path).startswith(str(root) + "/") or not path.is_file():
            raise ValueError("安装包路径无效: " + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("安装包文件损坏: " + relative)
    version = json.loads((root / "version.json").read_text(encoding="utf-8"))
    if version["version"] != manifest["version"]:
        raise ValueError("安装包版本不一致")
    if hashlib.sha256((root / "dist/release.json").read_bytes()).hexdigest() != version["sha256"]:
        raise ValueError("程序包校验失败")
    return version["version"]


if __name__ == "__main__":
    try:
        print("安装包校验通过: " + verify(sys.argv[1]))
    except (ValueError, KeyError, OSError) as error:
        print("安装包校验失败: %s" % error, file=sys.stderr)
        sys.exit(1)
