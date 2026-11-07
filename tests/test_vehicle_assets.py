"""Vehicle file validation, real audio conversion and queue synchronization."""
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import unittest
import wave
import zlib
from unittest.mock import patch

import test_regressions as fixtures
import test_runtime as http_fixtures


def wav(seconds=1, channels=1, rate=44100):
    result = io.BytesIO()
    with wave.open(result, "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(b"\x10\x00" * channels * int(seconds * rate))
    return result.getvalue()


def png(size=512):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\x88" * size) * size)) + chunk(b"IEND", b""))


def multipart(name, data, fields=None):
    boundary = b"tsf-vehicle-upload-boundary"
    body = bytearray()
    for key, value in (fields or {}).items():
        body.extend(b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + key.encode() + b'"\r\n\r\n' + str(value).encode() + b"\r\n")
    body.extend(b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="' + name.encode() + b'"\r\nContent-Type: application/octet-stream\r\n\r\n' + data + b"\r\n--" + boundary + b"--\r\n")
    return bytes(body), {"Content-Type": "multipart/form-data; boundary=" + boundary.decode()}


class VehicleAssetTests(unittest.TestCase):
    server = http_fixtures.RuntimeTests.server
    request = http_fixtures.RuntimeTests.request

    def setUp(self):
        fixtures.RegressionTests.setUp(self)
        self.assets = self.runtime.vehicle_assets
        self.mount = self.root / "usb"
        self.mount.mkdir()

    def test_normalized_wav_trims_and_queues_without_ffmpeg(self):
        with patch.object(self.runtime.media, "FFMPEG_BIN", "/no-such-ffmpeg"):
            data, duration = self.assets.lock_chime("自定义.wav", wav(7), start=1, duration=5)
        self.assertEqual(duration, 5)
        self.assertLess(len(data), 1000000)
        with wave.open(io.BytesIO(data)) as sound:
            self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()), (1, 2, 44100))
        self.assets.queue_chime(data)
        self.assertEqual((self.archive / "boombox-queue/LockChime.wav").read_bytes(), data)
        self.assertEqual(self.assets.flush_audio(str(self.mount)), 1)
        self.assertEqual((self.mount / "LockChime.wav").read_bytes(), data)
        self.assertFalse((self.archive / "boombox-queue/LockChime.wav").exists())
        self.assertEqual((self.archive / "boombox-done/LockChime.wav").read_bytes(), data)

    @unittest.skipUnless(shutil.which("ffmpeg"), "real conversion requires ffmpeg")
    def test_real_large_stereo_wav_and_mp3_conversion(self):
        source = wav(7, channels=2, rate=48000)
        self.assertGreater(len(source), 1000000)
        for name, encoded in (("recording.wav", source), ("recording.mp3", subprocess.run([
                shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-f", "mp3", "pipe:1"], input=source, capture_output=True, check=True).stdout)):
            data, duration = self.assets.lock_chime(name, encoded, start=0.5, duration=4.5)
            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual(duration, 4.5)
            self.assertLess(len(data), 1000000)
            with wave.open(io.BytesIO(data)) as sound:
                self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()), (1, 2, 44100))

    def test_invalid_audio_and_missing_encoder_preserve_existing_queue(self):
        self.assets.queue_chime(wav())
        before = (self.archive / "boombox-queue/LockChime.wav").read_bytes()
        for start, duration in (("nan", 5), (0, "inf"), (0, 6), (-1, 5), (0, 0)):
            with self.assertRaises(ValueError):
                self.assets.lock_chime("sound.wav", wav(), start, duration)
        with patch.object(self.runtime.media, "FFMPEG_BIN", "/no-such-ffmpeg"):
            with self.assertRaisesRegex(ValueError, "ffmpeg"):
                self.assets.lock_chime("sound.mp3", b"fake compressed audio")
        self.assertEqual((self.archive / "boombox-queue/LockChime.wav").read_bytes(), before)

    def test_failed_usb_copy_leaves_queue_and_existing_usb_file_intact(self):
        self.assets.queue_chime(wav())
        target = self.mount / "LockChime.wav"
        target.write_bytes(b"previous")
        real_write = self.runtime.storage.atomic_write

        def fail_usb(path, *args, **kwargs):
            if path == str(target):
                raise OSError("simulated full USB")
            return real_write(path, *args, **kwargs)
        with patch.object(self.runtime.storage, "atomic_write", side_effect=fail_usb):
            self.assertEqual(self.assets.flush_audio(str(self.mount)), 0)
        self.assertEqual(target.read_bytes(), b"previous")
        self.assertTrue((self.archive / "boombox-queue/LockChime.wav").exists())

    def test_upload_during_flush_cannot_be_removed_by_older_sync(self):
        old, new = wav(), wav(2)
        self.assets.queue_chime(old)
        copying, release, uploaded = threading.Event(), threading.Event(), threading.Event()
        errors = []
        real_write = self.runtime.storage.atomic_write

        def slow_write(path, *args, **kwargs):
            if path == str(self.mount / "LockChime.wav"):
                copying.set()
                if not release.wait(3):
                    raise RuntimeError("test synchronization timed out")
            return real_write(path, *args, **kwargs)

        def flush():
            try:
                self.assets.flush_audio(str(self.mount))
            except BaseException as error:
                errors.append(error)

        def upload():
            try:
                self.assets.queue_chime(new)
                uploaded.set()
            except BaseException as error:
                errors.append(error)
        with patch.object(self.runtime.storage, "atomic_write", side_effect=slow_write):
            first = threading.Thread(target=flush)
            second = threading.Thread(target=upload)
            first.start()
            try:
                self.assertTrue(copying.wait(2))
                second.start()
                self.assertFalse(uploaded.wait(0.1))
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(uploaded.is_set())
        self.assertEqual((self.mount / "LockChime.wav").read_bytes(), old)
        self.assertEqual((self.archive / "boombox-queue/LockChime.wav").read_bytes(), new)

    def test_wrap_validation_names_limit_and_explicit_overwrite(self):
        self.assertEqual(self.assets.validate_wrap(png(1024)), (1024, 1024))
        name, _, _ = self.assets.write_wrap(str(self.mount), "我的涂装.png", png())
        self.assertRegex(name, r"^[A-Za-z0-9 _-]+\.png$")
        self.assertLessEqual(len(name), 30)
        with self.assertRaises(FileExistsError):
            self.assets.write_wrap(str(self.mount), "我的涂装.png", png())
        self.assets.write_wrap(str(self.mount), "我的涂装.png", png(1024), overwrite=True)
        for n in range(9):
            self.assets.write_wrap(str(self.mount), "wrap%d.png" % n, png())
        with self.assertRaisesRegex(ValueError, "10"):
            self.assets.write_wrap(str(self.mount), "eleventh.png", png())
        self.assets.write_wrap(str(self.mount), "WRAP0.PNG", png(), overwrite=True)
        self.assertEqual(len(list((self.mount / "Wraps").iterdir())), 10)

    def test_bad_wraps_and_symlink_directory_are_rejected_without_writes(self):
        for data in (b"JPEG renamed as PNG", png(256), png()[:-4], png() + b"trailing", png()[:40] + b"broken", b"x" * 1000001):
            with self.assertRaises(ValueError):
                self.assets.write_wrap(str(self.mount), "invalid.png", data)
        self.assertFalse((self.mount / "Wraps").exists())
        outside = self.root / "outside"
        outside.mkdir()
        (self.mount / "Wraps").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.assets.write_wrap(str(self.mount), "valid.png", png())
        self.assertEqual(list(outside.iterdir()), [])

    def test_http_audio_conversion_and_wrap_management_gate(self):
        server = self.server()
        body, headers = multipart("用户音频.wav", wav(7), {"start": 1, "duration": 3})
        status, _, response = self.request(server, "POST", "/api/upload/audio", body, headers)
        self.assertEqual(status, 200, response)
        self.assertEqual(json.loads(response)["duration"], 3)
        self.assertEqual(json.loads(response)["name"], "LockChime.wav")
        body, headers = multipart("custom.png", png())
        status, _, _ = self.request(server, "POST", "/api/upload/wrap", body, headers)
        self.assertEqual(status, 400)
        with patch.object(self.runtime.usb, "usb_attached", True), patch.object(self.runtime.usb, "MNT", str(self.mount)), \
             patch.object(self.runtime.usb.os.path, "ismount", return_value=True), patch.object(self.runtime.usb, "_arm_usb_timer"):
            for overwrite, expected in ((False, 200), (False, 409), (True, 200)):
                body, headers = multipart("custom.png", png(), {"overwrite": "1"} if overwrite else {})
                status, _, response = self.request(server, "POST", "/api/upload/wrap", body, headers)
                self.assertEqual(status, expected, response)
        self.assertTrue((self.mount / "Wraps/custom.png").exists())
