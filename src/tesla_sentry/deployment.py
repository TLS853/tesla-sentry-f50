"""Validate complete releases, then activate them with one symlink replacement."""

from . import config

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from . import locking
from . import storage
from .storage import atomic_json
from .update_package import RUNTIME_FILES


def _release_check(path, version):
    """No hardware access: compile and import both Python entry points."""
    for name in RUNTIME_FILES:
        fp = os.path.join(path, name)
        if name.endswith(".py"):
            with open(fp, encoding="utf-8") as f:
                compile(f.read(), fp, "exec")
            result = subprocess.run([sys.executable, fp, "--ota-check"],
                                    capture_output=True, timeout=90)
            if result.returncode or result.stdout.strip() != ("ok " + version).encode():
                raise ValueError("release self-check failed: " + name)
        else:
            subprocess.run(["/bin/sh", "-n", fp], check=True, capture_output=True)


def _switch_release(root, target):
    # target is a directory created and validated beneath our release store.
    root = os.path.realpath(root)
    real = os.path.realpath(target)
    if not real.startswith(os.path.realpath(os.path.join(root, "releases")) + os.sep):
        raise ValueError("release outside release store")
    fd, tmp = tempfile.mkstemp(prefix=".current-", dir=root)
    os.close(fd)
    os.unlink(tmp)
    try:
        os.symlink(os.path.relpath(real, root), tmp)
        os.replace(tmp, os.path.join(root, "current"))
        dfd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def install_release(data, root, expected_version=None, legacy_viewer_backup=None):
    root = os.path.realpath(root)
    package = json.loads(data.decode("utf-8"))
    if package.get("format") != 1 or set(package.get("files", {})) != set(RUNTIME_FILES):
        raise ValueError("invalid release file list")
    version = package.get("version", "")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", version) or (expected_version and version != expected_version):
        raise ValueError("release version mismatch")
    os.makedirs(os.path.join(root, "releases"), exist_ok=True)
    with locking.ProcessLock(os.path.join(root, ".update.lock")):
        stage = tempfile.mkdtemp(prefix="release-", dir=os.path.join(root, "releases"))
        activated = False
        try:
            for name in RUNTIME_FILES:
                record = package["files"][name]
                content = record["content"].encode("utf-8")
                if hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise ValueError("release checksum mismatch: " + name)
                with open(os.path.join(stage, name), "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(os.path.join(stage, name), 0o755)
            # Bind the package version to its executable, not only to JSON metadata.
            with open(os.path.join(stage, "sentry-viewer.py"), encoding="utf-8") as f:
                executable = f.read()
            if not re.search(r'^APP_VERSION = "' + re.escape(version) + r'"$', executable, re.M):
                raise ValueError("executable version mismatch")
            _release_check(stage, version)
            storage.sync_directory(stage)
            storage.sync_directory(os.path.join(root, "releases"))
            with locking.usb_exclusive():
                if os.path.ismount(config.get("TSF_MNT", "/tmp/teslamnt")):
                    raise RuntimeError("USB image is still mounted")
                current = os.path.join(root, "current")
                previous = os.path.realpath(current) if os.path.islink(current) else None
                # Snapshot legacy flat installs before replacing their entry points.
                if previous is None and all(os.path.isfile(os.path.join(root, n)) for n in RUNTIME_FILES):
                    previous = tempfile.mkdtemp(prefix="legacy-", dir=os.path.join(root, "releases"))
                    for name in RUNTIME_FILES:
                        old = os.path.join(root, name)
                        if name == "sentry-viewer.py" and legacy_viewer_backup and os.path.isfile(legacy_viewer_backup):
                            old = legacy_viewer_backup
                        shutil.copy2(old, os.path.join(previous, name))
                    # Keep the legacy version active throughout entry-point migration.
                    _switch_release(root, previous)
                atomic_json(os.path.join(root, "previous-release.json"), {"path": previous})
                # After initial migration these links remain stable across upgrades.
                for name in RUNTIME_FILES:
                    entry = os.path.join(root, name)
                    if os.path.islink(entry) and os.readlink(entry) == "current/" + name:
                        continue
                    fd, tmp = tempfile.mkstemp(prefix=".entry-", dir=root)
                    os.close(fd)
                    os.unlink(tmp)
                    try:
                        os.symlink("current/" + name, tmp)
                        os.replace(tmp, entry)
                    finally:
                        if os.path.lexists(tmp):
                            os.unlink(tmp)
                _switch_release(root, stage)
                activated = True
            return version
        finally:
            if not activated:
                shutil.rmtree(stage)


def rollback_release(root):
    root = os.path.realpath(root)
    with locking.ProcessLock(os.path.join(root, ".update.lock")):
        with open(os.path.join(root, "previous-release.json"), encoding="utf-8") as f:
            previous = json.load(f).get("path")
        if not previous or not all(os.path.isfile(os.path.join(previous, n)) for n in RUNTIME_FILES):
            raise ValueError("no complete previous release")
        with locking.usb_exclusive():
            if os.path.ismount(config.get("TSF_MNT", "/tmp/teslamnt")):
                raise RuntimeError("USB image is still mounted")
            current = os.path.realpath(os.path.join(root, "current"))
            _switch_release(root, previous)
            atomic_json(os.path.join(root, "previous-release.json"), {"path": current})
