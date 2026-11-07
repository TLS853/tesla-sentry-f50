"""Mock QQ at the network boundary; never send messages to a real account."""
import datetime
import hashlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import test_regressions as fixtures
import test_runtime as http_fixtures


class Response(io.BytesIO):
    status = 200


class QQBotTests(unittest.TestCase):
    server = http_fixtures.RuntimeTests.server
    request = http_fixtures.RuntimeTests.request

    def setUp(self):
        fixtures.RegressionTests.setUp(self)
        self.qq = self.runtime.qqbot
        self.conf = {"provider": "qq", "qq_app_id": "12345", "qq_app_secret": "test-secret-A",
                     "qq_openid": "A" * 32, "qq_target_type": "user", "media": "text"}

    def test_proactive_messages_cache_tokens_by_both_credentials_and_target(self):
        calls = []

        def transport(request, **kwargs):
            calls.append(request)
            if request.full_url.endswith("/getAppAccessToken"):
                secret = json.loads(request.data)["clientSecret"]
                return {"access_token": "token-" + secret, "expires_in": "7200"}
            self.assertNotIn("msg_id", json.loads(request.data))
            self.assertNotIn("event_id", json.loads(request.data))
            return {"id": "message-id"}
        with patch.object(self.qq, "_request", side_effect=transport):
            self.qq.send(self.conf, "first")
            self.qq.send(self.conf, "second")
            other = dict(self.conf, qq_app_secret="test-secret-B", qq_target_type="group")
            self.qq.send(other, "group", "file-info")
        self.assertEqual(len([r for r in calls if r.full_url.endswith("/getAppAccessToken")]), 2)
        self.assertEqual(calls[0].full_url, "https://api.bot.qq.com/app/getAppAccessToken")
        self.assertIn("/users/", calls[1].full_url)
        self.assertIn("/groups/", calls[-1].full_url)
        self.assertEqual(calls[1].get_header("Authorization"), "QQBot token-test-secret-A")
        self.assertEqual(calls[-1].get_header("Authorization"), "QQBot token-test-secret-B")
        self.assertEqual(json.loads(calls[-1].data)["media"], {"file_info": "file-info"})
        self.assertNotIn(self.conf["qq_app_secret"], " ".join(self.qq._TOKENS))

    def test_unauthorized_token_is_refreshed_once_without_retrying_other_errors(self):
        requests = []

        def transport(request, **kwargs):
            requests.append(request)
            if request.full_url.endswith("/getAppAccessToken"):
                return {"access_token": "token%d" % len(requests), "expires_in": 7200}
            if len(requests) == 2:
                raise self.qq.QQError("expired", 401)
            return {"id": "delivered"}
        with patch.object(self.qq, "_request", side_effect=transport):
            self.assertEqual(self.qq.send(self.conf, "hello"), "delivered")
        self.assertEqual(len(requests), 4)
        with patch.object(self.qq, "_request", side_effect=self.qq.QQError("limited", 429)) as transport:
            with self.assertRaises(self.qq.QQError):
                self.qq.send(self.conf, "hello")
            self.assertEqual(transport.call_count, 1)

    def test_business_errors_in_http_200_fail_and_never_return_raw_secrets(self):
        opener = Mock()
        opener.open.return_value = Response(json.dumps({"err_code": 100016, "message": "bad " + self.conf["qq_app_secret"]}).encode())
        with patch.object(self.qq.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(self.qq.QQError) as raised:
                self.qq._request(self.qq.urllib.request.Request(self.qq.API + "/app/getAppAccessToken", data=b"{}"))
        self.assertIn("100016", str(raised.exception))
        self.assertNotIn(self.conf["qq_app_secret"], str(raised.exception))
        self.assertFalse(self.qq.ready(dict(self.conf, qq_openid="123456789")))
        self.assertFalse(self.qq.ready(dict(self.conf, qq_openid="../../anything")))

    def test_local_upload_checksums_parts_and_no_token_to_storage(self):
        video = self.root / "private-event.mp4"
        payload = b"abcdefghij"
        video.write_bytes(payload)
        calls, puts = [], []

        def api(conf, path, body):
            calls.append((path, body))
            if path.endswith("upload_prepare"):
                return {"upload_id": "upload-1", "block_size": "4", "parts": [
                    {"index": index, "presigned_url": "https://storage.example/part%d" % index} for index in (2, 0, 1)]}
            if path.endswith("/files"):
                return {"file_info": "opaque-info"}
            return {}

        def put(request, **kwargs):
            puts.append(request)
            return Response()
        opener = Mock()
        opener.open.side_effect = put
        with patch.object(self.qq, "_api", side_effect=api), patch.object(self.qq.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.qq.upload(self.conf, str(video), 2), "opaque-info")
        self.assertEqual(calls[0][1]["file_size"], "10")
        self.assertEqual(calls[0][1]["md5"], hashlib.md5(payload).hexdigest())
        self.assertEqual(calls[0][1]["sha1"], hashlib.sha1(payload).hexdigest())
        self.assertEqual(calls[0][1]["md5_10m"], hashlib.md5(payload).hexdigest())
        self.assertEqual([request.data for request in puts], [b"abcd", b"efgh", b"ij"])
        for request in puts:
            self.assertEqual(request.get_method(), "PUT")
            self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual([body["block_size"] for path, body in calls if path.endswith("upload_part_finish")], ["4", "4", "2"])
        self.assertEqual(calls[-1][1], {"file_type": 2, "file_name": "sentry.mp4", "upload_id": "upload-1", "srv_send_msg": False})

    def test_upload_rejects_invalid_part_urls_and_missing_parts(self):
        source = self.root / "image.jpg"
        source.write_bytes(b"abcd")
        for prepared in ({"upload_id": "u", "block_size": 4, "parts": [{"index": 0, "presigned_url": "http://storage.example/x"}]},
                         {"upload_id": "u", "block_size": 2, "parts": [{"index": 0, "presigned_url": "https://storage.example/x"}]}):
            with patch.object(self.qq, "_api", return_value=prepared), patch.object(self.qq.urllib.request, "build_opener") as opener:
                with self.assertRaises(self.qq.QQError):
                    self.qq.upload(self.conf, str(source), 1)
                opener.assert_not_called()

    def test_http_config_retains_secrets_and_qq_test_needs_no_recordings(self):
        server = self.server()
        status, _, response = self.request(server, "POST", "/api/notify/save", json.dumps(self.conf), {"Content-Type": "application/json"})
        self.assertEqual(status, 200, response)
        saved = json.loads(response)
        self.assertTrue(saved["status"]["qq_has_secret"])
        self.assertTrue(saved["status"]["ready"])
        self.assertNotIn(self.conf["qq_app_secret"].encode(), response)
        self.request(server, "POST", "/api/notify/save", json.dumps({"qq_app_secret": "", "delay": 30}), {"Content-Type": "application/json"})
        self.assertEqual(self.runtime.notifications.notify_conf()["qq_app_secret"], self.conf["qq_app_secret"])
        self.assertEqual(os.stat(self.runtime.notifications.NOTIFY_CONF).st_mode & 0o777, 0o600)
        with patch.object(self.qq, "send", return_value="test-message") as send, patch.object(self.runtime.feishu, "_feishu_send_dm") as feishu_send:
            status, _, response = self.request(server, "POST", "/api/notify/test")
            self.assertEqual(status, 200, response)
            self.assertTrue(json.loads(response)["ok"])
            self.assertIn("[测试]", send.call_args.args[1])
            feishu_send.assert_not_called()
        status, _, response = self.request(server, "GET", "/api/notify")
        self.assertNotIn(self.conf["qq_app_secret"].encode(), response)
        status, _, _ = self.request(server, "POST", "/api/notify/save", '{"provider":"unknown"}', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_qq_media_failure_falls_back_to_text_and_manual_video_uses_qq(self):
        notify = self.runtime.notifications
        notify.notify_save(dict(self.conf, media="video"))
        with patch.object(notify, "_pick_clip", return_value=(None, 0, {})), patch.object(self.qq, "send", return_value="id") as send:
            ok, message = notify.notify_push("SentryClips", "2026-11-08_00-00-00")
            self.assertTrue(ok)
            self.assertIn("已发文字", message)
            self.assertIsNone(send.call_args.args[2])
        with patch.object(notify.push_media, "_transcode_push_upload", return_value=("video.mp4", 12)), \
             patch.object(self.qq, "upload", return_value="info") as upload, patch.object(self.qq, "send", return_value="id") as send:
            self.assertTrue(notify.manual_push(self.conf, "source.webm", "SentryClips", "event")[0])
            upload.assert_called_once_with(self.conf, "video.mp4", 2)
            self.assertEqual(send.call_args.args[2], "info")

    def test_scheduler_uses_qq_readiness_and_keeps_first_scan_baseline(self):
        notify = self.runtime.notifications
        notify.notify_save(dict(self.conf, enabled=True))
        now = notify.time.time()
        base = self.archive / "SentryClips"
        base.mkdir()
        with patch.object(notify, "notify_push", return_value=(True, "sent")) as send:
            self.assertEqual(notify.notify_scan(), 0)
            send.assert_not_called()
            name = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H-%M-%S")
            event = base / name
            event.mkdir()
            (event / "event.json").write_text('{}')
            os.utime(event / "event.json", (now - 40, now - 40))
            self.assertEqual(notify.notify_scan(), 1)
            self.assertEqual(notify.notify_scan(), 0)
            send.assert_called_once_with("SentryClips", name)

    def test_notification_metadata_and_clips_cannot_escape_archive(self):
        event = self.archive / "SentryClips/2026-11-08_00-00-00"
        event.mkdir(parents=True)
        outside = self.root / "private.json"
        outside.write_text('{"city":"private-secret","reason":"private-secret"}')
        (event / "event.json").symlink_to(outside)
        (event / "front.mp4").symlink_to(outside)
        notify = self.runtime.notifications
        self.assertNotIn("private-secret", notify._event_text(self.conf, "SentryClips", event.name))
        self.assertEqual(notify._pick_clip("SentryClips", event.name), (None, 0.0, {}))
