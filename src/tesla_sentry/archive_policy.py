"""One retention policy and copy implementation for both archive paths."""

import datetime
import logging
import os
import shutil
import tempfile
from . import config, locking, storage

LOG = logging.getLogger(__name__)


def event_datetime(name):
    try:
        return datetime.datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def read_pruned(archive):
    try:
        with open(os.path.join(archive, ".pruned"), encoding="utf-8") as f:
            return set(line.rstrip("\r\n") for line in f if line.strip())
    except FileNotFoundError:
        return set()


def recent_cutoff(events, minutes):
    dates = [event_datetime(e) for e in events]
    dates = [d for d in dates if d is not None]
    return max(dates) - datetime.timedelta(minutes=minutes) if dates and minutes > 0 else None


def archive_allowed(cat, event, pruned, cutoff, filename=None):
    key = cat + "/" + event
    if key in pruned or (filename is not None and key + "/" + filename in pruned):
        return False
    dt = event_datetime(event)
    return not (cat == "RecentClips" and cutoff is not None and dt is not None and dt < cutoff)


@locking.archive_locked
def clear_recordings():
    """Clear only the archive categories, recording tombstones before deleting."""
    root = os.path.realpath(config.settings.archive)
    events = []
    tombstones = set()
    for cat in config.CATEGORIES:
        base = os.path.join(root, cat)
        if os.path.islink(base) or os.path.ismount(base):
            raise ValueError("录像归档目录无效")
        if not os.path.isdir(base):
            continue
        with os.scandir(base) as listing:
            for item in listing:
                if not item.is_dir(follow_symlinks=False):
                    continue
                if os.path.ismount(item.path) or "\n" in item.name or "\r" in item.name:
                    raise ValueError("录像归档目录无效")
                key = cat + "/" + item.name
                if cat == "RecentClips" and item.name == "_misc":
                    # This group can receive unrelated recordings later. Suppress only
                    # the files that are being cleared.
                    with os.scandir(item.path) as miscellaneous:
                        tombstones.update(key + "/" + f.name for f in miscellaneous
                                          if f.is_file(follow_symlinks=False)
                                          and f.name.lower().endswith(".mp4")
                                          and "\n" not in f.name and "\r" not in f.name)
                else:
                    tombstones.add(key)
                events.append(item.path)
    if tombstones:
        pruned_path = os.path.join(root, ".pruned")
        if os.path.islink(pruned_path):
            raise ValueError("归档清理记录路径无效")
        pruned = read_pruned(root) | tombstones
        storage.atomic_write(pruned_path, ("\n".join(sorted(pruned)) + "\n").encode("utf-8"))
    failed = 0
    for path in events:
        try:
            shutil.rmtree(path)
        except OSError as error:
            if os.path.lexists(path):
                failed += 1
                LOG.warning("Could not clear archive folder %s: %s", path, error)
    return len(events) - failed, failed


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
                    relative = os.path.relpath(fp, src)
                    if cat + "/" + event + "/" + relative in pruned:
                        continue
                    dest = os.path.realpath(os.path.join(config.settings.archive, cat, event, relative))
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
