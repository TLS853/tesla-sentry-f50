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
from unittest.mock import MagicMock, patch, Mock

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

    def test_viewer_reaches_http_loop_with_an_existing_vehicle_image(self):
        app = self.runtime.app
        image = self.root / "teslacam.img"
        image.write_bytes(b"existing vehicle image")
        server = Mock(server_address=("127.0.0.1", 8080))
        with patch.object(app.usb, "IMG", str(image)), \
             patch.object(app.usb, "usb_detach", return_value=(False, "busy")) as recover, \
             patch.object(app, "create_server", return_value=server), \
             patch.object(app, "BackgroundServices"):
            self.assertEqual(app.run(), 0)
        recover.assert_called_once()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_http_home_assets_query_routing_and_range_download(self):
        video = self.archive / "SentryClips" / "2026-11-07_12-00-00" / "front.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"0123456789")
        server = self.server()
        status, headers, body = self.request(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'type="module"', body)
        self.assertNotIn(b"__CAT_NAMES__", body)
        for name in ("app.js", "theme.js", "i18n.js", "model.js", "icons.js", "app.css"):
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
            status, _, body = self.request(server, "GET", "/login", headers={"Accept-Language": "en-US"})
            self.assertEqual(status, 200)
            self.assertIn(b'Welcome back', body)
            self.assertIn(b'safely remembered.', body)
            self.assertNotIn(b'__L_', body)
            self.assertIn(b'autocomplete="current-password"', body)
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
        volume = MagicMock(identity=["test-volume"])
        volume.__enter__.return_value = volume
        with patch.object(self.runtime.exfat, "ExFat", return_value=volume), \
             patch.object(self.runtime.sync, "do_sync", side_effect=ValueError("invalid volume")):
            self.assertEqual(self.runtime.sync.main(["--once"]), 1)
        volume.__exit__.assert_called_once()
        lock = self.runtime.locking.ProcessLock(self.env["TSF_USB_LOCK"])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_live_sync_reads_new_volume_after_reformatting_same_image(self):
        sync = self.runtime.sync
        image = self.root / "retained-inode.img"

        def create_volume(serial, root):
            vbr = bytearray(4096)
            vbr[3:11] = b"EXFAT   "
            struct.pack_into("<QIIIIII", vbr, 72, 8, 1, 1, 2, 6, root, serial)
            vbr[108:111] = bytes((9, 0, 1))
            with image.open("r+b") as stream:
                stream.write(vbr)

        image.touch()
        create_volume(1, 2)
        original_inode = image.stat().st_ino
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        stop.wait.side_effect = lambda *_: create_volume(2, 3)
        observations = []

        def scan(volume, state):
            observations.append((volume.root_cluster, state.get("__pending__")))
            state["__pending__"] = {"old recording": [50, 50, 1]}
            return 0, 0

        with patch.object(sync, "IMG", str(image)), patch.object(sync, "load_state", return_value={}), \
             patch.object(sync, "do_sync", side_effect=scan):
            self.assertEqual(sync.main([], stop_event=stop), 0)
        self.assertEqual(image.stat().st_ino, original_inode)
        self.assertEqual(observations, [(2, None), (3, None)])

    def test_sync_status_keeps_latest_error_and_last_success_separate(self):
        store = Path(self.env["TSF_DIR"])
        store.mkdir()
        image = store / "teslacam.img"
        with image.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024)
        (store / "tesla-live.log").write_text("[x] 已同步 earlier\n[x] 镜像读取失败\n")
        state = {"__pending__": {}, "health": {"last_success": 950}}
        with patch.object(self.runtime.sync.time, "time", return_value=1000), \
             patch.object(self.runtime.media, "_proc_running", return_value=True):
            self.runtime.sync.save_health(state, "read_error", detail="镜像读取失败")
            status = self.runtime.media.live_status()
        self.assertEqual(status["sync"]["phase"], "read_error")
        self.assertEqual(status["sync"]["last_success"], 950)
        self.assertEqual(status["last"], "镜像读取失败")
        self.assertEqual(status["storage"]["image"]["capacity"], 8 * 1024 * 1024)
        self.assertLessEqual(status["storage"]["image"]["allocated"], status["storage"]["image"]["capacity"])

    def test_data_saver_does_not_fall_back_to_original_video(self):
        video = self.archive / "RecentClips" / "2026-11-14_16-00-00" / "front.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"original video")
        server = self.server()
        route = "/media/RecentClips/2026-11-14_16-00-00/front.mp4?q=360p"
        with patch.object(self.runtime.transcode, "get_variant", side_effect=lambda path, quality: path):
            code, _, body = self.request(server, "GET", route + "&strict=1")
            self.assertEqual(code, 503)
            self.assertNotIn(b"original video", body)
            code, _, body = self.request(server, "GET", route)
            self.assertEqual((code, body), (200, b"original video"))

    def test_telemetry_reads_official_fields_from_avc_and_hevc_mp4(self):
        telemetry = self.runtime.telemetry
        fields = bytes((8, 1, 16, 1, 24, 42, 56, 1, 64, 1, 72, 1, 80, 2))
        for number, value in {4: 20, 5: 37.5, 6: -18.5}.items():
            fields += bytes(((number << 3) | 5,)) + struct.pack("<f", value)
        fields += b"\x75" + struct.pack("<f", 0.5)
        fields += b"\x7d" + struct.pack("<f", -0.75)
        fields += b"\x85\x01" + struct.pack("<f", 9.8)
        for number, value in {11: 31.25, 12: 121.5, 13: 180.0}.items():
            fields += bytes(((number << 3) | 1,)) + struct.pack("<d", value)
        escaped = bytearray()
        zeros = 0
        for byte in fields:
            if zeros >= 2 and byte <= 3:
                escaped.append(3)
                zeros = 0
            escaped.append(byte)
            zeros = zeros + 1 if byte == 0 else 0

        def box(name, content):
            return struct.pack(">I4s", len(content) + 8, name) + content

        for codec, header, config_box, config_length in (
            ("avc", b"\x06", b"avcC", 5), ("hevc", b"\x4e\x01", b"hvcC", 22)
        ):
            with self.subTest(codec=codec):
                nal = header + bytes((5, len(escaped) + 16)) + b"B" * 15 + b"i" + escaped + b"\x80"
                sample = struct.pack(">I", len(nal)) + nal
                mdat = box(b"mdat", sample * 2)
                stbl = box(b"stsd", box(config_box, bytes(config_length - 1) + b"\x03"))
                stbl += box(b"stsz", struct.pack(">III", 0, len(sample), 2))
                stbl += box(b"co64", struct.pack(">IIQ", 0, 1, 8))
                stbl += box(b"stsc", struct.pack(">IIIII", 0, 1, 1, 2, 1))
                moov = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stbl)))))
                path = self.root / (codec + ".mp4")
                path.write_bytes(mdat + moov)
                with patch.object(self.runtime.transcode, "probe_duration", return_value=2):
                    data = telemetry.parse_telemetry(str(path), stride=1)
                self.assertEqual(data["frames"], [
                    [i, "D", 72.0, 37.5, -18.5, 1, 3, 2, 31.25, 121.5, 180.0, 0.5, -0.75, 9.8, 1, "42"]
                    for i in (0, 1)
                ])

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

    def test_live_sync_recovers_recording_in_partitioned_exfat(self):
        sector = 512
        volume = bytearray(32 * sector)
        volume[3:11] = b"EXFAT   "
        struct.pack_into("<QQIIIIII", volume, 64, 2048, 32, 1, 1, 2, 30, 2, 0)
        volume[108:111] = bytes((9, 0, 1))
        struct.pack_into("<I", volume, sector + 2 * 4, 0xFFFFFFFF)

        def entry(name, cluster, length, directory=False):
            encoded = name.encode("utf-16le")
            names = [encoded[i:i + 30] for i in range(0, len(encoded), 30)]
            data = bytearray(32 * (2 + len(names)))
            data[0:2] = bytes((0x85, 1 + len(names)))
            struct.pack_into("<H", data, 4, 0x10 if directory else 0)
            data[32:34] = bytes((0xC0, 3))
            data[35] = len(name)
            struct.pack_into("<Q", data, 40, length)
            struct.pack_into("<IQ", data, 52, cluster, length)
            for i, part in enumerate(names, 2):
                data[32 * i] = 0xC1
                data[32 * i + 2:32 * i + 2 + len(part)] = part
            struct.pack_into("<H", data, 2, self.runtime.exfat.ExFat._set_checksum(data))
            return data

        event = "2026-11-14_16-00-00"
        clip = b"completed recording"
        event_dir = bytes((0x05,)) * sector + entry("front.mp4", 7, len(clip))
        for cluster, data in [(2, entry("TeslaCam", 3, sector, True)),
                              (3, entry("SavedClips", 4, sector, True)),
                              (4, entry(event, 5, sector * 2, True)),
                              (5, event_dir), (7, clip)]:
            volume[cluster * sector:cluster * sector + len(data)] = data

        def disk_image(layout):
            if layout == "raw":
                return volume
            first_lba = 2048
            disk = bytearray(first_lba * sector)
            disk[510:512] = b"\x55\xaa"
            if layout == "mbr":
                disk[450] = 0x07
                struct.pack_into("<II", disk, 454, first_lba, 32)
            else:
                import zlib
                disk[450] = 0xEE
                struct.pack_into("<II", disk, 454, 1, first_lba + 31)
                table = bytearray(4 * 128)
                table[0:16] = bytes.fromhex("a2a0d0ebe5b9334487c068b6b72699c7")
                table[16:32] = bytes(range(16))
                struct.pack_into("<QQ", table, 32, first_lba, first_lba + 31)
                header = bytearray(92)
                header[:8] = b"EFI PART"
                struct.pack_into("<IIIIQQQQ", header, 8, 0x10000, len(header), 0, 0,
                                 1, first_lba + 33, 34, first_lba + 31)
                struct.pack_into("<QIII", header, 72, 2, 4, 128, zlib.crc32(table))
                struct.pack_into("<I", header, 16, zlib.crc32(header))
                disk[sector:sector + len(header)] = header
                disk[2 * sector:3 * sector] = table
                return disk + volume + bytearray(2 * sector)
            return disk + volume

        for layout in ("raw", "mbr", "gpt"):
            with self.subTest(layout=layout):
                path = (self.root / (layout + ".img")).resolve()
                path.write_bytes(disk_image(layout))
                dest = self.archive / "SavedClips" / event / "front.mp4"
                if dest.exists():
                    dest.unlink()
                with self.runtime.exfat.ExFat(str(path)) as reader, \
                     patch.object(self.runtime.sync, "save_state"), \
                     patch.object(self.runtime.sync, "log"):
                    state = {}
                    self.assertEqual(self.runtime.sync.do_sync(reader, state), (0, 0))
                    self.assertEqual(state["health"]["phase"], "waiting")
                    self.assertEqual(self.runtime.sync.do_sync(reader, state), (1, len(clip)))
                    self.assertEqual(state["health"]["phase"], "ready")
                    self.assertEqual(state["health"]["last_copied"], 1)
                self.assertEqual(dest.read_bytes(), clip)
                usb = self.runtime.usb
                gadget = self.root / "gadget"
                lun = gadget / "functions/mass_storage.0/lun.0/file"
                lun.parent.mkdir(parents=True, exist_ok=True)
                lun.write_text(str(path))
                udc = gadget / "UDC"
                udc.write_text("controller")
                with patch.object(usb, "IMG", str(path)), patch.object(usb, "MNT", str(self.root / "mount")), \
                     patch.object(usb, "_lun", return_value=lun), \
                     patch.object(usb, "_command", side_effect=["/dev/loop4", "", "", ""]) as run:
                    mount = usb.UsbVolume()
                    mount.open()
                    self.assertEqual(lun.read_text(), "\n")
                    args = [] if layout == "raw" else ["-o", str(2048 * sector)]
                    run.assert_any_call("连接 U 盘镜像失败", usb.LOSETUP, *args, "/dev/loop4", str(path))
                    run.assert_any_call("挂载 exFAT 镜像失败", "mount", "-t", "exfat", "-o", "rw",
                                        "/dev/loop4", str(self.root / "mount"))
                    mount.close()
                    self.assertEqual(lun.read_text().strip(), str(path))
                    self.assertEqual(udc.read_text(), "controller")

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
        assets = self.runtime.assets
        self.assertEqual([code for code, _ in assets.LANG_NAMES], ["zh", "en"])
        self.assertEqual(set(assets.LANG_TABLE), {"en"})
        self.assertEqual(assets.lang_from_accept("de-DE,zh-CN;q=0.8,en;q=0.6"), "zh")
        self.assertEqual(assets.lang_from_accept("ja-JP"), "en")


if __name__ == "__main__":
    unittest.main()
