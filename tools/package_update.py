#!/usr/bin/env python3
"""Build a deterministic update ZIP for a direct R2/public/signed download URL."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from tesla_sentry import update_package
from tesla_sentry.storage import atomic_write


def package(root=ROOT, output=None):
    root = Path(root)
    artifacts = runpy.run_path(str(root / "tools/build.py"))["outputs"]()
    for name, expected in artifacts.items():
        if not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != expected:
            raise ValueError("源码与构建产物不一致, 请先运行 python3 tools/build.py")
    metadata = json.loads((root / "version.json").read_text(encoding="utf-8"))
    raw = (root / "dist/release.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata.get("sha256"):
        raise ValueError("程序包已过期, 请先运行 python3 tools/build.py")
    release = json.loads(raw.decode("utf-8"))
    for name, record in release["files"].items():
        source = root / name if name.endswith(".py") else root / "scripts" / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("运行文件与构建产物不一致: " + name)
    data = update_package.make_zip(release, metadata)
    output = Path(output) if output else root / "dist" / ("tesla-sentry-f50-" + metadata["version"] + "-update.zip")
    atomic_write(str(output), data, mode=0o644)
    checksum = hashlib.sha256(data).hexdigest()
    atomic_write(str(output) + ".sha256", (checksum + "  " + output.name + "\n").encode(), mode=0o644)
    return output.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(package(output=args.output))
