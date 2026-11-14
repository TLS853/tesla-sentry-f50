"""Hardware-free regressions for archive, configuration, locks and releases."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "src"))


def load_runtime():
    import importlib
    for name in list(sys.modules):
        if name == "tesla_sentry" or name.startswith("tesla_sentry."):
            del sys.modules[name]
    names = ("app", "archive", "archive_policy", "assets", "auth", "cli", "config", "deployment",
             "exfat", "feishu", "qqbot", "vehicle_assets", "push_media", "storage", "http", "http_get", "http_post", "locking", "media", "notifications",
             "retention", "routes", "sync", "telemetry", "transcode", "updates", "update_package", "usb")
    return types.SimpleNamespace(**{name: importlib.import_module("tesla_sentry." + name) for name in names})


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.env = dict(os.environ, TSF_DIR=str(self.root / "install"),
                        TSF_ARCHIVE=str(self.archive), TSF_USB_LOCK=str(self.root / "usb.lock"),
                        TSF_AUTH_FILE=str(self.root / "no-auth"), TSF_CONF=str(self.root / "no-conf"))
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime = load_runtime()
        self.live = self.runtime.sync

    def source_file(self, cat, event, name="front.mp4", content=b"video"):
        fp = self.root / "mount" / "TeslaCam" / cat / event / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
        return fp

    def test_mounted_copy_obeys_tombstones_window_and_merges_partial_events(self):
        old, new = "2026-11-07_10-00-00", "2026-11-07_12-00-00"
        self.source_file("RecentClips", old)
        self.source_file("RecentClips", new)
        self.source_file("SentryClips", old)
        self.source_file("SavedClips", new)
        self.source_file("SavedClips", new, "back.mp4", b"back")
        (self.archive / ".pruned").write_text("SentryClips/" + old + "\n")
        partial = self.archive / "SavedClips" / new / "front.mp4"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"v")
        self.assertEqual(self.runtime.archive_policy.copy_mounted_archive(str(self.root / "mount")), 3)
        self.assertEqual(partial.read_bytes(), b"video")
        self.assertFalse((self.archive / "RecentClips" / old).exists())
        self.assertFalse((self.archive / "SentryClips" / old).exists())
        self.assertEqual(self.runtime.archive_policy.copy_mounted_archive(str(self.root / "mount")), 0)
        self.assertFalse(list(self.archive.rglob("*.part")))

    def test_live_sync_uses_same_filters_and_waits_for_two_stable_scans(self):
        old, new = "2026-11-07_10-00-00", "2026-11-07_12-00-00"
        entries = {("RecentClips", old, "front.mp4"): Mock(size=3, vdl=3, mtime=1),
                   ("RecentClips", new, "front.mp4"): Mock(size=3, vdl=3, mtime=1),
                   ("SentryClips", old, "front.mp4"): Mock(size=3, vdl=3, mtime=1)}
        (self.archive / ".pruned").write_text("SentryClips/" + old + "\n")
        with patch.object(self.live, "scan", return_value=entries), patch.object(self.live, "save_state"), \
             patch.object(self.live, "copy_entry", return_value=3) as copy_file, patch.object(self.live, "log"):
            state = {}
            self.assertEqual(self.live.do_sync(None, state), (0, 0))
            self.assertEqual(self.live.do_sync(None, state), (1, 3))
            self.assertEqual(copy_file.call_args.args[2], str((self.archive / "RecentClips" / new / "front.mp4").resolve()))

    def test_cross_process_usb_exclusion_and_release(self):
        code = "import fcntl,os,sys; f=open(sys.argv[1],'a');\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(75)"
        lock = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        lock.acquire()
        try:
            result = subprocess.run([sys.executable, "-c", code, lock.path])
            self.assertEqual(result.returncode, 75)
        finally:
            lock.release()
        self.assertEqual(subprocess.run([sys.executable, "-c", code, lock.path]).returncode, 0)

    def test_shell_child_inherits_usb_lock_for_archive_copy(self):
        self.source_file("SavedClips", "2026-11-07_12-00-00")
        command = [sys.executable, str(ROOT / "sentry-viewer.py"), "--usb-lock-exec", "/bin/sh", "-c",
                   'exec "$1" "$2" --archive-copy "$3"', "sh", sys.executable,
                   str(ROOT / "sentry-viewer.py"), str(self.root / "mount")]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")

    def test_failed_usb_detach_keeps_lease_and_does_not_reexport(self):
        lease = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        lease.acquire()
        self.runtime.usb.usb_lease = lease
        self.addCleanup(lease.release)
        with patch.object(self.runtime.usb.os.path, "ismount", return_value=True), \
             patch.object(self.runtime.usb.subprocess, "run", return_value=Mock(returncode=1)), \
             patch.object(self.runtime.usb.time, "sleep"), patch.object(self.runtime.usb, "_arm_usb_timer") as retry, \
             patch.object(self.runtime.usb, "_gadget_rebind") as rebind:
            self.assertFalse(self.runtime.usb.usb_detach()[0])
            self.assertIs(self.runtime.usb.usb_lease, lease)
            rebind.assert_not_called()
            retry.assert_called_once()
        other = self.runtime.locking.ProcessLock(lease.path)
        self.assertFalse(other.acquire(blocking=False))

    def test_failed_stop_export_prevents_mount(self):
        with patch.object(self.runtime.usb, "_gadget_rebind", return_value=False), \
             patch.object(self.runtime.usb.os.path, "ismount", return_value=False), \
             patch.object(self.runtime.usb.subprocess, "run") as run:
            self.assertFalse(self.runtime.usb.usb_attach()[0])
            run.assert_not_called()
        lock = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_openwrt_and_generic_services_export_added_configuration(self):
        install = (ROOT / "install.sh").read_text()
        conf = self.root / "settings.conf"
        conf.write_text("TSF_RECENT_ROLL_MIN=120\nTSF_BOX_KEEP_DAYS=30\nTSF_OTA_REPO=example/fork\nTSF_FUTURE_OPTION=yes\n")
        # Execute actual config-loading blocks extracted from all Python service templates.
        for marker in ('cat > /etc/init.d/sentry-viewer', 'cat > /etc/init.d/tesla-live', 'cat > "$TSF_DIR/tsf-start.sh"'):
            start = install.index(marker)
            block = install[start:install.index("\nEOF", start)]
            config = block[block.index("set -a"):block.index("set +a") + len("set +a")]
            result = subprocess.run(["/bin/sh", "-c", config + '\nexec "$1" -c "import os; print(os.environ.get(\'TSF_RECENT_ROLL_MIN\'),os.environ.get(\'TSF_FUTURE_OPTION\'))"',
                                     "sh", sys.executable], env=dict(self.env, CONF=str(conf)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "120 yes")

    def test_reinstall_preserves_optional_config_and_selected_paths_win(self):
        import runpy
        merge = runpy.run_path(str(ROOT / "tools/merge_config.py"))["merge"]
        existing = "# custom\nTSF_DIR=/old\nTSF_BOX_KEEP_DAYS=45\nTSF_RECENT_ROLL_MIN=180\nTSF_FUTURE_OPTION=yes\n"
        defaults = "TSF_DIR=/new\nTSF_BOX_KEEP_DAYS=14\nTSF_RECENT_ROLL_MIN=60\nTSF_PORT=8080\n"
        result = merge(existing, defaults)
        self.assertIn("TSF_DIR=/new\n", result)
        self.assertIn("TSF_BOX_KEEP_DAYS=45\n", result)
        self.assertIn("TSF_RECENT_ROLL_MIN=180\n", result)
        self.assertIn("TSF_FUTURE_OPTION=yes\n", result)

    def test_procd_command_loads_config_in_the_daemon_child(self):
        install = (ROOT / "install.sh").read_text()
        conf = self.root / "config"
        conf.write_text("TSF_DIR=" + str(self.root) + "\nTSF_FUTURE_OPTION=yes\n")
        for name in ("sentry-viewer.py", "tesla-live-sync.py"):
            (self.root / name).write_text('import os; print(os.environ.get("TSF_FUTURE_OPTION"))')
        for marker in ('cat > /etc/init.d/sentry-viewer', 'cat > /etc/init.d/tesla-live'):
            start = install.index("\n", install.index(marker)) + 1
            block = install[start:install.index("\nEOF", start)]
            # Let a real shell perform the install-time heredoc expansion.
            expanded = subprocess.run(["/bin/sh", "-c", "cat <<EOF\n" + block + "\nEOF"],
                                      env=dict(self.env, CONF=str(conf), PYTHON=sys.executable),
                                      capture_output=True, text=True, check=True).stdout
            # procd is another daemon: model its clean environment, not our shell's exports.
            stub = '''
procd_open_instance() { :; }
procd_close_instance() { :; }
procd_set_param() {
    if [ "$1" = command ]; then
        shift
        env -i PATH="$PATH" "$@"
    fi
}
'''
            result = subprocess.run(["/bin/sh", "-c", stub + expanded + "\nstart_service\n"],
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "yes")

    def test_shell_unmount_failure_leaves_usb_disconnected(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        mounted = self.root / "mounted"
        commands = {
            "ls": '#!/bin/sh\necho controller\n',
            "date": '#!/bin/sh\necho 1000\n',
            "sleep": '#!/bin/sh\nexit 0\n',
            "sync": '#!/bin/sh\nexit 0\n',
            "losetup": '#!/bin/sh\n[ "$1" != -f ] || echo /dev/fake\nexit 0\n',
            "mount": '#!/bin/sh\ntouch "$TEST_MOUNTED"\n',
            "umount": '#!/bin/sh\nexit 1\n',
            "grep": '#!/bin/sh\nfor arg do\n if [ "$arg" = /proc/mounts ]; then\n  [ -f "$TEST_MOUNTED" ]; exit $?\n fi\ndone\nexec /usr/bin/grep "$@"\n',
        }
        for name, text in commands.items():
            fp = bin_dir / name
            fp.write_text(text)
            fp.chmod(0o755)
        gadget = self.root / "gadget"
        (gadget / "configs").mkdir(parents=True)
        (gadget / "UDC").write_text("controller")
        install = Path(self.env["TSF_DIR"])
        install.mkdir()
        (install / "sentry-viewer.py").symlink_to(ROOT / "sentry-viewer.py")
        env = dict(self.env, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                   TSF_GADGET=str(gadget), TSF_PYTHON=sys.executable,
                   TSF_MNT=str(self.root / "mount"), TSF_IDLE="0", TEST_MOUNTED=str(mounted))
        result = subprocess.run(["/bin/sh", str(ROOT / "scripts/tesla-sync.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((gadget / "UDC").read_text().strip(), "")
        self.assertIn("umount failed", (install / "tesla-sync.log").read_text())

    def test_http_routes_keep_authentication_and_response_contracts(self):
        handler = object.__new__(self.runtime.http.Handler)
        handler._send = Mock()
        handler._json = Mock()
        handler._gate = Mock(return_value=True)
        handler.path = "/api/events"
        with patch.object(self.runtime.archive, "list_events", return_value={"SentryClips": []}):
            handler.do_GET()
        handler._json.assert_called_once_with({"SentryClips": []})
        handler.path = "/api/ota/apply"
        body = b'zip bytes'
        handler.headers = {"Content-Type": "application/zip", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._json.reset_mock()
        with patch.object(self.runtime.updates, "ota_apply", return_value=(True, "updated")) as apply:
            handler.do_POST()
            apply.assert_called_once_with(handler.rfile, len(body), "")
        handler._json.assert_called_once_with({"ok": True, "msg": "updated"})
        handler._gate.return_value = False
        with patch.object(self.runtime.updates, "ota_apply") as apply:
            handler.do_POST()
            apply.assert_not_called()
        with patch.object(self.runtime.usb.os, "remove") as remove:
            handler.path = "/api/file?path=private"
            handler.do_DELETE()
            remove.assert_not_called()
        handler._gate.return_value = True
        handler.path = "/unknown"
        handler.do_GET()
        handler._send.assert_called_once_with(404, b"not found")

    def test_obsolete_idle_timer_does_not_end_active_management(self):
        self.runtime.usb.usb_generation = 2
        with patch.object(self.runtime.usb, "usb_detach") as detach:
            self.runtime.usb.usb_detach_safe(1)
            detach.assert_not_called()

    def test_legacy_single_file_ota_bootstraps_all_companions(self):
        root = Path(self.env["TSF_DIR"])
        root.mkdir()
        for name in self.runtime.deployment.RUNTIME_FILES:
            (root / name).write_text("old companion")
        (root / "sentry-viewer.py").write_text((ROOT / "sentry-viewer.py").read_text())
        backup = root / "sentry-viewer.bak.py"
        backup.write_text("previous viewer")
        self.runtime.cli.launcher_path = lambda: str(root / "sentry-viewer.py")
        self.runtime.updates.OTA_BACKUP = str(backup)
        self.assertTrue(self.runtime.cli.bootstrap_flat_install())
        self.assertFalse(self.runtime.cli.bootstrap_flat_install())
        self.assertIn('ENTRYPOINT = "sync"', (root / "tesla-live-sync.py").read_text())
        self.assertIn("--archive-copy", (root / "tesla-sync.sh").read_text())
        self.runtime.deployment.rollback_release(str(root))
        self.assertEqual((root / "sentry-viewer.py").read_text(), "previous viewer")

    def package(self, version):
        import runpy
        artifacts = runpy.run_path(str(ROOT / "tools/build.py"))["outputs"](version=version)
        return json.loads(artifacts["dist/release.json"])

    def install(self, data):
        return self.runtime.deployment.install_release(json.dumps(data).encode(), self.env["TSF_DIR"], data["version"])

    def test_complete_release_switch_and_rollback(self):
        self.install(self.package("2.6.1"))
        root = Path(self.env["TSF_DIR"])
        before = (root / "current").resolve()
        for name in self.runtime.deployment.RUNTIME_FILES:
            self.assertTrue((root / name).is_symlink())
        self.install(self.package("2.6.2"))
        after = (root / "current").resolve()
        self.assertNotEqual(before, after)
        self.runtime.deployment.rollback_release(str(root))
        self.assertEqual((root / "current").resolve(), before)
        self.runtime.deployment.rollback_release(str(root))
        self.assertEqual((root / "current").resolve(), after)

    def test_corrupt_or_incomplete_package_preserves_current_release(self):
        self.install(self.package("2.6.1"))
        root = Path(self.env["TSF_DIR"])
        before = (root / "current").resolve()
        corrupt = self.package("2.6.2")
        corrupt["files"]["tesla-sync.sh"]["content"] += "bad"
        missing = self.package("2.6.2")
        del missing["files"]["tesla-live-sync.py"]
        escape = self.package("2.6.2")
        escape["files"]["../../escaped"] = escape["files"].pop("tesla-img.sh")
        for package in (corrupt, missing, escape):
            with self.assertRaises(ValueError):
                self.install(package)
            self.assertEqual((root / "current").resolve(), before)
        self.assertEqual(len(list((root / "releases").iterdir())), 1)

    def test_busy_usb_prevents_release_activation(self):
        self.install(self.package("2.6.1"))
        root = Path(self.env["TSF_DIR"])
        before = (root / "current").resolve()
        with self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"]):
            with self.assertRaises(RuntimeError):
                self.install(self.package("2.6.2"))
        self.assertEqual((root / "current").resolve(), before)

    def test_legacy_flat_install_migrates_without_touching_user_data(self):
        root = Path(self.env["TSF_DIR"])
        root.mkdir()
        for name in self.runtime.deployment.RUNTIME_FILES:
            (root / name).write_text("legacy " + name)
        (root / "auth.conf").write_text("private settings")
        (root / "teslacam.img").write_bytes(b"image")
        self.install(self.package("2.6.1"))
        self.runtime.deployment.rollback_release(str(root))
        for name in self.runtime.deployment.RUNTIME_FILES:
            self.assertEqual((root / name).read_text(), "legacy " + name)
        self.assertEqual((root / "auth.conf").read_text(), "private settings")
        self.assertEqual((root / "teslacam.img").read_bytes(), b"image")


if __name__ == "__main__":
    unittest.main()
