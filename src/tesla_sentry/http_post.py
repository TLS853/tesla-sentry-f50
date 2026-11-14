"""Http Post service."""

from . import feishu
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from . import archive, auth, config, notifications, qqbot, transcode, update_package, updates, usb, vehicle_assets

class PostRoutesMixin:


    def _post_api_auth_pass(self, path):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
        except Exception:
            self._json({"ok": False, "msg": "参数错误"}, 400)
            return
        ok, msg = auth.auth_set_pass(str(body.get("old") or ""), str(body.get("new") or ""))
        self._json({"ok": ok, "msg": msg, "user": auth.AUTH_USER})
        return


    def _post_api_notify_save(self, path):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 <= n <= 65536:
                raise ValueError("invalid settings size")
            body = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            if not isinstance(body, dict):
                raise ValueError("settings must be an object")
        except Exception:
            self._json({"ok": False, "msg": "参数错误"}, 400)
            return
        patch = {}
        for k in ("enabled", "saved"):
            if k in body:
                patch[k] = bool(body.get(k))
        for k in ("mode", "media", "quality", "webhook", "app_id", "receive_id", "provider",
                  "receive_id_type", "public_url", "qq_app_id", "qq_target_type", "qq_openid"):
            if k in body:
                patch[k] = str(body.get(k) or "").strip()
        for key in ("app_secret", "qq_app_secret"):
            if str(body.get(key) or "").strip():
                patch[key] = str(body[key]).strip()
        for key, allowed in (("provider", ("feishu", "qq")), ("qq_target_type", ("user", "group"))):
            if key in patch and patch[key] not in allowed:
                self._json({"ok": False, "msg": "推送渠道或接收类型无效"}, 400)
                return
        if "delay" in body:
            try:
                patch["delay"] = max(5, min(600, int(body["delay"])))
            except (TypeError, ValueError):
                pass
        if "vdur" in body:
            try:
                patch["vdur"] = max(10, min(60, int(body["vdur"])))
            except (TypeError, ValueError):
                pass
        if str(patch.get("quality") or "") not in ("360p", "480p", "720p"):
            patch.pop("quality", None)
        if str(patch.get("media") or "") not in ("thumb", "video", "text"):
            patch.pop("media", None)
        conf = notifications.notify_save(patch)
        if conf.get("provider") == "qq":
            warn = ("QQ 已就绪; 请确保接收方允许主动消息, 点击「发送测试」验证平台权限"
                    if qqbot.ready(conf) else "QQ 需要 AppID / AppSecret 和接收 OpenID, 不能填写 QQ 号或群号")
        elif feishu._dm_ready(conf):
            warn = "" if (conf.get("public_url") or "").strip() else "私聊已可用; 填「查看链接前缀」后卡片上会有查看按钮"
        elif str(conf.get("webhook") or "").strip():
            warn = "私聊未就绪 (需 App ID/Secret + 接收人), 当前会推到群里"
        else:
            warn = "还没配接收人: 私聊需要 App ID/Secret + 接收人, 或填群机器人 webhook"
        self._json({"ok": True, "msg": "已保存", "status": notifications.notify_status(), "warn": warn})
        return


    def _post_api_notify_test(self, path):
        cat, name = "SentryClips", ""
        try:
            base = os.path.join(config.settings.archive, "SentryClips")
            cands = sorted(e for e in os.listdir(base)
                           if re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", e))
            name = cands[-1] if cands else ""
        except OSError:
            pass
        if not name and notifications.notify_conf().get("provider") != "qq":
            self._json({"ok": False, "msg": "归档里还没有哨兵事件, 无法生成测试卡片"}, 400)
            return
        ok, msg = notifications.notify_push(cat, name, test=True)
        self._json({"ok": ok, "msg": msg})
        return


    def _post_api_push_upload(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        cat = q.get("cat", [""])[0]
        name = q.get("name", [""])[0]
        quality = q.get("q", ["480p"])[0]
        if quality not in ("360p", "480p", "720p", "orig"):
            quality = "480p"
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not name or not re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", name):
            self._json({"ok": False, "msg": "参数错误"}, 400)
            return
        if length <= 0 or length > 300 * 1048576:
            self._json({"ok": False, "msg": "片段为空或超过 300MB"}, 400)
            return
        evdir = archive.safe_archive_dir(os.path.join(config.settings.archive, cat, name))
        if not evdir or not os.path.isdir(evdir):
            self._json({"ok": False, "msg": "事件不存在"}, 400)
            return
        descriptor, tmp = tempfile.mkstemp(prefix="tsf-push-upload-")
        try:
            with os.fdopen(descriptor, "wb") as f:
                rem = length
                while rem > 0:
                    chunk = self.rfile.read(min(1 << 20, rem))
                    if not chunk:
                        break
                    f.write(chunk)
                    rem -= len(chunk)
            if rem:
                raise ValueError("视频上传不完整, 请重试")
            ok, msg = notifications.manual_push(notifications.notify_conf(), tmp, cat, name, quality)
        except Exception as ex:
            ok, msg = False, "推送失败: %s" % str(ex)[:160]
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._json({"ok": ok, "msg": msg})
        return


    def _post_api_ota_apply(self, path):
        content_type = self.headers.get("Content-Type", "").strip().lower()
        if content_type != "application/zip":
            self._json({"ok": False, "msg": "请刷新更新页并上传本地 ZIP 文件"}, 415)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._json({"ok": False, "msg": "ZIP 上传须提供文件长度"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                raise ValueError()
        except ValueError:
            self._json({"ok": False, "msg": "ZIP 文件为空或长度无效"}, 400)
            return
        if length > update_package.MAX_ZIP_SIZE:
            self._json({"ok": False, "msg": "更新 ZIP 超过 16 MiB"}, 413)
            return
        ok, msg = updates.ota_apply(self.rfile, length, self.headers.get("X-Update-SHA256", "").strip())
        self._json({"ok": ok, "msg": msg})
        return


    def _post_api_ota_rollback(self, path):
        ok, msg = updates.ota_rollback()
        self._json({"ok": ok, "msg": msg})
        return


    def _post_api_stitch(self, path):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
        except Exception:
            self._json({"ok": False, "error": "参数错误"}, 400)
            return
        jid, err = transcode.start_stitch(body.get("cat") or "", body.get("name") or "", body.get("cams") or [])
        if not jid:
            self._json({"ok": False, "error": err}, 400)
            return
        self._json({"ok": True, "id": jid})
        return


    def _post_api_upload_skin(self, path):
        fn, data, _f = self._read_upload()
        if fn == "TOOBIG":
            self._json({"ok": False, "msg": "文件超过 3MB"}, 400)
            return
        fn = archive.safe_name(fn)
        if not fn or not data or not fn.lower().endswith((".css", ".jpg", ".jpeg", ".png", ".webp")):
            self._json({"ok": False, "msg": "仅支持 css/jpg/png/webp"}, 400)
            return
        with open(os.path.join(config.settings.skins_dir, fn), "wb") as f:
            f.write(data)
        activated = False
        if fn.endswith(".css"):
            with open(os.path.join(config.settings.skins_dir, "active.json"), "w") as f:
                json.dump({"skin": fn}, f)
            activated = True
        self._json({"ok": True, "msg": ("皮肤已上传并应用" if activated else "图片已上传, 请在 CSS 中用 /skin-media/" + fn), "activated": activated})


    def _post_api_upload_audio(self, path):
        fn, data, fields = self._read_upload(vehicle_assets.MAX_AUDIO + 65536)
        if fn == "TOOBIG":
            self._json({"ok": False, "msg": "原始音频超过 20 MB"}, 400)
            return
        try:
            converted, duration = vehicle_assets.lock_chime(fn, data, fields.get("start", 0), fields.get("duration", 5))
            vehicle_assets.queue_chime(converted)
        except ValueError as error:
            self._json({"ok": False, "msg": str(error)}, 400)
            return
        self._json({"ok": True, "name": "LockChime.wav", "duration": duration, "size": len(converted),
                    "msg": "已转换为 LockChime.wav (%.1f 秒 / %d KB), 归档同步时自动写入 USB 根目录" % (duration, len(converted) // 1024)})


    @usb.usb_operation
    def _post_api_upload_wrap(self, path):
        fn, data, fields = self._read_upload(vehicle_assets.MAX_WRAP + 65536)
        if fn == "TOOBIG":
            self._json({"ok": False, "msg": "涂装 PNG 不得超过 1 MB"}, 400)
            return
        if not usb.usb_attached or not os.path.ismount(usb.MNT):
            self._json({"ok": False, "msg": "请先连接车机 U 盘进入管理模式"}, 400)
            return
        try:
            if not fn:
                raise ValueError("请选择 PNG 涂装文件")
            name, width, height = vehicle_assets.write_wrap(usb.MNT, fn, data, fields.get("overwrite") == "1")
        except FileExistsError as error:
            self._json({"ok": False, "msg": str(error), "exists": True}, 409)
            return
        except ValueError as error:
            self._json({"ok": False, "msg": str(error)}, 400)
            return
        self._json({"ok": True, "name": name, "width": width, "height": height,
                    "msg": "已写入 Wraps/%s; 恢复车机导出后, 在玩具箱 → 喷漆车间 → 涂装选择" % name})


    def _post_api_usb_attach(self, path):
        ok, msg = usb.usb_attach()
        self._json({"ok": ok, "msg": "车机U盘已连接 (已临时断开车机读取, 5分钟无操作自动恢复)" if ok and msg != "already" else ("已处于管理状态" if ok else msg)})


    def _post_api_usb_detach(self, path):
        ok, msg = usb.usb_detach()
        self._json({"ok": ok, "msg": "已恢复车机导出" if ok else msg})


    @usb.usb_operation
    def _post_api_upload_usb(self, path):
        fn, data, fields = self._read_upload()
        if fn == "TOOBIG":
            self._json({"ok": False, "msg": "文件超过 100MB 上限"}, 400)
            return
        fn = archive.safe_name(fn)
        if not fn or not data:
            self._json({"ok": False, "msg": "无效的文件"}, 400)
            return
        if not usb.usb_attached or not os.path.ismount(usb.MNT):
            self._json({"ok": False, "msg": "车机U盘未连接, 请先点「连接车机U盘」"}, 400)
            return
        dest = usb.usb_real(fields.get("path"))
        if dest is None or not os.path.isdir(dest):
            self._json({"ok": False, "msg": "目标目录不存在"}, 400)
            return
        with open(os.path.join(dest, fn), "wb") as f:
            f.write(data)
        subprocess.run(["sync"], capture_output=True)
        rel = os.path.relpath(dest, usb.MNT).replace("\\", "/")
        self._json({"ok": True, "msg": f"已上传到 U盘/{'' if rel == '.' else rel + '/' }{fn} ({len(data)//1024} KB)"})


    def _post_api_upload_file(self, path):
        fn, data, fields = self._read_upload()
        if fn == "TOOBIG":
            self._json({"ok": False, "msg": "文件超过 100MB 上限"}, 400)
            return
        fn = archive.safe_name(fn)
        if not fn or not data:
            self._json({"ok": False, "msg": "无效的文件"}, 400)
            return
        req_path = (fields.get("path") or "").strip()
        dest_dir = archive.safe_archive_dir(req_path)
        if req_path and not dest_dir:
            self._json({"ok": False, "msg": "目标路径无效 (必须在 /archive 内)"}, 400)
            return
        if not dest_dir:
            dest_dir = config.settings.files_dir
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, fn), "wb") as f:
            f.write(data)
        self._json({"ok": True, "msg": f"已上传到 {dest_dir}/{fn} ({len(data)//1024} KB)"})


    def _post_api_skin_activate(self, path):
        length = int(self.headers.get("Content-Length", 0))
        try:
            name = json.loads(self.rfile.read(length)).get("name")
        except Exception:
            name = None
        name = archive.safe_name(name)
        if name and os.path.isfile(os.path.join(config.settings.skins_dir, name)):
            with open(os.path.join(config.settings.skins_dir, "active.json"), "w") as f:
                json.dump({"skin": name}, f)
            self._json({"ok": True})
        else:
            self._json({"ok": False, "msg": "皮肤不存在"}, 404)


    def _post_api_audio_setlockchime(self, path):
        length = int(self.headers.get("Content-Length", 0))
        try:
            name = json.loads(self.rfile.read(length)).get("name")
        except Exception:
            name = None
        try:
            data = vehicle_assets.queued_audio(name)
            converted, duration = vehicle_assets.lock_chime(name, data)
            vehicle_assets.queue_chime(converted)
        except ValueError as error:
            self._json({"ok": False, "msg": str(error)}, 400)
            return
        self._json({"ok": True, "name": "LockChime.wav", "duration": duration, "size": len(converted)})
