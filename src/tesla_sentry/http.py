"""Http service."""
from http.server import BaseHTTPRequestHandler
import base64
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from . import archive, assets, auth, config, http_get, http_post, media, usb, routes
import logging

class Handler(http_get.GetRoutesMixin, http_post.PostRoutesMixin, BaseHTTPRequestHandler):
    timeout = 30

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _post_login(self, path):
        if not auth.AUTH_ON:
            self._send(404, b"not found")
            return
        self._do_login()

    def _dispatch(self, method):
        self._response_started = False
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if not self._gate():
            return
        try:
            if method == "GET" and path.startswith("/assets/"):
                return self._asset(path)
            handler = routes.EXACT.get((method, path))
            if handler is None:
                handler = next((name for (verb, prefix), name in routes.PREFIX.items()
                                if method == verb and path.startswith(prefix)), None)
            if handler is not None:
                return getattr(self, handler)(path)
            if method == "GET":
                return self._get_media(path)
            if method == "DELETE":
                return self._delete(path)
            self._send(404, b"not found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ValueError:
            if not getattr(self, "_response_started", False):
                self._json({"ok": False, "msg": "参数错误"}, 400)
        except Exception:
            logging.getLogger(__name__).exception("Request failed: %s %s", method, path)
            if not getattr(self, "_response_started", False):
                self._json({"ok": False, "msg": "服务暂时不可用"}, 500)
            self.close_connection = True

    def send_response(self, *args, **kwargs):
        self._response_started = True
        return super().send_response(*args, **kwargs)

    def _asset(self, path):
        content_types = {"app.css": "text/css; charset=utf-8"}
        content_types.update({name: "text/javascript; charset=utf-8"
                              for name in ("app.js", "i18n.js", "theme.js", "model.js")})
        name = path[len("/assets/"):]
        if name not in content_types:
            self._send(404, b"not found")
            return
        self._send(200, assets.resource(name), content_types[name])

    def log_message(self, fmt, *args):
        pass

    # ---- 认证 ----
    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _authed(self):
        if not auth.AUTH_ON:
            return True
        if auth.sess_ok(self._cookie("tsf_sess")):
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode("utf-8", "replace").partition(":")
            except Exception:
                return False
            return u == auth.AUTH_USER and auth.pw_ok(p)
        return False

    def _login_page(self, msg="", nxt="/", code=200):
        lang = assets.lang_from_accept(self.headers.get("Accept-Language") or "", "zh")
        days = str(max(1, auth.AUTH_TTL // 86400))
        foot = assets.tr("仅限本人访问", lang) + " · " + assets.tr("登录状态保留", lang) + " " + days + " " + assets.tr("天", lang)
        html = (assets.LOGIN_HTML.replace("__USER__", auth.AUTH_USER)
                .replace("__NEXT__", nxt.replace('"', "&quot;"))
                .replace("__MSG__", assets.tr(msg, lang))
                .replace("__L_BRAND__", assets.tr("特斯拉哨兵云盘", lang))
                .replace("__L_USER__", assets.tr("用户名", lang))
                .replace("__L_PASS__", assets.tr("密码", lang))
                .replace("__L_LOGIN__", assets.tr("登录", lang))
                .replace("__L_FOOT__", foot))
        if lang != "zh":
            html = html.replace('<html lang="zh">', '<html lang="%s">' % lang)
        self._send(code, html.encode())

    def _do_login(self):
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            f = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            f = {}
        nxt = (f.get("next", ["/"])[0] or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        ip = self.client_address[0] if self.client_address else "?"
        if auth.fail_check(ip, False):
            self._login_page("尝试次数过多，请 5 分钟后再试", nxt, 429)
            return
        if f.get("u", [""])[0] == auth.AUTH_USER and auth.pw_ok(f.get("p", [""])[0]):
            auth.fail_check(ip, False)
            with auth.FAIL_LOCK:
                auth.FAILS.pop(ip, None)
            exp = int(time.time()) + auth.AUTH_TTL
            self.send_response(302)
            self.send_header("Location", nxt)
            self.send_header("Set-Cookie",
                             "tsf_sess=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (auth.sess_token(exp), auth.AUTH_TTL))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        auth.fail_check(ip, True)
        time.sleep(0.4)          # 轻微延迟, 拖慢暴力破解
        self._login_page("用户名或密码不对", nxt, 401)

    def _gate(self):
        """未登录: 页面跳登录页, 媒体/接口直接 401。返回 True 表示可继续"""
        if not auth.AUTH_ON:
            return True
        p = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if p in ("/login", "/logout"):
            return True
        if self._authed():
            return True
        if p in ("/", "/index.html") or "text/html" in (self.headers.get("Accept") or ""):
            self.send_response(302)
            self.send_header("Location", "/login?next=" + urllib.parse.quote(self.path or "/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(401, "unauthorized".encode(), "text/plain; charset=utf-8")
        return False

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _read_upload(self):
        """解析 multipart: 返回 (文件名, 文件数据, 普通字段dict)"""
        ctype = self.headers.get("Content-Type", "")
        mm = re.search(r'boundary=([^;]+)', ctype)
        if "multipart/form-data" not in ctype or not mm:
            return None, None, {}
        boundary = mm.group(1).strip('"').encode()
        length = int(self.headers.get("Content-Length", 0))
        if length > config.MAX_UPLOAD:
            return "TOOBIG", None, {}
        body = self.rfile.read(length)
        fields = {}
        file_fn = None
        file_data = None
        for part in body.split(b"--" + boundary):
            i = part.find(b"\r\n\r\n")
            if i == -1:
                continue
            headers = part[:i].decode("utf-8", "replace")
            data = part[i + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            nm = re.search(r'name="([^"]*)"', headers)
            if 'filename="' in headers:
                fn = re.search(r'filename="([^"]*)"', headers)
                if fn and fn.group(1) and data:
                    file_fn, file_data = fn.group(1), data
            elif nm and nm.group(1):
                fields[nm.group(1)] = data.decode("utf-8", "replace").strip()
        return file_fn, file_data, fields


    def _serve_thumb(self, path):
        """列表缩略图: 用 ffmpeg 抽指定秒数一帧 (?t=秒, 默认 1), 结果按 (路径+大小+mtime+t) 哈希缓存在 eMMC"""
        parts = path[len("/thumb/"):].split("/")
        if len(parts) != 3:
            self._send(404, b"not found")
            return
        cat, name, fn = [urllib.parse.unquote(p) for p in parts]
        evdir = archive.safe_archive_dir(os.path.join(config.settings.archive, cat, name))
        fn = archive.safe_name(fn)
        if not evdir or not fn:
            self._send(404, b"not found")
            return
        fp = archive.safe_archive_path(os.path.join(evdir, fn))
        if not fp:
            self._send(404, b"not found")
            return
        try:
            st = os.stat(fp)
        except OSError:
            self._send(404, b"not found")
            return
        try:
            ss_t = max(0.0, min(float(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t", ["1"])[0]), 3600.0))
        except ValueError:
            ss_t = 1.0
        out = media.thumb_file(fp, ss_t)
        if not out:
            self._send(404, b"no thumb")
            return
        try:
            with open(out, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


    def _delete(self, path):
        if path.startswith("/api/skin/"):
            name = archive.safe_name(path[len("/api/skin/"):])
            fp = os.path.join(config.settings.skins_dir, name) if name else ""
            if name and os.path.isfile(fp):
                os.remove(fp)
                if media.active_skin() == name:
                    os.remove(os.path.join(config.settings.skins_dir, "active.json"))
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        elif path.startswith("/api/audio/"):
            name = archive.safe_name(path[len("/api/audio/"):].split("?")[0])
            done = "done=1" in self.path
            base = config.settings.audio_done if done else config.settings.audio_queue
            fp = os.path.join(base, name) if name else ""
            if name and os.path.isfile(fp) and name != "LockChime.wav":
                os.remove(fp)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        elif path.startswith("/api/usb"):
            return self._delete_usb()
        elif path.startswith("/api/file"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fp = archive.safe_archive_path(q.get("path", [""])[0])
            if fp:
                os.remove(fp)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "not found"}, 404)
        else:
            self._send(404, b"not found")


    @usb.usb_operation
    def _delete_usb(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        fp = usb.usb_real(q.get("path", [""])[0])
        if not fp or not os.path.isfile(fp) or fp == usb.MNT:
            self._json({"ok": False, "msg": "not found"}, 404)
            return
        os.remove(fp)
        subprocess.run(["sync"], capture_output=True)
        self._json({"ok": True})
