"""Http Get service."""
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from . import archive, assets, auth, config, media, notifications, telemetry, transcode, updates, usb

class GetRoutesMixin:

    def _get_login(self, path):
        if not auth.AUTH_ON:
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self._login_page("", q.get("next", ["/"])[0])
        return


    def _get_logout(self, path):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "tsf_sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return


    def _get_home(self, path):
        html = (assets.HTML.replace("__CAT_NAMES__", json.dumps(config.CATEGORY_NAMES, ensure_ascii=False))
                .replace("__I18N__", assets.I18N_JS).replace("__LANGS__", assets.LANG_JS))
        self._send(200, html.encode())


    def _get_api_stitch_status(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        job = media.STITCH_JOBS.get(q.get("id", [""])[0])
        if not job:
            self._json({"state": "error", "err": "任务不存在或已过期"}, 404)
            return
        self._json({"state": job["state"], "pct": job.get("pct", 0),
                    "err": job.get("err", ""), "size": job.get("size", 0)})


    def _get_api_stitch_dl(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        jid = q.get("id", [""])[0]
        job = media.STITCH_JOBS.get(jid)
        if not job or job.get("state") != "done" or not os.path.isfile(job.get("path") or ""):
            self._send(404, b"not ready")
            return
        fp = job["path"]
        fname = job.get("name") or "stitch.mp4"
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % urllib.parse.quote(fname))
        self.end_headers()
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)
        with media.STITCH_LOCK:
            media.STITCH_JOBS.pop(jid, None)
        try:
            os.unlink(fp)
        except OSError:
            pass


    def _get_api_events(self, path):
        self._json(archive.list_events())


    def _get_api_live_status(self, path):
        folder, newest = usb.live_latest()
        self._json({"ok": True, "folder": folder,
                    "age": round(newest - time.time(), 1) if newest else -1,
                    "car_age": round(usb.usb_img_age(), 1)})


    def _get_api_telemetry(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        p = q.get("path", [""])[0]
        fp = archive.safe_archive_dir(p) if p else None
        if not fp or not os.path.isfile(fp):
            self._json({"ok": False, "reason": "bad-path"}, 404)
            return
        tel = telemetry.telemetry_cached(fp)
        if not tel:
            self._json({"ok": False, "reason": "no-sei"})
            return
        self._json({"ok": True, **tel})


    def _get_api_live(self, path):
        self._json(media.live_status())


    def _get_api_auth_info(self, path):
        self._json({"user": auth.AUTH_USER, "on": auth.AUTH_ON})


    def _get_api_notify(self, path):
        self._json(notifications.notify_status())


    def _get_api_ota_check(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self._json(updates.ota_check(force=q.get("force", ["0"])[0] == "1"))


    def _get_api_skins(self, path):
        skins = sorted(f for f in os.listdir(config.settings.skins_dir)
                       if f != "active.json" and os.path.isfile(os.path.join(config.settings.skins_dir, f)))
        self._json({"skins": skins, "active": media.active_skin()})


    def _get_api_skin(self, path):
        s = media.active_skin()
        if s and s.endswith(".css") and os.path.isfile(os.path.join(config.settings.skins_dir, s)):
            with open(os.path.join(config.settings.skins_dir, s), "rb") as f:
                self._send(200, f.read(), "text/css; charset=utf-8")
        else:
            self._send(204, b"", "text/css")


    def _get_skin_media(self, path):
        fn = archive.safe_name(path[len("/skin-media/"):])
        fp = os.path.join(config.settings.skins_dir, fn) if fn else ""
        if fn and os.path.isfile(fp):
            ext = fn.rsplit(".", 1)[-1].lower()
            ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                  "webp": "image/webp", "css": "text/css"}.get(ext, "application/octet-stream")
            with open(fp, "rb") as f:
                self._send(200, f.read(), ct)
        else:
            self._send(404, b"not found")


    def _get_api_audio(self, path):
        q = sorted(f for f in os.listdir(config.settings.audio_queue) if os.path.isfile(os.path.join(config.settings.audio_queue, f)))
        d = sorted(f for f in os.listdir(config.settings.audio_done) if os.path.isfile(os.path.join(config.settings.audio_done, f)))
        self._json({"queue": q, "done": d})


    def _get_api_browse(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        d = archive.safe_archive_dir(q.get("dir", [""])[0])
        if not d or not os.path.isdir(d):
            self._json({"ok": False, "msg": "目录不存在或不在 /archive 内"}, 404)
            return
        dirs, files = [], []
        for e in sorted(os.listdir(d)):
            ep = os.path.join(d, e)
            if os.path.isdir(ep):
                dirs.append(e)
            elif os.path.isfile(ep):
                files.append({"name": e, "size": os.path.getsize(ep)})
        self._json({"ok": True, "dir": d, "dirs": dirs, "files": files})


    @usb.usb_operation
    def _get_api_usb_browse(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        fp = usb.usb_real(q.get("dir", [""])[0])
        if not usb.usb_attached or not os.path.ismount(usb.MNT):
            self._json({"ok": False, "attached": False, "msg": "车机U盘未连接"})
            return
        if fp is None or not os.path.isdir(fp):
            self._json({"ok": False, "attached": True, "msg": "目录不存在"}, 404)
            return
        dirs, files = [], []
        for e in sorted(os.listdir(fp)):
            ep = os.path.join(fp, e)
            if os.path.isdir(ep):
                dirs.append(e)
            elif os.path.isfile(ep):
                files.append({"name": e, "size": os.path.getsize(ep)})
        rel = os.path.relpath(fp, usb.MNT).replace("\\", "/")
        self._json({"ok": True, "attached": True, "dir": "" if rel == "." else rel,
                    "dirs": dirs, "files": files})


    @usb.usb_operation
    def _get_usbdl(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        fp = usb.usb_real(q.get("path", [""])[0])
        if not fp or not os.path.isfile(fp):
            self._send(404, b"not found")
            return
        usb.serve_file(self, fp)


    def _get_dl(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        fp = archive.safe_archive_path(q.get("path", [""])[0])
        if not fp:
            self._send(404, b"not found")
            return
        out = fp
        qv = q.get("q", [""])[0]
        if qv in media.RES_SCALE:  # 下载也支持清晰度: 转码缓存副本, 失败回退原文件
            out = transcode.get_variant(fp, qv)
        usb.serve_file(self, out)


    def _get_api_export(self, path):
        m2 = re.match(r"^/api/export/([\w-]+)/([\w\-. ]+)/?$", path)
        if not m2:
            self._send(404, b"not found")
            return
        q2 = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        cams = [c.strip() for c in (q2.get("cams", [""])[0] or "").split(",") if c.strip()]
        evdir = archive.safe_archive_dir(config.settings.archive + "/" + m2.group(1) + "/" + m2.group(2))
        if not evdir or not os.path.isdir(evdir):
            self._send(404, b"not found")
            return
        vids = []
        allf = transcode.latest_group(sorted(f for f in os.listdir(evdir) if f.endswith(".mp4")))
        for f in allf:
            if not archive.safe_archive_path(os.path.join(evdir, f)):
                continue
            cam = f.rsplit("-", 1)[-1][:-4] if "-" in f else f
            if not cams or cam in cams:
                vids.append(f)
        if not vids:
            self._send(404, b"no matching videos")
            return
        evname = m2.group(2)
        zname = evname + (("_" + "-".join(cams)) if cams and len(cams) < 6 else "") + ".zip"
        qv = q2.get("q", [""])[0]  # 打包zip也支持清晰度: 每路先取转码副本 (串行转码), 失败回退原文件
        if qv not in media.RES_SCALE:
            qv = ""
        tmp = tempfile.NamedTemporaryFile(prefix="exp_", suffix=".zip", delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
                for f in vids:
                    ffp = os.path.join(evdir, f)
                    if qv:
                        ffp = transcode.get_variant(ffp, qv)
                    z.write(ffp, arcname=evname + "/" + f)
                ej = archive.safe_archive_path(os.path.join(evdir, "event.json"))
                if ej:
                    z.write(ej, arcname=evname + "/event.json")
            zsize = os.path.getsize(tmp.name)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(zsize))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % urllib.parse.quote(zname))
            self.end_headers()
            with open(tmp.name, "rb") as zf:
                while True:
                    chunk = zf.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


    def _get_thumb(self, path):
        self._serve_thumb(path)


    def _get_media(self, path):
        m = re.match(r"^/media/([\w-]+)/([\w\-. ]+)/([\w\-. ]+)$", path)
        if not m:
            self._send(404, b"not found")
            return
        fp = archive.safe_archive_path(os.path.join(config.settings.archive, m.group(1), m.group(2), m.group(3)))
        if not fp:
            self._send(404, b"not found")
            return
        # 清晰度: ?q=360p/480p/720p 时返回按需转码的缓存副本 (失败回退原文件)
        qv = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", ["orig"])[0]
        if qv in media.RES_SCALE and qv != "orig":
            fp = transcode.get_variant(fp, qv)
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        if rng:
            # 支持三种形式: bytes=a-b / bytes=a- / bytes=-n (尾部 n 字节)
            mm2 = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if not mm2:
                self._send(416, b"bad range")
                return
            s1, s2 = mm2.group(1), mm2.group(2)
            if s1 == "":
                # 尾部范围: 浏览器读 mp4 moov 时常用, 不支持会让视频一直卡在 readyState 0
                n = int(s2 or 0)
                start = max(0, size - n)
                end = size - 1
            else:
                start = int(s1)
                end = int(s2) if s2 else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            if end < start:
                end = size - 1
        else:
            start, end = 0, size - 1
        length = end - start + 1
        # 并发限流: 浏览器一次会为几十个缩略图发请求, 不限流会把设备拖死
        if not media.MEDIA_SEM.acquire(timeout=20):
            self._send(503, b"busy")
            return
        try:
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(65536, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            media.MEDIA_SEM.release()
