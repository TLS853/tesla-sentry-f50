"""Exercise the modular package and its HTTP contract without USB hardware."""
import ast
import base64
import http.client
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch, Mock

import test_regressions as fixtures

ROOT = fixtures.ROOT


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        fixtures.RegressionTests.setUp(self)

    def server(self):
        server = self.runtime.app.create_server("127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.addCleanup(close)
        return server

    def request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_source_import_and_self_check_have_no_filesystem_side_effects(self):
        self.assertFalse(Path(self.env["TSF_DIR"]).exists())
        for launcher in ("sentry-viewer.py", "tesla-live-sync.py"):
            result = subprocess.run([sys.executable, str(ROOT / launcher), "--ota-check"],
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("ok "))
        self.assertFalse(Path(self.env["TSF_DIR"]).exists())

    def test_http_home_assets_query_routing_and_range_download(self):
        video = self.archive / "SentryClips" / "2026-11-07_12-00-00" / "front.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"0123456789")
        server = self.server()
        status, headers, body = self.request(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'type="module"', body)
        self.assertNotIn(b"__CAT_NAMES__", body)
        for name in ("app.js", "theme.js", "i18n.js", "model.js", "app.css"):
            status, headers, body = self.request(server, "GET", "/assets/" + name)
            self.assertEqual(status, 200, name)
            self.assertEqual(len(body), int(headers["Content-Length"]))
        status, _, body = self.request(server, "GET", "/api/events?ignored=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["SentryClips"]), 1)
        status, headers, body = self.request(server, "GET", "/media/SentryClips/2026-11-07_12-00-00/front.mp4",
                                             headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.request(server, "GET", "/assets/../config.py")[0], 404)

    def test_http_authentication_covers_all_verbs_and_live_password_changes(self):
        auth = self.runtime.auth
        with patch.multiple(auth, AUTH_ON=True, AUTH_USER="admin", AUTH_PASS="old-secret", AUTH_SHA="", AUTH_SECRET="old-key"):
            server = self.server()
            for method, path in (("GET", "/api/events"), ("POST", "/api/ota/apply"), ("DELETE", "/api/file?path=x")):
                self.assertEqual(self.request(server, method, path)[0], 401)
            token = base64.b64encode(b"admin:old-secret").decode()
            headers = {"Authorization": "Basic " + token, "Content-Type": "application/json"}
            status, _, body = self.request(server, "POST", "/api/auth/pass?client=web",
                                             json.dumps({"old": "old-secret", "new": "new-secret"}), headers)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            self.assertEqual(self.request(server, "GET", "/api/events", headers=headers)[0], 401)
            headers["Authorization"] = "Basic " + base64.b64encode(b"admin:new-secret").decode()
            self.assertEqual(self.request(server, "GET", "/api/events", headers=headers)[0], 200)

    def test_media_cannot_follow_symlinks_outside_archive(self):
        private = self.root / "private.mp4"
        private.write_bytes(b"private")
        directory = self.archive / "SentryClips" / "2026-11-07_12-00-00"
        directory.mkdir(parents=True)
        (directory / "front.mp4").symlink_to(private)
        metadata = self.root / "private.json"
        metadata.write_text('{"secret":"private"}')
        (directory / "event.json").symlink_to(metadata)
        server = self.server()
        for path in ("/media/SentryClips/2026-11-07_12-00-00/front.mp4",
                     "/thumb/SentryClips/2026-11-07_12-00-00/front.mp4",
                     "/api/export/SentryClips/2026-11-07_12-00-00"):
            self.assertEqual(self.request(server, "GET", path)[0], 404)
        self.assertEqual(self.runtime.archive.list_events()["SentryClips"][0]["meta"], {})

    def test_configuration_precedence_validation_and_no_shell_execution(self):
        config = self.root / "config"
        config.write_text('TSF_PORT=9000\nTSF_ARCHIVE="/tmp/an archive"\nTSF_OTA_REPO=local/fork\nTSF_LITERAL="$(touch NEVER)"\n')
        env = self.runtime.config.read_environment({"TSF_CONF": str(config), "TSF_PORT": "8000"})
        settings = self.runtime.config.Settings.from_env(env)
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.archive, "/private/tmp/an archive" if sys.platform == "darwin" else "/tmp/an archive")
        self.assertEqual(env["TSF_LITERAL"], "$(touch NEVER)")
        for key, value in (("TSF_PORT", "bad"), ("TSF_MEDIA_CONN", "0"), ("TSF_RECENT_ROLL_MIN", "-1")):
            with self.assertRaises(ValueError):
                self.runtime.config.Settings.from_env({key: value})

    def test_background_workers_stop_without_waiting_for_poll_intervals(self):
        services = self.runtime.app.BackgroundServices()
        with patch.object(self.runtime.retention, "roll_recent"), patch.object(self.runtime.retention, "roll_box"), \
             patch.object(self.runtime.retention, "free_mb", return_value=999999):
            services.start()
            services.stop()
        self.assertTrue(all(not worker.is_alive() for worker in services.workers))

    def test_sync_closes_volume_and_releases_usb_lease_on_exception(self):
        volume = Mock()
        stat = os.stat(self.root)
        volume.fd_dev, volume.fd_ino = stat.st_dev, stat.st_ino
        with patch.object(self.runtime.sync.os, "stat", return_value=stat), \
             patch.object(self.runtime.exfat, "ExFat", return_value=volume), \
             patch.object(self.runtime.sync, "do_sync", side_effect=ValueError("invalid volume")):
            self.assertEqual(self.runtime.sync.main(["--once"]), 1)
        volume.close.assert_called_once()
        lock = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_exfat_parser_closes_descriptor_when_boot_sector_is_invalid(self):
        image = self.root / "invalid.img"
        image.write_bytes(bytes(512))
        with patch.object(self.runtime.exfat.os, "close", wraps=os.close) as close:
            with self.assertRaises(ValueError):
                self.runtime.exfat.ExFat(str(image))
            close.assert_called_once()

    def test_exfat_context_manager_reads_a_minimal_volume(self):
        image = bytearray(4096)
        image[3:11] = b"EXFAT   "
        struct.pack_into("<Q", image, 72, 8)
        struct.pack_into("<I", image, 80, 1)
        struct.pack_into("<I", image, 84, 1)
        struct.pack_into("<I", image, 88, 2)
        struct.pack_into("<I", image, 92, 6)
        struct.pack_into("<I", image, 96, 2)
        image[108:111] = bytes((9, 0, 1))
        path = self.root / "volume.img"
        path.write_bytes(image)
        with self.runtime.exfat.ExFat(str(path)) as volume:
            self.assertEqual(volume.cluster_size, 512)
            self.assertEqual(volume.list_dir(2), [])
        self.assertIsNone(volume.fd)

    def test_package_import_graph_has_no_cycles_or_implicit_shared_globals(self):
        package = ROOT / "src/tesla_sentry"
        edges = {}
        for fp in package.glob("*.py"):
            edges[fp.stem] = set()
            for node in ast.walk(ast.parse(fp.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    if node.module:
                        edges[fp.stem].add(node.module.split(".")[0])
                    else:
                        edges[fp.stem].update(alias.name for alias in node.names)
        def visit(name, stack):
            self.assertNotIn(name, stack, "circular module dependency: " + " -> ".join(stack + [name]))
            for dependency in edges.get(name, ()):
                if dependency not in ("__version__",):
                    visit(dependency, stack + [name])
        for name in edges:
            visit(name, [])

    def test_notification_token_cache_is_scoped_to_application_credentials(self):
        client = self.runtime.feishu
        results = [{"code": 0, "tenant_access_token": "token-one", "expire": 7200},
                   {"code": 0, "tenant_access_token": "token-two", "expire": 7200}]
        one, two = {"app_id": "one", "app_secret": "secret"}, {"app_id": "two", "app_secret": "secret"}
        with patch.object(client, "_post_json", side_effect=results) as request:
            self.assertEqual(client._feishu_token(one), "token-one")
            self.assertEqual(client._feishu_token(one), "token-one")
            self.assertEqual(client._feishu_token(two), "token-two")
            self.assertEqual(request.call_count, 2)

    def test_notification_save_failure_is_reported_instead_of_acknowledged(self):
        with patch.object(self.runtime.storage, "atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.runtime.notifications.notify_save({"enabled": True})

    def test_translations_are_strings_and_camera_count_is_translated(self):
        for table in self.runtime.assets.LANG_TABLE.values():
            self.assertTrue(all(isinstance(value, str) for value in table.values()))
        self.assertEqual(self.runtime.assets.tr("路", "en"), "cams")


if __name__ == "__main__":
    unittest.main()
