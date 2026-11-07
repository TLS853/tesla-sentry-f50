"""Archive service."""
import json
import os
import re
from . import config

def ensure_dirs():
    for d in (config.settings.skins_dir, config.settings.audio_queue, config.settings.audio_done, config.settings.files_dir, config.settings.archive):
        os.makedirs(d, exist_ok=True)
    for c in config.CATEGORIES:
        os.makedirs(os.path.join(config.settings.archive, c), exist_ok=True)


def safe_name(name):
    name = os.path.basename(name or "")
    return name if re.match(r'^[\w\-. ]{1,80}$', name) else None


def list_events():
    out = {}
    for cat in config.CATEGORIES:
        base = os.path.join(config.settings.archive, cat)
        events = []
        if os.path.isdir(base):
            for name in sorted(os.listdir(base), reverse=True):
                d = os.path.join(base, name)
                if not os.path.isdir(d) or not safe_archive_dir(d):
                    continue
                vids = []
                total = 0
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".mp4"):
                        cam = f.rsplit("-", 1)[-1][:-4] if "-" in f else f
                        vids.append({"url": f"/media/{cat}/{name}/{f}", "cam": cam, "f": f})
                        try:
                            total += os.path.getsize(os.path.join(d, f))
                        except OSError:
                            pass
                meta = {}
                ej = safe_archive_path(os.path.join(d, "event.json"))
                if ej:
                    try:
                        with open(ej, "r", encoding="utf-8", errors="ignore") as jf:
                            meta = json.load(jf)
                    except Exception:
                        meta = {}
                events.append({"name": name, "videos": vids, "size": total, "meta": meta})
        out[cat] = events
    return out


def _map_archive(p):
    """前端与深链固定用 /archive/... 作为虚拟前缀; 归档区可配置时映射到实际目录"""
    if config.settings.archive != "/archive" and (p == "/archive" or p.startswith("/archive/")):
        return config.settings.archive + p[len("/archive"):]
    return p


def safe_archive_dir(p):
    """校验目录必须落在归档区内 (防路径逃逸), 返回 realpath 或 None"""
    p = _map_archive((p or "").strip())
    if not p.startswith(config.settings.archive):
        return None
    rp = os.path.realpath(p)
    root = os.path.realpath(config.settings.archive)
    if rp != root and not rp.startswith(root + "/"):
        return None
    return rp


def safe_archive_path(p):
    """校验文件路径必须落在归档区内且真实存在"""
    p = _map_archive((p or "").strip())
    if not p.startswith(config.settings.archive):
        return None
    rp = os.path.realpath(p)
    if not rp.startswith(os.path.realpath(config.settings.archive) + "/") or not os.path.isfile(rp):
        return None
    return rp
