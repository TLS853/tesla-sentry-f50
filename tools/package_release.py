#!/usr/bin/env python3
"""Create a small offline installer from already-built deployment artifacts."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import os

ROOT = Path(__file__).resolve().parent.parent
FILES = ("install.sh", "install-offline.sh", "uninstall.sh", "sentry-viewer.py",
         "tesla-live-sync.py", "version.json", "dist/release.json",
         "tools/merge_config.py", "tools/check_storage.py", "tools/verify_package.py",
         "tools/package_release.py", "tools/deploy_f50.sh", "README.md", "docs/DEPLOYMENT.md", "docs/FEATURES.md", "docs/UPDATES.md")


def package(root=ROOT, output=None):
    root = Path(root)
    version = json.loads((root / "version.json").read_text(encoding="utf-8"))
    number = version["version"]
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", number):
        raise ValueError("invalid package version")
    release = json.loads((root / "dist/release.json").read_text(encoding="utf-8"))
    if release["version"] != number or hashlib.sha256((root / "dist/release.json").read_bytes()).hexdigest() != version["sha256"]:
        raise ValueError("release artifacts are stale; run python3 tools/build.py first")
    for name, record in release["files"].items():
        local = root / name if name.endswith(".py") else root / "scripts" / name
        if hashlib.sha256(local.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("runtime differs from release bundle: " + name)
    files = sorted(set(FILES) | {p.relative_to(root).as_posix() for p in (root / "scripts").glob("*.sh")})
    payloads = {name: (root / name).read_bytes() for name in files}
    manifest = {"version": number, "files": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}}
    payloads["DEPLOYMENT.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output = Path(output) if output else root / "dist" / ("tesla-sentry-f50-" + number + "-offline.tar.gz")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".offline-", dir=str(output.parent))
    try:
        with os.fdopen(descriptor, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, data in sorted(payloads.items()):
                    entry = tarfile.TarInfo("tesla-sentry-f50-" + number + "/" + name)
                    entry.size = len(data)
                    entry.mode = 0o755 if name.endswith(".sh") else 0o644
                    archive.addfile(entry, io.BytesIO(data))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    output.with_suffix(output.suffix + ".sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n")
    return output.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(package(output=args.output))
