"""Manual ZIP updates, exercised against local HTTP and temporary release stores."""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import runpy
import stat
import struct
import threading
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

    def download_server(self, routes):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                status, headers, body = routes.get(self.path.split("?", 1)[0], (404, {}, b"missing"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, str(value))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        return "http://%s:%s" % server.server_address, requests

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

    def test_local_download_accepts_signed_query_and_redirect_without_extra_fetches(self):
        data = self.zip()
        base, requests = self.download_server({
            "/redirect": (302, {"Location": "/release.zip?X-Amz-Signature=example-signature"}, b""),
            "/release.zip": (200, {"Content-Length": len(data)}, data),
        })
        self.assertEqual(self.updates._download_zip(base + "/redirect"), data)
        self.assertEqual(requests, ["/redirect", "/release.zip?X-Amz-Signature=example-signature"])

    def test_download_rejects_partial_oversize_and_http_errors_without_leaking_query(self):
        base, _requests = self.download_server({
            "/partial": (200, {"Content-Length": 20}, b"short"),
            "/large": (200, {"Content-Length": self.format.MAX_DOWNLOAD + 1}, b""),
            "/denied": (403, {}, b"denied"),
            "/range": (206, {}, b"partial"),
            "/stream": (200, {}, b"12345"),
        })
        for path in ("/partial", "/large", "/denied", "/range"):
            with self.assertRaises(ValueError) as error:
                self.updates._download_zip(base + path + "?X-Amz-Signature=private-signature")
            self.assertNotIn("private-signature", str(error.exception))
        with patch.object(self.format, "MAX_DOWNLOAD", 4):
            with self.assertRaisesRegex(ValueError, "超过"):
                self.updates._download_zip(base + "/stream")

    def test_hash_and_version_rejection_leave_installation_untouched(self):
        future = self.zip()
        with patch.object(self.updates, "_download_zip", return_value=future), \
             patch.object(self.runtime.deployment, "install_release") as install, patch.object(self.updates, "_restart_later") as restart:
            ok, message = self.updates.ota_apply("https://example.test/update.zip", "0" * 64)
            self.assertFalse(ok)
            self.assertIn("SHA-256", message)
            install.assert_not_called()
            restart.assert_not_called()
        for version in (self.updates.APP_VERSION, "0.1.0"):
            with patch.object(self.updates, "_download_zip", return_value=self.zip(version)), \
                 patch.object(self.runtime.deployment, "install_release") as install:
                self.assertFalse(self.updates.ota_apply("https://example.test/update.zip")[0])
                install.assert_not_called()

    def test_url_validation_and_failed_install_never_schedule_restart(self):
        with patch.object(self.updates, "_download_zip") as download:
            for url in ("", "file:///etc/passwd", "https://user:password@example.test/a.zip", "https://example.test/a.zip#fragment", "https://example.test/\n.zip"):
                self.assertFalse(self.updates.ota_apply(url)[0])
            download.assert_not_called()
        bad = self.release("9.0.0")
        content = "this is invalid Python ("
        bad["files"]["tesla-live-sync.py"] = {"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        with patch.object(self.updates, "_download_zip", return_value=self.zip(release=bad)), \
             patch.object(self.updates, "_restart_later") as restart:
            self.assertFalse(self.updates.ota_apply("https://example.test/update.zip")[0])
            self.assertFalse((Path(self.env["TSF_DIR"]) / "current").exists())
            restart.assert_not_called()

    def test_http_status_and_invalid_apply_requests_cannot_start_download(self):
        server = self.server()
        with patch.object(self.updates.urllib.request, "build_opener") as network:
            for path in ("/api/ota/status", "/api/ota/check", "/api/ota/check?force=1"):
                status, _, body = self.request(server, "GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["mode"], "manual_zip")
            for body in ('{}', '[]', '{"url":12}', '{"url":"x","sha256":false}', 'invalid'):
                status, _, _ = self.request(server, "POST", "/api/ota/apply", body, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
            network.assert_not_called()

    def test_real_http_zip_update_and_rollback_preserve_user_data_and_signed_url_privacy(self):
        store = Path(self.env["TSF_DIR"])
        self.runtime.deployment.install_release(json.dumps(self.release("2.8.0")).encode(), str(store))
        previous = (store / "current").resolve()
        sentinels = {store / "auth.conf": b"account", store / "notify.json": b"QQ credentials",
                     store / "teslacam.img": b"existing recordings", self.archive / "keep.mp4": b"archive recordings"}
        for path, content in sentinels.items():
            path.write_bytes(content)
        data = self.zip()
        base, requests = self.download_server({"/update.zip": (200, {"Content-Length": len(data)}, data)})
        server = self.server()
        body = json.dumps({"url": base + "/update.zip?X-Amz-Signature=private-signature",
                           "sha256": hashlib.sha256(data).hexdigest()})
        with patch.object(self.updates, "_restart_later") as restart, \
             patch.dict(self.runtime.config.environment, {"TSF_OTA_ENABLED": "0"}):
            status, _, result = self.request(server, "POST", "/api/ota/apply", body, {"Content-Type": "application/json"})
            self.assertEqual(status, 200, result)
            self.assertTrue(json.loads(result)["ok"], result)
            self.assertNotEqual((store / "current").resolve(), previous)
            self.assertTrue(self.updates.ota_status()["rollback_ready"])
            self.assertFalse((store / "sentry-viewer.bak.py").exists())
            history = Path(self.updates.OTA_STATE).read_text()
            self.assertNotIn("private-signature", history)
            self.assertNotIn("http", history)
            self.assertTrue(self.updates.ota_rollback()[0])
            self.assertEqual((store / "current").resolve(), previous)
            self.assertEqual(restart.call_count, 2)
        for path, content in sentinels.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(requests, ["/update.zip?X-Amz-Signature=private-signature"])

    def test_busy_usb_and_optional_history_failure_have_correct_activation_result(self):
        data = self.zip()
        lease = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        lease.acquire()
        try:
            with patch.object(self.updates, "_download_zip", return_value=data), patch.object(self.updates, "_restart_later") as restart:
                self.assertFalse(self.updates.ota_apply("https://example.test/update.zip")[0])
                restart.assert_not_called()
        finally:
            lease.release()
        with patch.object(self.updates, "_download_zip", return_value=data), \
             patch.object(self.runtime.deployment, "install_release"), \
             patch.object(self.runtime.deployment, "atomic_json", side_effect=OSError("history is unavailable")), \
             patch.object(self.updates, "_restart_later") as restart:
            self.assertTrue(self.updates.ota_apply("https://example.test/update.zip")[0])
            restart.assert_called_once()
