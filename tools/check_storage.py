#!/usr/bin/env python3
"""Reject a new image when its filesystem cannot hold the image and archive reserve."""
import os
from pathlib import Path
import sys


def check(image, size_gib):
    if not 1 <= size_gib <= 65536:
        raise ValueError("镜像容量必须为 1–65536 GiB")
    image = Path(image).resolve()
    if image.is_file():
        return "保留已有镜像: %s (不会调整容量或格式化)" % image
    ancestor = image.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    stat = os.statvfs(str(ancestor))
    available = stat.f_bavail * stat.f_frsize
    required = (size_gib + 20) * 1024 ** 3
    if available < required:
        raise ValueError("%s 所在文件系统仅剩 %.1f GiB; 新建 %d GiB 镜像需至少 %d GiB。"
                         "请用 --dir 和 --archive 指定大容量持久化存储, 或降低 --size。"
                         % (image.parent, available / 1024 ** 3, size_gib, size_gib + 20))
    return "新镜像空间检查通过: %.1f GiB 可用" % (available / 1024 ** 3)


if __name__ == "__main__":
    try:
        print(check(sys.argv[1], int(sys.argv[2])))
    except (ValueError, OSError) as error:
        print("空间检查失败: %s" % error, file=sys.stderr)
        sys.exit(1)
