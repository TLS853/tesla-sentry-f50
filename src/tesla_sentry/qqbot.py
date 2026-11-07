"""QQ official OpenAPI: credential cache, proactive messages and private local uploads.

API reference: https://bot.q.qq.com/wiki/develop/api-v2/
No gateway, public media server or unofficial QQ login is required.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bot.qq.com"
_LOCK = threading.RLock()
_TOKENS = {}


class QQError(RuntimeError):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        # Do not expose raw responses: token errors can include credential material.
        super().__init__("QQ API 错误 (code=%s, HTTP=%s); 请检查应用凭据、OpenID、主动消息权限及平台频控" % (code, status))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward authorization or upload bytes across redirects.


def _request(request, timeout=30):
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(16384)
        error.close()
    except (OSError, urllib.error.URLError):
        raise RuntimeError("QQ 网络请求失败, 请检查 F50 到 QQ 官方接口的 HTTPS 连接")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeError):
        raise QQError("invalid_response", status)
    if not isinstance(data, dict) or len(raw) > 1024 * 1024:
        raise QQError("invalid_response", status)
    code = data.get("err_code", data.get("code", 0))
    if not 200 <= status < 300 or code not in (0, "0", None):
        raise QQError(code or "http_error", status)
    return data


def _key(conf):
    return hashlib.sha256((str(conf.get("qq_app_id") or "") + "\0" + str(conf.get("qq_app_secret") or "")).encode()).hexdigest()


def _token(conf):
    key = _key(conf)
    with _LOCK:
        cached = _TOKENS.get(key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        body = {"appId": conf.get("qq_app_id"), "clientSecret": conf.get("qq_app_secret")}
        result = _request(urllib.request.Request(API + "/app/getAppAccessToken",
                          data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"))
        token = str(result.get("access_token") or "")
        try:
            lifetime = max(1, min(7200, int(result.get("expires_in", 7200))))
        except (TypeError, ValueError):
            raise QQError("invalid_token_expiry")
        if not token or "\n" in token or "\r" in token:
            raise QQError("missing_token")
        if len(_TOKENS) >= 32:
            _TOKENS.clear()
        _TOKENS[key] = token, time.monotonic() + max(0, lifetime - 60)
        return token


def _target(conf):
    kind, openid = conf.get("qq_target_type", "user"), str(conf.get("qq_openid") or "").strip()
    if kind not in ("user", "group") or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", openid) or (openid.isdigit() and len(openid) < 20):
        raise ValueError("QQ 需要该机器人事件里的 user_openid / group_openid, 不能填 QQ 号码或群号")
    return "/v2/%s/%s" % ("users" if kind == "user" else "groups", urllib.parse.quote(openid, safe=""))


def ready(conf):
    if not conf.get("qq_app_id") or not conf.get("qq_app_secret"):
        return False
    try:
        _target(conf)
        return True
    except ValueError:
        return False


def _api(conf, path, body):
    if not ready(conf):
        raise ValueError("QQ 未配置完整: 需要 AppID / AppSecret 和接收 OpenID")
    for attempt in range(2):
        token = _token(conf)
        request = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "QQBot " + token}, method="POST")
        try:
            return _request(request, timeout=60)
        except QQError as error:
            if error.status != 401 or attempt:
                raise
            with _LOCK:
                if _TOKENS.get(_key(conf), (None,))[0] == token:
                    _TOKENS.pop(_key(conf), None)


def upload(conf, path, file_type):
    """QQ multipart upload; bounded RAM, no unauthenticated URLs back to the device."""
    size = os.path.getsize(path)
    limit = (20 if file_type == 1 else 30) * 1024 * 1024
    if file_type not in (1, 2) or not 0 < size <= limit:
        raise ValueError("QQ 图片须小于 20 MB, 视频须小于 30 MB")
    target = _target(conf)
    # Generated neutral names avoid leaking local paths or user-supplied filenames.
    filename = "sentry.jpg" if file_type == 1 else "sentry.mp4"
    md5, sha1, first = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    with open(path, "rb") as stream:
        remaining = 10002432
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            if remaining:
                first.update(chunk[:remaining])
                remaining = max(0, remaining - len(chunk))
        prepared = _api(conf, target + "/upload_prepare", {
            "file_type": file_type, "file_size": str(size), "file_name": filename,
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": first.hexdigest(),
        })
        upload_id, parts = prepared.get("upload_id"), prepared.get("parts") or []
        if not upload_id or not isinstance(parts, list):
            raise QQError("invalid_upload_prepare")
        # Empty parts are allowed for a server-side content hash hit.
        stream.seek(0)
        try:
            block_size = int(prepared.get("block_size") or 5 * 1024 * 1024)
            ordered = sorted(parts, key=lambda part: int(part["index"]))
            if not 0 < block_size <= 32 * 1024 * 1024:
                raise ValueError()
            if ordered and [int(p["index"]) for p in ordered] != list(range((size + block_size - 1) // block_size)):
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            raise QQError("invalid_upload_parts")
        for part in ordered:
            url = str(part.get("presigned_url") or "")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise QQError("invalid_upload_url")
            chunk = stream.read(block_size)
            # Signed storage receives ONLY bytes; the QQ access token stays on api.bot.qq.com.
            request = urllib.request.Request(url, data=chunk,
                headers={"Content-Type": "application/octet-stream"}, method="PUT")
            try:
                with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError()
            except (OSError, urllib.error.URLError, RuntimeError):
                raise RuntimeError("QQ 媒体分片上传失败, 请检查网络后重试")
            _api(conf, target + "/upload_part_finish", {"upload_id": upload_id, "part_index": int(part["index"]),
                 "block_size": str(len(chunk)), "md5": hashlib.md5(chunk).hexdigest()})
    result = _api(conf, target + "/files", {"file_type": file_type, "file_name": filename,
                  "upload_id": upload_id, "srv_send_msg": False})
    if not result.get("file_info"):
        raise QQError("missing_file_info")
    return result["file_info"]


def send(conf, content, file_info=None):
    body = {"msg_type": 7 if file_info else 0, "content": str(content)[:1800]}
    if file_info:
        body["media"] = {"file_info": file_info}
    result = _api(conf, _target(conf) + "/messages", body)
    if not result.get("id"):
        raise QQError("missing_message_id")
    return result["id"]
