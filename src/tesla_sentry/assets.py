"""Read-only browser resources, available from source trees and bundled launchers."""
import json
import pkgutil
from pathlib import Path

LANG_NAMES = [("zh", "中文"), ("en", "English"), ("de", "Deutsch"), ("fr", "Français"),
              ("it", "Italiano"), ("es", "Español"), ("nl", "Nederlands"), ("ru", "Русский"),
              ("ja", "日本語"), ("ko", "한국어")]


def resource(name):
    data = pkgutil.get_data("tesla_sentry", "web/" + name)
    if data is None:
        raise FileNotFoundError(name)
    return data


def runtime_script(name):
    if "/" in name or not name.endswith(".sh"):
        raise ValueError("invalid runtime script")
    try:
        return resource("runtime/" + name)
    except FileNotFoundError:
        # Source-tree execution uses the same scripts that the builder embeds.
        return (Path(__file__).resolve().parents[2] / "scripts" / name).read_bytes()


LANG_TABLE = json.loads(resource("i18n.json"))
if any(not isinstance(text, str) for table in LANG_TABLE.values() for text in table.values()):
    raise ValueError("translation tables must contain string values")
HTML = resource("index.html").decode("utf-8")
LOGIN_HTML = resource("login.html").decode("utf-8")
I18N_JS = json.dumps(LANG_TABLE, ensure_ascii=False, separators=(",", ":"))
LANG_JS = json.dumps(LANG_NAMES, ensure_ascii=False, separators=(",", ":"))


def tr(text, lang="en"):
    if not text or lang == "zh":
        return text
    return (LANG_TABLE.get(lang) or {}).get(text) or LANG_TABLE.get("en", {}).get(text) or text


def lang_from_accept(header, default="zh"):
    supported = dict(LANG_NAMES)
    for part in (header or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if tag in supported:
            return tag
        if tag.split("-")[0] in supported:
            return tag.split("-")[0]
    return default
