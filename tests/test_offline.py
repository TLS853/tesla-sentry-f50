"""Offline packaging and upload tests; never connect to a device or the internet."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import shlex
import unittest
from unittest.mock import Mock, patch

import test_regressions as fixtures

ROOT = fixtures.ROOT


class OfflineTests(unittest.TestCase):
    def setUp(self):
        fixtures.RegressionTests.setUp(self)

    def test_offline_update_check_does_not_make_network_requests(self):
        with patch.dict(self.runtime.config.environment, {"TSF_OTA_ENABLED": "0"}), \
             patch.object(self.runtime.updates, "_http_get") as network:
            with self.assertRaisesRegex(RuntimeError, "离线"):
                self.runtime.updates.ota_latest(force=True)
            network.assert_not_called()

    def test_online_default_points_to_the_published_branch(self):
        self.assertEqual(self.runtime.updates.OTA_REF, "codex/modular-runtime")
        self.assertIn("/codex/modular-runtime", self.runtime.updates.OTA_SOURCES[0])
        self.assertTrue(all("ghfast" not in url for url in self.runtime.updates.OTA_SOURCES))

    def test_offline_font_step_cannot_run_curl(self):
        script = (ROOT / "install.sh").read_text()
        start = script.index('if [ "$OFFLINE" = 1 ]; then')
        end = script.index('\nsay "启动查看器', start)
        block = script[start:end]
        executable = self.root / "bin"
        executable.mkdir()
        marker = self.root / "network-was-called"
        curl = executable / "curl"
        curl.write_text('#!/bin/sh\ntouch "$NETWORK_MARKER"\nexit 1\n')
        curl.chmod(0o755)
        env = dict(self.env, OFFLINE="1", NETWORK_MARKER=str(marker), PATH=str(executable) + ":" + os.environ["PATH"])
        result = subprocess.run(["/bin/sh", "-c", 'say() { echo "$*"; };\n' + block], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("跳过字体下载", result.stdout)
        self.assertFalse(marker.exists())

    def test_package_can_be_verified_and_imported_without_source_tree(self):
        package = runpy.run_path(str(ROOT / "tools/package_release.py"))["package"]
        output = package(output=self.root / "offline.tar.gz")
        data = output.read_bytes()
        again = package(output=self.root / "offline-again.tar.gz")
        self.assertEqual(data, again.read_bytes())
        with tarfile.open(output) as archive:
            self.assertTrue(all(not item.issym() and not item.islnk() for item in archive.getmembers()))
            archive.extractall(self.root / "unpack", filter="data")
        folder = next((self.root / "unpack").iterdir())
        verify = runpy.run_path(str(ROOT / "tools/verify_package.py"))["verify"]
        self.assertEqual(verify(folder), self.runtime.updates.APP_VERSION)
        self.assertFalse((folder / "src").exists())
        check = subprocess.run([sys.executable, str(folder / "sentry-viewer.py"), "--ota-check"],
                               env=self.env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(check.stdout.startswith("ok "))
        (folder / "install.sh").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "损坏"):
            verify(folder)

    def test_new_image_cannot_fill_a_small_system_partition(self):
        check = runpy.run_path(str(ROOT / "tools/check_storage.py"))["check"]
        with patch("os.statvfs", return_value=Mock(f_bavail=512 * 1024, f_frsize=1024)):
            with self.assertRaisesRegex(ValueError, "大容量持久化存储"):
                check(self.root / "system" / "teslacam.img", 64)
        image = self.root / "existing.img"
        image.write_bytes(b"existing image")
        with patch("os.statvfs", side_effect=AssertionError("must not require a second full image allocation")):
            self.assertIn("保留已有镜像", check(image, 64))
        self.assertEqual(image.read_bytes(), b"existing image")

    def test_mac_uploader_streams_an_archive_and_quotes_remote_arguments(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text('''#!/bin/sh
printf '%s\n' "$@" > "$UPLOAD_ARGUMENTS"
cat > "$UPLOADED_PACKAGE"
echo INSTALL_LOG=/tmp/tesla-sentry-deploy.test/install.log
''')
        ssh.chmod(0o755)
        captured, arguments = self.root / "uploaded.tar.gz", self.root / "arguments.txt"
        env = dict(self.env, PATH=str(bin_dir) + ":" + os.environ["PATH"], UPLOADED_PACKAGE=str(captured), UPLOAD_ARGUMENTS=str(arguments))
        result = subprocess.run(["/bin/sh", str(ROOT / "tools/deploy_f50.sh"), "root@192.0.2.1",
                                 "--archive", "/data/a path; touch NEVER"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(captured) as archive:
            self.assertTrue(any(item.name.endswith("/install-offline.sh") for item in archive.getmembers()))
        args = arguments.read_text()
        self.assertIn("root@192.0.2.1", args)
        self.assertIn("setsid sh ./install-offline.sh", args)
        self.assertIn("/data/a path; touch NEVER", args)
        command = "\n".join(args.splitlines()[3:])
        self.assertEqual(shlex.split(command)[-2:], ["--archive", "/data/a path; touch NEVER"])
        self.assertIn("INSTALL_LOG", result.stdout)
        self.assertFalse((ROOT / "NEVER").exists())

    def test_installer_configuration_quotes_paths_as_literal_data(self):
        merge = runpy.run_path(str(ROOT / "tools/merge_config.py"))["merge"]
        marker = self.root / "not-a-command"
        value = "/data/a path; touch " + str(marker)
        conf = self.root / "settings.conf"
        conf.write_text(merge("", "TSF_ARCHIVE=" + value + "\n"))
        result = subprocess.run(["/bin/sh", "-c", '. "$1"; printf "%s" "$TSF_ARCHIVE"', "sh", str(conf)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, value)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
