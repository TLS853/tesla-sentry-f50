"""One retention policy and copy implementation for both archive paths."""

import datetime
import os
import shutil
import tempfile
from . import config, locking


def event_datetime(name):
    try:
        return datetime.datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def read_pruned(archive):
    try:
        with open(os.path.join(archive, ".pruned"), encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def recent_cutoff(events, minutes):
    dates = [event_datetime(e) for e in events]
    dates = [d for d in dates if d is not None]
    return max(dates) - datetime.timedelta(minutes=minutes) if dates and minutes > 0 else None


def archive_allowed(cat, event, pruned, cutoff):
    if cat + "/" + event in pruned:
        return False
    dt = event_datetime(event)
    return not (cat == "RecentClips" and cutoff is not None and dt is not None and dt < cutoff)


@locking.archive_locked
def copy_mounted_archive(mount):
    """Merge complete mounted files atomically, including partially copied events."""
    root = os.path.join(mount, "TeslaCam")
    pruned = read_pruned(config.settings.archive)
    recent = os.path.join(root, "RecentClips")
    cutoff = recent_cutoff(os.listdir(recent) if os.path.isdir(recent) else [],
                           int(config.get("TSF_RECENT_ROLL_MIN", "60")))
    copied = 0
    for cat in ("SentryClips", "SavedClips", "RecentClips"):
        base = os.path.join(root, cat)
        if not os.path.isdir(base):
            continue
        for event in os.listdir(base):
            src = os.path.join(base, event)
            if os.path.islink(src) or not os.path.isdir(src) or not archive_allowed(cat, event, pruned, cutoff):
                continue
            for current, dirs, files in os.walk(src, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
                for name in files:
                    fp = os.path.join(current, name)
                    if os.path.islink(fp) or name.endswith(".part"):
                        continue
                    dest = os.path.realpath(os.path.join(config.settings.archive, cat, event, os.path.relpath(fp, src)))
                    if not dest.startswith(os.path.realpath(config.settings.archive) + os.sep):
                        raise ValueError("archive path escapes archive root")
                    if os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(fp):
                        continue
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    fd, tmp = tempfile.mkstemp(prefix=".archive-", suffix=".part", dir=os.path.dirname(dest))
                    try:
                        with os.fdopen(fd, "wb") as out, open(fp, "rb") as inp:
                            shutil.copyfileobj(inp, out)
                            out.flush()
                            os.fsync(out.fileno())
                        shutil.copystat(fp, tmp)
                        os.replace(tmp, dest)
                        copied += 1
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
    return copied
