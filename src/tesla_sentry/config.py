"""One validated configuration boundary for HTTP, archive and Shell entry points."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex

CATEGORIES = ("SentryClips", "SavedClips", "RecentClips")
CATEGORY_NAMES = dict(zip(CATEGORIES, ("哨兵告警", "手动保存", "行车记录")))
MAX_UPLOAD = 100 * 1024 * 1024


def read_environment(environ=None):
    """Read literal KEY=value assignments; environment variables take precedence."""
    env = dict(os.environ if environ is None else environ)
    values = {}
    path = Path(env.get("TSF_CONF", "/etc/tesla-sentry.conf"))
    if path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?(TSF_\w+)=(.*)", line)
            if not match:
                raise ValueError("%s:%d: expected TSF_NAME=value" % (path, number))
            words = shlex.split(match.group(2), comments=True)
            if len(words) > 1:
                raise ValueError("%s:%d: quote values containing spaces" % (path, number))
            values[match.group(1)] = words[0] if words else ""
    values.update(env)
    return values


@dataclass(frozen=True)
class Settings:
    install_dir: str
    archive: str
    image: str
    mount: str
    cache_dir: str
    port: int
    host: str
    usb_lock: str
    usb_timeout: int
    recent_minutes: int

    @classmethod
    def from_env(cls, env):
        def path(key, default):
            return os.path.realpath(os.path.expanduser(env.get(key) or default))

        def integer(key, default, minimum=0, maximum=None):
            try:
                value = int(env.get(key) or default)
            except ValueError:
                raise ValueError("%s must be an integer" % key) from None
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError("%s is out of range" % key)
            return value

        root = path("TSF_DIR", "/opt/tesla-sentry")
        cache = "/opt/mu300" if os.path.isdir("/opt/mu300") else os.path.join(root, "cache")
        for key, default in (("TSF_MEDIA_CONN", 8), ("TSF_TR_CODES", 2), ("TSF_TEL_CONN", 1), ("TSF_LIVE_INTERVAL", 15)):
            integer(key, default, 1)
        for key, default in (("TSF_BOX_KEEP_DAYS", 14), ("TSF_BOX_MAX_GB", 12), ("TSF_FREE_MIN_MB", 4096),
                             ("TSF_FREE_TARGET_MB", 10240), ("TSF_RECENT_KEEP_MIN", 60)):
            integer(key, default)
        return cls(root, path("TSF_ARCHIVE", "/archive"), path("TSF_IMG", root + "/teslacam.img"),
                   path("TSF_MNT", "/tmp/teslamnt"), path("TSF_CACHE", cache),
                   integer("TSF_PORT", 8080, 0, 65535), env.get("TSF_HOST", "0.0.0.0"),
                   path("TSF_USB_LOCK", "/tmp/tesla-sentry-usb.lock"), integer("TSF_USB_TIMEOUT", 300, 1),
                   integer("TSF_RECENT_ROLL_MIN", 60))

    @property
    def skins_dir(self):
        return os.path.join(self.archive, ".skins")

    @property
    def audio_queue(self):
        return os.path.join(self.archive, "boombox-queue")

    @property
    def audio_done(self):
        return os.path.join(self.archive, "boombox-done")

    @property
    def files_dir(self):
        return os.path.join(self.archive, "files")


environment = read_environment()
settings = Settings.from_env(environment)


def get(name, default=None):
    return environment.get(name, default)
