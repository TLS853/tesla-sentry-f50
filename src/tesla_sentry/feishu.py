"""Feishu transport, token cache and messaging API."""
import hashlib
import http.client
import json
import os
import ssl
import time
import threading
import hashlib
import urllib.request

FEISHU_API = "https://open.feishu.cn/open-apis"


_TOK = {"v": "", "exp": 0}


def _post_json(url, payload, timeout=12, headers=None):
    """POST JSON 并解析返回。
    注意: 这里用 http.client 而不是 urllib — urllib 会给请求带上 Connection: close,
    飞书网关对这种短连接 POST 会直接断链 (SSL BAD_RECORD_MAC), 实测 http.client 正常。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    return json.loads(_http_post(url, body, h, timeout).decode("utf-8", "replace"))


def _tls12():
    """给飞书 API 用的 SSLContext: 强制 TLS 1.2。
    实测在 F50 的 4G 链路上 TLS 1.3 会随机丢/坏记录 (SSLV3_ALERT_BAD_RECORD_MAC, 约 1/3 概率),
    同一请求降到 TLS 1.2 后 100% 成功, 所以对飞书固定用 1.2。"""
    global _TLS_CTX
    if _TLS_CTX is None:
        c = ssl.create_default_context()
        try:
            c.maximum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            pass
        _TLS_CTX = c
    return _TLS_CTX


_TLS_CTX = None


def _http_post(url, body, headers=None, timeout=12):
    """发一个 POST, 返回响应体 bytes; HTTP 4xx/5xx 也照常返回内容 (里面有飞书的错误码)
    TLS 抖动会自动重试 3 次 (每次新建连接)。"""
    u = urllib.parse.urlsplit(url)
    last = None
    for attempt in range(3):
        conn = None
        try:
            if u.scheme == "https":
                conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                                   context=_tls12())
            elif u.scheme == "http":
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            else:
                raise RuntimeError("不支持的地址: %s" % url[:40])
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            conn.request("POST", path, body=body, headers=dict(headers or {}))
            r = conn.getresponse()
            return r.read() or b"{}"
        except (ssl.SSLError, ConnectionError, OSError) as ex:
            last = ex
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
    raise last if last else RuntimeError("POST 失败")


_TOKEN_LOCK = threading.Lock()


def _feishu_token(conf):
    identity = hashlib.sha256((str(conf.get("app_id", "")) + "\0" + str(conf.get("app_secret", ""))).encode()).hexdigest()
    with _TOKEN_LOCK:
        if _TOK.get("identity") != identity:
            _TOK.update(identity=identity, v="", exp=0)
        return _request_token(conf)


def _request_token(conf):
    if _TOK["v"] and _TOK["exp"] > time.time() + 60:
        return _TOK["v"]
    j = _post_json(FEISHU_API + "/auth/v3/tenant_access_token/internal",
                   {"app_id": conf.get("app_id", ""), "app_secret": conf.get("app_secret", "")})
    if j.get("code") != 0 or not j.get("tenant_access_token"):
        raise RuntimeError("取 token 失败: %s" % (j.get("msg") or j))
    _TOK["v"] = j["tenant_access_token"]
    _TOK["exp"] = time.time() + max(0, int(j.get("expire") or 7200) - 60)
    return _TOK["v"]


def _feishu_upload(conf, jpg):
    """上传封面图, 返回 image_key (失败抛异常)"""
    with open(jpg, "rb") as f:
        data = f.read()
    bd = "----tsf%s" % hashlib.md5(("%s%.6f" % (jpg, time.time())).encode()).hexdigest()
    pre = ("--%s\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n"
           "--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"cover.jpg\"\r\n"
           "Content-Type: image/jpeg\r\n\r\n" % (bd, bd)).encode()
    body = pre + data + ("\r\n--%s--\r\n" % bd).encode()
    raw = _http_post(FEISHU_API + "/im/v1/images", body, {
        "Authorization": "Bearer " + _feishu_token(conf),
        "Content-Type": "multipart/form-data; boundary=" + bd,
    }, timeout=30)
    j = json.loads(raw.decode("utf-8", "replace"))
    key = ((j or {}).get("data") or {}).get("image_key")
    if j.get("code") != 0 or not key:
        raise RuntimeError("上传封面失败: %s" % (j.get("msg") or j))
    return key


def _feishu_upload_file(conf, path, dur=30):
    """上传视频文件到飞书, 返回 file_key"""
    with open(path, "rb") as f:
        data = f.read()
    bd = "----tsf%s" % hashlib.md5(("%s%.6f" % (path, time.time())).encode()).hexdigest()

    def fld(k, v):
        return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bd, k, v)).encode()

    body = (fld("file_type", "mp4") + fld("file_name", os.path.basename(path))
            + fld("duration", str(int(max(1, dur) * 1000)))
            + ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.mp4\"\r\n"
               "Content-Type: video/mp4\r\n\r\n" % bd).encode()
            + data + ("\r\n--%s--\r\n" % bd).encode())
    raw = _http_post(FEISHU_API + "/im/v1/files", body, {
        "Authorization": "Bearer " + _feishu_token(conf),
        "Content-Type": "multipart/form-data; boundary=" + bd,
    }, timeout=180)
    j = json.loads(raw.decode("utf-8", "replace"))
    key = ((j or {}).get("data") or {}).get("file_key")
    if j.get("code") != 0 or not key:
        raise RuntimeError("上传视频失败: %s" % (j.get("msg") or j))
    return key


def _feishu_send_media(conf, file_key, image_key=""):
    """给本人发一条视频消息 (media)"""
    rid, rtype = _dm_target(conf)
    content = {"file_key": file_key}
    if image_key:
        content["image_key"] = image_key
    j = _post_json(FEISHU_API + "/im/v1/messages?receive_id_type=" + urllib.parse.quote(rtype),
                   {"receive_id": rid, "msg_type": "media", "content": json.dumps(content)},
                   timeout=60, headers={"Authorization": "Bearer " + _feishu_token(conf)})
    if j.get("code") != 0:
        raise RuntimeError("%s (code=%s)" % (j.get("msg") or "发视频失败", j.get("code")))
    return True


def _dm_target(conf):
    """返回 (receive_id, receive_id_type); 没配返回 (None, None)。
    自动识别: ou_/on_ → open_id, oc_ → chat_id, 含 @ → email"""
    rid = str(conf.get("receive_id") or "").strip()
    if not rid:
        return None, None
    t = str(conf.get("receive_id_type") or "").strip()
    if not t:
        if rid.startswith("oc_"):
            t = "chat_id"
        elif "@" in rid:
            t = "email"
        else:
            t = "open_id"
    return rid, t


def _dm_ready(conf):
    rid, _t = _dm_target(conf)
    return bool(rid and conf.get("app_id") and conf.get("app_secret"))


def _feishu_send_dm(conf, card):
    """用自建应用机器人给指定用户发卡片 — 只进你和机器人的单聊, 别人看不到"""
    rid, rtype = _dm_target(conf)
    url = FEISHU_API + "/im/v1/messages?receive_id_type=" + urllib.parse.quote(rtype)
    payload = {"receive_id": rid, "msg_type": "interactive",
               "content": json.dumps(card, ensure_ascii=False)}
    j = _post_json(url, payload, timeout=20,
                   headers={"Authorization": "Bearer " + _feishu_token(conf)})
    if j.get("code") != 0:
        raise RuntimeError("%s (code=%s)" % (j.get("msg") or "发送失败", j.get("code")))
    return True
