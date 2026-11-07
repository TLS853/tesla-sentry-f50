#!/usr/bin/env python3
"""Merge installer-selected paths with existing optional settings atomically."""
from pathlib import Path
import os
import re
import shlex
import sys

SELECTED = {"TSF_DIR", "TSF_IMG", "TSF_ARCHIVE", "TSF_PORT", "TSF_GADGET", "TSF_CACHE",
            "TSF_FFMPEG", "TSF_FFPROBE", "TSF_OTA_ENABLED"}


def merge(existing, defaults):
    def key(line):
        match = re.match(r"(?:export\s+)?(TSF_\w+)=", line)
        return match.group(1) if match else None
    new = {key(line): key(line) + "=" + shlex.quote(line.split("=", 1)[1])
           for line in defaults.splitlines() if key(line)}
    seen = set()
    out = []
    for line in existing.splitlines():
        name = key(line)
        if name:
            seen.add(name)
            if name in SELECTED:
                line = new[name]
        out.append(line)
    out.extend(line for name, line in new.items() if name not in seen)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    target, staged = map(Path, sys.argv[1:])
    if target.exists():
        staged.write_text(merge(target.read_text(), staged.read_text()), encoding="utf-8")
    os.replace(str(staged), str(target))
