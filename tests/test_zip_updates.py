"""Offline ZIP uploads using local HTTP and temporary release stores."""
import hashlib
import io
import json
from pathlib import Path
import runpy
import stat
import struct
import unittest
import zipfile
from unittest.mock import patch

import test_regressions as fixtures
import test_runtime as http_fixtures


class ZipUpdateTests(unittest.TestCase):
    server = http_fixtures.RuntimeTests.server
    request = http_fixtures.RuntimeTests.request

    def setUp(self):
        fixtures.RegressionTests.setUp(self)
        self.updates = self.runtime.updates
        self.format = self.runtime.update_package

    def release(self, version):
        return fixtures.RegressionTests.package(self, version)

    def zip(self, version="9.0.0", release=None):
        release = release or self.release(version)
        return self.format.make_zip(release, {"version": version, "date": "2026-11-08", "notes": ["Local update test"]})

    def rewrite(self, data, change=None, omit=None, extra=()):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                if item.filename == omit:
                    continue
                content = source.read(item)
                if change:
                    item, content = change(item, content)
                target.writestr(item, content)
            for name, content in extra:
                target.writestr(name, content)
        return output.getvalue()

    def test_artifact_is_deterministic_and_matches_built_runtime(self):
        package = runpy.run_path(str(fixtures.ROOT / "tools/package_update.py"))["package"]
        first = package(output=self.root / "update.zip")
        again = package(output=self.root / "again.zip")
        self.assertEqual(first.read_bytes(), again.read_bytes())
        manifest, release = self.format.read_zip(first.read_bytes())
        self.assertEqual(manifest["version"], self.updates.APP_VERSION)
        expected = json.loads((fixtures.ROOT / "dist/release.json").read_text())
        self.assertEqual(json.loads(release), expected)
        self.assertEqual(first.with_suffix('.zip.sha256').read_text().split()[0], hashlib.sha256(first.read_bytes()).hexdigest())

    def test_zip_rejects_traversal_missing_files_symlinks_and_tampering(self):
        data = self.zip()

        def symlink(item, content):
            if item.filename == "tesla-sync.sh":
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
            return item, content

        def corrupt(item, content):
            return item, content + b"changed" if item.filename == "tesla-sync.sh" else content

        for invalid in (b"<html>not a ZIP</html>", data[:30],
                        self.rewrite(data, omit="tesla-live-sync.py"),
                        self.rewrite(data, extra=(("../escaped", b"outside"),)),
                        self.rewrite(data, change=symlink), self.rewrite(data, change=corrupt)):
            with self.assertRaises(ValueError):
                self.format.read_zip(invalid)
        self.assertFalse((self.root / "escaped").exists())

    def test_zip_rejects_duplicate_members_encryption_and_oversize_payloads(self):
        data = self.zip()
        with self.assertWarns(UserWarning):
            duplicate = self.rewrite(data, extra=(("tesla-sync.sh", b"duplicate"),))
        with self.assertRaises(ValueError):
            self.format.read_zip(duplicate)
        encrypted = bytearray(data)
        central = encrypted.index(b"PK\x01\x02")
        struct.pack_into("<H", encrypted, central + 8, struct.unpack_from("<H", encrypted, central + 8)[0] | 1)
        with self.assertRaises(ValueError):
            self.format.read_zip(bytes(encrypted))
        oversized = self.rewrite(data, change=lambda item, content: (item, b"x" * (self.format.MAX_MEMBER + 1))
                                 if item.filename == "tesla-sync.sh" else (item, content))
        with self.assertRaises(ValueError):
            self.format.read_zip(oversized)
        duplicated_manifest = self.rewrite(data, change=lambda item, content:
            (item, b'{"format":1,' + content[1:]) if item.filename == "manifest.json" else (item, content))
        with self.assertRaisesRegex(ValueError, "重复"):
            self.format.read_zip(duplicated_manifest)

    def apply(self, data, sha256="", length=None):
        return self.updates.ota_apply(io.BytesIO(data), len(data) if length is None else length, sha256)

    def test_incomplete_and_oversize_uploads_do_not_activate(self):
        data = self.zip()
        with patch.object(self.runtime.deployment, "install_release") as install:
            self.assertIn("不完整", self.apply(data[:10], length=len(data))[1])
            with patch.object(self.format, "MAX_ZIP_SIZE", 4):
                self.assertIn("超过", self.apply(data)[1])
            install.assert_not_called()
        self.assertFalse(self.updates.ota_status()["busy"])

    def test_hash_and_version_rejection_leave_installation_untouched(self):
        future = self.zip()
        with patch.object(self.runtime.deployment, "install_release") as install, \
             patch.object(self.updates, "_restart_later") as restart:
            ok, message = self.apply(future, "0" * 64)
            self.assertFalse(ok)
            self.assertIn("SHA-256", message)
            for version in (self.updates.APP_VERSION, "0.1.0"):
                self.assertFalse(self.apply(self.zip(version))[0])
            install.assert_not_called()
            restart.assert_not_called()

    def test_failed_install_never_schedules_restart(self):
        bad = self.release("9.0.0")
        content = "this is invalid Python ("
        bad["files"]["tesla-live-sync.py"] = {"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        with patch.object(self.updates, "_restart_later") as restart:
            self.assertFalse(self.apply(self.zip(release=bad))[0])
            self.assertFalse((Path(self.env["TSF_DIR"]) / "current").exists())
            restart.assert_not_called()

    def test_http_status_and_rejected_legacy_url_requests_never_contact_network(self):
        server = self.server()
        with patch("urllib.request.build_opener") as network, patch.object(self.updates, "ota_apply") as apply:
            for path in ("/api/ota/status", "/api/ota/check", "/api/ota/check?force=1"):
                status, _, body = self.request(server, "GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["mode"], "upload_zip")
            old_body = json.dumps({"url": "https://updates.example/release.zip"})
            status, _, body = self.request(server, "POST", "/api/ota/apply", old_body,
                                           {"Content-Type": "application/json"})
            self.assertEqual(status, 415)
            self.assertIn("本地 ZIP", json.loads(body)["msg"])
            status, _, _ = self.request(server, "POST", "/api/ota/apply", b"",
                                         {"Content-Type": "application/zip"})
            self.assertEqual(status, 400)
            status, _, _ = self.request(server, "POST", "/api/ota/apply", b"",
                                         {"Content-Type": "application/zip",
                                          "Content-Length": str(self.format.MAX_ZIP_SIZE + 1)})
            self.assertEqual(status, 413)
            apply.assert_not_called()
            network.assert_not_called()

    def test_http_upload_and_rollback_preserve_user_data(self):
        store = Path(self.env["TSF_DIR"])
        self.runtime.deployment.install_release(json.dumps(self.release("2.8.0")).encode(), str(store))
        previous = (store / "current").resolve()
        sentinels = {store / "auth.conf": b"account", store / "notify.json": b"QQ credentials",
                     store / "teslacam.img": b"existing recordings", self.archive / "keep.mp4": b"archive recordings"}
        for path, content in sentinels.items():
            path.write_bytes(content)
        data = self.zip()
        server = self.server()
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(self.updates, "_restart_later") as restart, patch("urllib.request.build_opener") as network:
            status, _, result = self.request(server, "POST", "/api/ota/apply", data,
                                             {"Content-Type": "application/zip", "X-Update-SHA256": digest})
            self.assertEqual(status, 200, result)
            self.assertTrue(json.loads(result)["ok"], result)
            self.assertNotEqual((store / "current").resolve(), previous)
            self.assertTrue(self.updates.ota_status()["rollback_ready"])
            self.assertFalse((store / "sentry-viewer.bak.py").exists())
            history = json.loads(Path(self.updates.OTA_STATE).read_text())
            self.assertEqual(history["sha256"], digest)
            self.assertTrue(self.updates.ota_rollback()[0])
            self.assertEqual((store / "current").resolve(), previous)
            self.assertEqual(restart.call_count, 2)
            network.assert_not_called()
        for path, content in sentinels.items():
            self.assertEqual(path.read_bytes(), content)

    def test_busy_usb_and_optional_history_failure_have_correct_activation_result(self):
        data = self.zip()
        lease = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        lease.acquire()
        try:
            with patch.object(self.updates, "_restart_later") as restart:
                self.assertFalse(self.apply(data)[0])
                restart.assert_not_called()
        finally:
            lease.release()
        with patch.object(self.runtime.deployment, "install_release"), \
             patch.object(self.runtime.deployment, "atomic_json", side_effect=OSError("history is unavailable")), \
             patch.object(self.updates, "_restart_later") as restart:
            self.assertTrue(self.apply(data)[0])
            restart.assert_called_once()
