#!/usr/bin/env python3
"""Serve the source UI on loopback with temporary data and no mutation endpoints."""
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="tsf-ui-preview-") as root:
        for key in tuple(os.environ):
            if key.startswith("TSF_"):
                del os.environ[key]
        os.environ.update(TSF_DIR=root + "/install", TSF_ARCHIVE=root + "/archive",
                          TSF_CONF=root + "/no-config", TSF_AUTH_FILE=root + "/no-auth", TSF_USB_LOCK=root + "/usb.lock")
        from tesla_sentry import archive, assets
        from tesla_sentry.http import Handler
        archive.ensure_dirs()

        class Preview(Handler):
            def _get_home(self, path):
                assets.HTML = assets.resource("index.html").decode("utf-8")
                assets.LANG_TABLE = json.loads(assets.resource("i18n.json"))
                assets.I18N_JS = json.dumps(assets.LANG_TABLE, ensure_ascii=False)
                return super()._get_home(path)

            def _get_login(self, path):
                assets.LOGIN_HTML = assets.resource("login.html").decode("utf-8")
                self._login_page()

            def do_POST(self):
                self._json({"ok": False, "msg": "本机预览不执行写入操作"}, 405)

            do_DELETE = do_POST

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Preview)
        print("UI preview: http://127.0.0.1:%d (temporary data, writes disabled)" % server.server_port, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
