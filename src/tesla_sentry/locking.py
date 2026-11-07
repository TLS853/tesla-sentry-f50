"""Linux process locks shared by the viewer, live reader and Shell tools."""

import contextlib
import fcntl
import functools
import os
import subprocess
import sys
from . import config


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self, blocking=True):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # close, never unlink: all processes must lock the same inode
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def usb_lock_path():
    return config.settings.usb_lock


@contextlib.contextmanager
def usb_exclusive():
    lock = ProcessLock(usb_lock_path())
    if not lock.acquire(blocking=False):
        raise RuntimeError("USB image is busy; retry after management/sync finishes")
    try:
        yield lock
    finally:
        lock.release()


def archive_locked(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with ProcessLock(os.path.join(config.settings.archive, ".archive.lock")):
            return fn(*args, **kwargs)
    return wrapped


def inherited_usb_lock():
    """Only accept an inherited descriptor pointing at our actual lock inode."""
    try:
        fd = int(config.get("TSF_USB_LOCK_FD", "-1"))
        a, b = os.fstat(fd), os.stat(usb_lock_path())
        return fd if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) else None
    except (ValueError, OSError):
        return None


def usb_lock_exec(argv):
    if not argv:
        return 2
    lock = ProcessLock(usb_lock_path())
    if not lock.acquire(blocking=False):
        print("SKIP: USB image is managed by another process", file=sys.stderr)
        return 75
    try:
        env = dict(config.environment, TSF_USB_LOCK_FD=str(lock.fd))
        return subprocess.call(argv, env=env, pass_fds=(lock.fd,))
    finally:
        lock.release()
