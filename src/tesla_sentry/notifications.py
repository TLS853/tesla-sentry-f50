"""Notification settings, event selection and delivery scheduling."""
import datetime
import json
import os
import re
import time
import threading
import urllib.request
from . import archive, config
from . import media
from . import feishu, qqbot, storage
from . import push_media

NOTIFY_CONF = config.get("TSF_NOTIFY_CONF", os.path.join(config.settings.install_dir, "notify.json"))


NOTIFY_STATE = config.get("TSF_NOTIFY_STATE", os.path.join(config.settings.install_dir, "notify-state.json"))


_NC_DEFAULT = {
    "provider": "feishu", "qq_app_id": "", "qq_app_secret": "",
    "qq_target_type": "user", "qq_openid": "",
    "enabled": False, "mode": "dm", "webhook": "", "app_id": "", "app_secret": "",
    "receive_id": "", "receive_id_type": "", "public_url": "", "saved": False, "delay": 20,
    "media": "thumb", "quality": "480p", "vdur": 30,
}


CAM_NUM = {0: "前视", 3: "左B柱", 4: "右B柱", 5: "左翼子板", 6: "右翼子板", 7: "后视"}


CAM_SUFFIX = {0: "front", 3: "left_pillar", 4: "right_pillar",
              5: "left_repeater", 6: "right_repeater", 7: "back"}


REASON_DETAIL = {
    "sentry_aware_object_detection": "侦测到物体移动",
    "sentry_aware_accel": "侦测到碰撞/震动",
    "sentry_aware_door": "车门被拉动",
    "sentry_aware_handle": "门把手被拉动",
    "sentry_panic": "遭遇袭击 (紧急报警)",
    "sentry_trigger": "哨兵模式触发",
    "user_interaction_honk": "按喇叭保存",
    "user_interaction_dashcam_saved": "手动保存",
    "user_interaction_dashcam_menu": "行车记录仪菜单保存",
}


def notify_conf():
    c = dict(_NC_DEFAULT)
    try:
        with open(NOTIFY_CONF, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            c.update(j)
    except Exception:
        pass
    return c


_CONFIG_LOCK = threading.RLock()


def notify_save(patch):
    with _CONFIG_LOCK:
        return _save_config(patch)


def _save_config(patch):
    c = notify_conf()
    for k, v in (patch or {}).items():
        if k in _NC_DEFAULT:
            c[k] = v
    storage.atomic_json(NOTIFY_CONF, c)
    return c


def _nstate_load():
    try:
        with open(NOTIFY_STATE, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _nstate_save(st):
    try:
        os.makedirs(os.path.dirname(NOTIFY_STATE) or ".", exist_ok=True)
        tmp = NOTIFY_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, NOTIFY_STATE)
    except OSError:
        pass


def _reason_info(reason):
    """reason → (分类, 卡片主题色, 详情)"""
    r = str(reason or "")
    detail = REASON_DETAIL.get(r) or ("未标注原因" if not r else r)
    if r.startswith(("sentry_panic", "sentry_aware_accel")):
        return "遭遇袭击", "red", detail
    if r.startswith(("sentry_aware_door", "sentry_aware_handle")):
        return "车门拉动", "orange", detail
    if r.startswith("sentry_"):
        return "哨兵警觉", "blue", detail
    if r.startswith("user_interaction"):
        return "手动保存", "grey", detail
    return "其他事件", "grey", detail


def _ev_files(d):
    if not archive.safe_archive_dir(d):
        return []
    try:
        return sorted(f for f in os.listdir(d) if f.lower().endswith(".mp4") and archive.safe_archive_path(os.path.join(d, f)))
    except OSError:
        return []


def _event_meta(cat, name):
    path = archive.safe_archive_path(os.path.join(config.settings.archive, cat, name, "event.json"))
    if path:
        try:
            with open(path, encoding="utf-8") as stream:
                meta = json.load(stream)
            if isinstance(meta, dict):
                return meta
        except (OSError, ValueError):
            pass
    return {}


def _pick_clip(cat, name):
    """按 event.json 的 camera + timestamp 找出触发摄像头那一路的 mp4, 返回 (文件路径, 触发偏移秒, meta);
    对不上时回落到前视。找不到任何片段返回 (None, 0, meta)。"""
    d = os.path.join(config.settings.archive, cat, name)
    meta = _event_meta(cat, name)
    files = _ev_files(d)
    if not files:
        return None, 0.0, meta
    cam_key = CAM_SUFFIX.get(int(meta.get("camera")) if str(meta.get("camera", "")).strip().isdigit() else -1)
    ts = None
    try:
        ts = datetime.datetime.strptime(str(meta.get("timestamp"))[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        ts = None
    if ts is not None:
        for f in files:                       # 文件名前 19 位是片段开始时间, 与 timestamp 同一时基
            try:
                s = datetime.datetime.strptime(f[:19], "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                continue
            off = (ts - s).total_seconds()
            if 0 <= off <= 70 and f.rsplit("-", 1)[-1][:-4] == cam_key:
                return os.path.join(d, f), max(0.0, min(off, 60.0)), meta
    front = [f for f in files if f.endswith("-front.mp4")]
    return os.path.join(d, front[0] if front else files[0]), 1.0, meta


def _trigger_frame(cat, name):
    """触发摄像头那一帧的封面图; 返回 (jpg 路径, meta)"""
    fp, off, meta = _pick_clip(cat, name)
    if not fp:
        return None, meta
    return media.thumb_file(fp, off), meta


def manual_push(conf, src, cat, name, quality="480p"):
    """把一个现成的视频文件 (浏览器合成的多路画面) 推到飞书私聊; 返回 (成功?, 提示)。
    src 会被整段转码后上传, 推完发送事件卡片 (带查看按钮)。"""
    if conf.get("provider") == "qq":
        if not qqbot.ready(conf):
            return False, "QQ 未就绪: 请配置 AppID / AppSecret 和接收 OpenID"
        vid, dur = push_media._transcode_push_upload(src, quality)
        file_info = qqbot.upload(conf, vid, 2)
        qqbot.send(conf, _event_text(conf, cat, name), file_info)
        return True, "已推送 %.0f 秒视频到 QQ" % dur
    if not feishu._dm_ready(conf):
        return False, "飞书私聊未就绪: 需要在「推送」里配好 App ID/Secret + 接收人 (群机器人 webhook 发不了视频)"
    vid, dur = push_media._transcode_push_upload(src, quality)
    mb = os.path.getsize(vid) / 1048576.0
    if mb > 28:
        return False, "转码后仍有 %.0fMB, 超过飞书 30MB 上限; 请选更短片段或更低清晰度" % mb
    fk = feishu._feishu_upload_file(conf, vid, dur)
    feishu._feishu_send_media(conf, fk)
    try:
        feishu._feishu_send_dm(conf, _feishu_card(conf, cat, name, img_key=None))
    except Exception:
        pass
    return True, "已推送 %.0f 秒视频 (%.1fMB) 到飞书私聊" % (dur, mb)


def _feishu_card(conf, cat, name, img_key=None, test=False):
    """拼一张飞书卡片 (标题/时间/位置/原因/镜头/封面/查看按钮)"""
    d = os.path.join(config.settings.archive, cat, name)
    meta = _event_meta(cat, name)
    cat_name, color, detail = _reason_info(meta.get("reason"))
    if cat == "SavedClips" and not str(meta.get("reason", "")).startswith("sentry_"):
        cat_name, color = "手动保存", "grey"
    cam = CAM_NUM.get(int(meta["camera"])) if str(meta.get("camera", "")).strip().isdigit() else None
    when = str(meta.get("timestamp") or name)[:19].replace("T", " ").replace("_", " ")
    try:
        dt = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        when = "%d月%d日 %02d:%02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute, dt.second)
    except Exception:
        pass
    loc = " ".join(x for x in (meta.get("city") or "", meta.get("street") or "") if x).strip() or "位置未知"
    nclip = len(_ev_files(d))
    title = ("🧪 [测试] " if test else "🚨 ") + cat_name + (" · " + cam if cam else "")
    fields = [("时间", when), ("位置", loc), ("触发原因", detail)]
    if cam:
        fields.append(("触发镜头", "%s (#%s)" % (cam, meta.get("camera"))))
    fields.append(("片段", "%d 路画面" % nclip))
    els = []
    if img_key:
        els.append({"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": "触发画面"}})
    for i in range(0, len(fields), 2):
        els.append({"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (k, v)}}
            for k, v in fields[i:i + 2]]})
    link = (conf.get("public_url") or "").rstrip("/")
    acts = []
    if link:
        acts.append({"tag": "button", "text": {"tag": "plain_text", "content": "查看录像"},
                     "url": "%s/?ev=%s/%s" % (link, urllib.parse.quote(cat), urllib.parse.quote(name)),
                     "type": "primary"})
    if acts:
        els.append({"tag": "action", "actions": acts})
    if test:
        els.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "这是一条测试推送"}]})
    return {
        "config": {"wide_screen": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": els,
    }


def _event_text(conf, cat, name, test=False):
    meta = _event_meta(cat, name)
    title, _color, reason = _reason_info(meta.get("reason"))
    if cat == "SavedClips" and not str(meta.get("reason", "")).startswith("sentry_"):
        title = "手动保存"
    lines = [("[测试] " if test else "") + "Tesla 哨兵 · " + title,
             "时间: " + str(meta.get("timestamp") or name or datetime.datetime.now().isoformat(timespec="seconds")),
             "位置: " + " ".join(str(meta.get(k) or "") for k in ("city", "street")).strip(),
             "原因: " + reason]
    if str(meta.get("camera", "")).isdigit():
        lines.append("镜头: " + str(CAM_NUM.get(int(meta["camera"]), meta["camera"])))
    link = str(conf.get("public_url") or "").rstrip("/")
    if link and name:
        lines.append("查看: %s/?ev=%s/%s" % (link, urllib.parse.quote(cat, safe=""), urllib.parse.quote(name, safe="")))
    return "\n".join(lines)


def _qq_push(conf, cat, name, test=False):
    if not qqbot.ready(conf):
        return False, "QQ 未配置完整: 需要 AppID / AppSecret 和接收 OpenID (不是 QQ 号/群号)"
    file_info, warn = None, ""
    content = _event_text(conf, cat, name, test)
    try:
        mode = conf.get("media", "thumb")
        if mode == "video":
            source, offset, _meta = _pick_clip(cat, name)
            if not source:
                raise ValueError("归档里没有可用片段")
            video = push_media._make_push_video(source, offset, conf.get("quality") or "480p", int(conf.get("vdur") or 30))
            file_info = qqbot.upload(conf, video, 2)
        elif mode == "thumb":
            jpg, _meta = _trigger_frame(cat, name)
            if not jpg:
                raise ValueError("未找到封面帧")
            file_info = qqbot.upload(conf, jpg, 1)
    except Exception as error:
        warn = " (媒体未附上, 已发文字: %s)" % str(error)[:150]
    try:
        qqbot.send(conf, content, file_info)
    except Exception as error:
        return False, "QQ 推送失败: " + str(error)[:200]
    return True, "已推送到 QQ" + ("群" if conf.get("qq_target_type") == "group" else "单聊") + warn


def notify_ready(conf):
    if conf.get("provider") == "qq":
        return qqbot.ready(conf)
    return feishu._dm_ready(conf) or bool(str(conf.get("webhook") or "").strip())


def notify_push(cat, name, test=False):
    """Deliver through the selected provider; return (success, message)."""
    conf = notify_conf()
    if conf.get("provider") == "qq":
        return _qq_push(conf, cat, name, test)
    dm = feishu._dm_ready(conf)
    hook = str(conf.get("webhook") or "").strip()
    use_dm = dm and (conf.get("mode") != "group" or not hook)
    if not use_dm and not hook:
        return False, "未配置: 需要 App ID/Secret + 接收人 (私聊), 或群机器人 webhook"
    img_key = None
    warn = ""
    if conf.get("media") != "text" and conf.get("app_id") and conf.get("app_secret"):
        try:
            jpg, _m = _trigger_frame(cat, name)
            if jpg:
                img_key = feishu._feishu_upload(conf, jpg)
            else:
                warn = " (没抽到封面帧)"
        except Exception as ex:
            warn = " (封面未附上: %s)" % str(ex)[:80]
    elif conf.get("media") != "text":
        warn = " (未配 app_id/app_secret, 只发文字卡片)"
    # 视频模式: 先把触发摄像头那一路上传成视频消息, 再补一条精简卡片(带查看按钮)
    if use_dm and (conf.get("media") or "thumb") == "video":
        try:
            fp, off, _meta = _pick_clip(cat, name)
            if not fp:
                raise RuntimeError("归档里没有可用的片段")
            dur = int(conf.get("vdur") or 30)
            vid = push_media._make_push_video(fp, off, conf.get("quality") or "480p", dur)
            fk = feishu._feishu_upload_file(conf, vid, dur)
            feishu._feishu_send_media(conf, fk, img_key or "")
            try:
                feishu._feishu_send_dm(conf, _feishu_card(conf, cat, name, img_key=None, test=test))
            except Exception:
                pass
            mb = os.path.getsize(vid) / 1048576.0
            return True, "已推送 %d 秒视频 (%.1fMB) 到飞书私聊" % (dur, mb) + warn
        except Exception as ex:
            warn = " (视频没发成, 已回落封面卡片: %s)" % str(ex)[:90]
    card = _feishu_card(conf, cat, name, img_key=img_key, test=test)
    try:
        if use_dm:
            feishu._feishu_send_dm(conf, card)
            return True, "已推送到你的飞书私聊" + warn
        j = feishu._post_json(hook, {"msg_type": "interactive", "card": card})
        code = j.get("code", j.get("StatusCode", -1))
        if code not in (0, None):
            raise RuntimeError("%s (code=%s)" % (j.get("msg") or j.get("StatusMessage") or "发送失败", code))
    except Exception as ex:
        return False, "推送失败: %s" % str(ex)[:140]
    return True, "已推送到飞书群" + warn


def _ev_ts(name):
    """从事件目录名 YYYY-MM-DD_HH-MM-SS 解析事件真实发生时间; 失败返回 0"""
    try:
        return time.mktime(time.strptime(name, "%Y-%m-%d_%H-%M-%S"))
    except Exception:
        return 0


def notify_scan():
    """扫一遍归档, 把新出现的哨兵事件推给飞书; 返回本轮推送条数"""
    conf = notify_conf()
    if not conf.get("enabled") or not notify_ready(conf):
        return 0
    st = _nstate_load()
    seen = st.setdefault("seen", {})
    tries = st.setdefault("try", {})
    first = not st.get("init")
    delay = max(5, int(conf.get("delay") or 20))
    cats = ["SentryClips"] + (["SavedClips"] if conf.get("saved") else [])
    sent = 0
    for cat in cats:
        if sent >= 3:
            break
        base = os.path.join(config.settings.archive, cat)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", name):
                continue
            key = cat + "/" + name
            if key in seen:
                continue
            d = os.path.join(base, name)
            newest = 0.0
            try:
                for f in os.listdir(d):
                    newest = max(newest, os.path.getmtime(os.path.join(d, f)))
            except OSError:
                continue
            now = time.time()
            if now - newest < delay:
                continue                     # 还在同步写入, 下一轮再看
            # 两道判定:
            # (1) 目录名里的日期 = 事件真实发生时间(车机时钟)。车机U盘循环覆盖后
            #     tesla-sync 会把旧片段重新归档, 复制出来的文件 mtime 是"复制时刻",
            #     只看 mtime 会把几天前的事件当新事件推出去。所以超过 24h 的直接记账不推
            #     (留 24h 余量足以吸收车机与盒子的时区偏差)。
            # (2) 新鲜度仍按 mtime: 保留"还在写入就不推""超过 6h 不补"的原语义。
            ev_ts = _ev_ts(name)
            if ev_ts and now - ev_ts > 24 * 3600:
                seen[key] = int(now)         # 历史回灌(一天以前的片段), 不推
                continue
            if first or now - newest > 6 * 3600:
                seen[key] = int(now)         # 首次启用不补历史; 太旧的(>6h)也不补
                continue
            ok, msg = notify_push(cat, name)
            st["last"] = {"at": int(time.time()), "ok": ok, "msg": msg, "ev": key}
            if ok:
                sent += 1
                seen[key] = int(now)
                tries.pop(key, None)
            else:
                tries[key] = tries.get(key, 0) + 1
                if tries[key] >= 3:
                    seen[key] = int(now)      # 连推 3 次都失败就放弃, 防止卡住队列
                    tries.pop(key, None)
            if sent >= 3:
                break
    if first:
        st["init"] = 1
    if len(seen) > 3000:                       # 防止状态文件无限膨胀
        for k in sorted(seen, key=lambda x: seen.get(x) or 0)[:len(seen) - 2000]:
            seen.pop(k, None)
    _nstate_save(st)
    return sent


def notify_loop(stop_event):
    if stop_event.wait(25):
        return
    while not stop_event.is_set():
        try:
            notify_scan()
        except Exception:
            pass
        stop_event.wait(30)


def notify_status():
    conf = notify_conf()
    st = _nstate_load()
    rid, rtype = feishu._dm_target(conf)
    return {
        "provider": conf.get("provider") or "feishu",
        "qq_app_id": conf.get("qq_app_id") or "",
        "qq_has_secret": bool(conf.get("qq_app_secret")),
        "qq_target_type": conf.get("qq_target_type") or "user",
        "qq_openid": conf.get("qq_openid") or "",
        "ready": notify_ready(conf),
        "enabled": bool(conf.get("enabled")),
        "mode": conf.get("mode") or "dm",
        "webhook": conf.get("webhook") or "",
        "app_id": conf.get("app_id") or "",
        "has_secret": bool(conf.get("app_secret")),
        "receive_id": rid or "",
        "receive_id_type": rtype or "",
        "dm_ready": feishu._dm_ready(conf),
        "public_url": conf.get("public_url") or "",
        "saved": bool(conf.get("saved")),
        "delay": int(conf.get("delay") or 20),
        "media": conf.get("media") or "thumb",
        "quality": conf.get("quality") or "480p",
        "vdur": int(conf.get("vdur") or 30),
        "last": st.get("last") or {},
        "sent_total": len(st.get("seen") or {}),
    }
