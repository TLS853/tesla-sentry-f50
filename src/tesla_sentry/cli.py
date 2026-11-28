"""Development, production, installer and legacy command-line entry points."""
import argparse
from dataclasses import replace
import hashlib
import importlib
import json
import logging
import os
import signal
import sys
import time

from . import __version__, config


def launcher_path():
    return os.path.abspath(sys.argv[0])


def self_check():
    # Import the complete dependency graph, but never start threads or access USB hardware.
    for name in ("app", "sync", "exfat", "deployment"):
        importlib.import_module("tesla_sentry." + name)
    print("ok " + __version__)
    return 0


def bootstrap_flat_install():
    """Migrate old single-file OTA clients using the runtime embedded in the launcher."""
    from . import assets, deployment, updates
    root = config.settings.install_dir
    entry = os.path.join(root, "sentry-viewer.py")
    if os.path.realpath(launcher_path()) != os.path.realpath(entry) or os.path.islink(os.path.join(root, "current")):
        return False
    with open(entry, encoding="utf-8") as f:
        viewer = f.read()
    contents = {"sentry-viewer.py": viewer,
                "tesla-live-sync.py": viewer.replace('ENTRYPOINT = "viewer"', 'ENTRYPOINT = "sync"')}
    for name in deployment.RUNTIME_FILES:
        if name.endswith(".sh"):
            contents[name] = assets.runtime_script(name).decode("utf-8")
    files = {name: {"content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
             for name, text in contents.items()}
    package = json.dumps({"format": 1, "version": __version__, "files": files}, ensure_ascii=False).encode()
    deployment.install_release(package, root, __version__, legacy_viewer_backup=updates.OTA_BACKUP)
    return True


def sync_archive():
    """Cron entry point. A busy vehicle or management session leaves the disk alone."""
    from . import archive_policy, sync, usb, vehicle_assets

    log = logging.getLogger(__name__)

    if os.path.ismount(usb.MNT):
        log.info("skip: mirror is mounted")
        return 0
    idle = int(config.get("TSF_IDLE") or "600")
    age = time.time() - os.path.getmtime(usb.IMG)
    if age < idle:
        log.info("skip: image written %.0f seconds ago", age)
        return 0
    if sync.live_scan_current() and not vehicle_assets.has_queued_audio():
        log.info("skip: live archive up to date and no queued USB writes")
        return 0

    def stop(*_):
        raise SystemExit(1)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGHUP, signal.SIGTERM)}
    volume = usb.UsbVolume()
    try:
        if not volume.open(background=True):
            log.info("skip: USB host still connected or controller state unknown")
            return 0
        pushed = vehicle_assets.flush_audio(usb.MNT)
        log.info("wrote %d audio files to the USB root", pushed)
        copied = archive_policy.copy_mounted_archive(usb.MNT)
        log.info("archived %d new files", copied)
        return 0
    finally:
        try:
            if volume.restore:
                volume.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None, entrypoint="viewer"):
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args and args[0] == "--usb-lock-exec":
            from .locking import usb_lock_exec
            return usb_lock_exec(args[1:])
        if args[:1] == ["--archive-sync"]:
            from . import locking
            if len(args) != 1 or locking.inherited_usb_lock() is None:
                raise ValueError("archive-sync requires USB lock ownership")
            return sync_archive()
        if args[:1] == ["--archive-copy"]:
            from . import archive_policy, locking
            if len(args) != 2 or locking.inherited_usb_lock() is None:
                raise ValueError("archive-copy requires a mount path and USB lock ownership")
            print(archive_policy.copy_mounted_archive(args[1]))
            return 0
        if args[:1] == ["--audio-flush"]:
            from . import locking, vehicle_assets
            if len(args) != 2 or locking.inherited_usb_lock() is None:
                raise ValueError("audio-flush requires a mount path and USB lock ownership")
            print(vehicle_assets.flush_audio(args[1]))
            return 0
        if args[:1] == ["--install-release"]:
            from .deployment import install_release
            if len(args) != 2:
                raise ValueError("install-release requires a release file")
            with open(args[1], "rb") as f:
                print(install_release(f.read(), config.settings.install_dir))
            return 0
        if args[:1] in (["--ota-check"], ["check"]):
            return self_check()
        if args[:1] == ["sync"]:
            entrypoint, args = "sync", args[1:]
        if entrypoint == "sync":
            from .sync import main as sync_main
            return sync_main(args)
        if args[:1] == ["serve"]:
            args = args[1:]
        parser = argparse.ArgumentParser(description="Tesla Sentry Drive web viewer")
        parser.add_argument("--host", default=config.settings.host)
        parser.add_argument("--port", type=int, default=config.settings.port)
        parser.add_argument("--archive", default=config.settings.archive)
        options = parser.parse_args(args)
        if not 0 <= options.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        config.settings = replace(config.settings, host=options.host, port=options.port,
                                  archive=os.path.abspath(options.archive))
        from . import app, updates
        if bootstrap_flat_install():
            updates._ota_restart()
            return 0
        return app.run()
    except (OSError, ValueError, RuntimeError) as error:
        logging.getLogger(__name__).error("%s", error)
        return 1
