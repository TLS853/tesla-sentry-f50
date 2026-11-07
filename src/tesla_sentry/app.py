"""Own the HTTP server, background workers, signals and USB session lifetime."""
import logging
import os
import signal
import threading
from http.server import ThreadingHTTPServer

from . import archive, auth, config, notifications, retention, usb
from .http import Handler

LOG = logging.getLogger(__name__)


def create_server(host=None, port=None):
    return ThreadingHTTPServer((host if host is not None else config.settings.host,
                                port if port is not None else config.settings.port), Handler)


class BackgroundServices:
    def __init__(self):
        self.stop_event = threading.Event()
        self.workers = []

    def start(self):
        for target in (retention.disk_guard, retention.health_logger, notifications.notify_loop):
            worker = threading.Thread(target=target, args=(self.stop_event,), name=target.__name__, daemon=True)
            worker.start()
            self.workers.append(worker)

    def stop(self):
        self.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=2)
            if worker.is_alive():
                LOG.warning("Worker %s is finishing an outstanding operation", worker.name)


def run(host=None, port=None):
    archive.ensure_dirs()
    server = create_server(host, port)  # Bind before touching hardware or starting workers.
    services = BackgroundServices()
    previous_signals = {}

    def shutdown(signum, frame):
        services.stop_event.set()
        # shutdown() must be called outside the serve_forever thread.
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_signals[sig] = signal.signal(sig, shutdown)
    try:
        if os.path.isfile(usb.IMG) and os.path.isdir(usb.GADGET_DIR):
            ok, message = usb.usb_detach()
            if not ok:
                LOG.warning("USB startup recovery deferred: %s", message)
        LOG.info("Listening on %s:%d (authentication %s)", *server.server_address,
                 "enabled" if auth.AUTH_ON else "disabled")
        services.start()
        server.serve_forever(poll_interval=0.2)
    finally:
        services.stop()
        server.server_close()
        if usb.usb_lease is not None:
            usb.usb_detach()
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    return 0
