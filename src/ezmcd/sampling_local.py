"""App-scoped sampling transport. Never writes macOS network settings.

Native routing is experimental until independently verified on the supported
App. Only wallet collection uses it; it is not an order-submission safeguard.
"""
import asyncio
import math
import os
import threading
import time
from pathlib import Path

from .errors import EzMcdError
from .storage import write_private

TRANSPORT = "local-app-v1"
# A positive process selector, never bare `local` or an exclusion selector.
# mitmproxy matches this against executable paths, including translocated Apps.
LOCAL_MODE = "local:/McdApp.app/McdApp"
PROBE_MODE = "regular@127.0.0.1:18089"


def ready_servers(proxyserver):
    """A listening probe socket alone does not prove local routing is ready."""
    if proxyserver is None or proxyserver.servers.is_updating:
        return False
    servers = list(proxyserver.servers)
    return (len(servers) == 2 and {s.mode.full_spec for s in servers} == {LOCAL_MODE, PROBE_MODE}
            and all(s.is_running and s.last_exception is None for s in servers))


class LocalCaptureLifecycle:
    """Bound child lifetime even when the parent terminal disappears."""
    def __init__(self, config):
        if (config.get("transport") != TRANSPORT or config.get("local_mode") != LOCAL_MODE
                or type(config.get("parent_pid")) is not int or config["parent_pid"] <= 1
                or type(config.get("deadline")) not in (int, float) or not math.isfinite(config["deadline"])
                or config.get("purpose") not in ("account_connect", "account_maintenance")):
            raise EzMcdError("sampling_local_scope_invalid")
        self.config = config
        self.task = None
        self.stopped = threading.Event()

    def load(self, loader):
        # Server startup can wait inside a native extension before `running`.
        # Keep a separate clock/thread so even that wait (or a stalled event
        # loop) cannot outlive the parent or retain capture indefinitely.
        threading.Thread(target=self.emergency_watch, daemon=True).start()

    def emergency_watch(self):
        maximum = time.monotonic() + min(150, max(0, self.config["deadline"] - time.time()) + 10)
        while not self.stopped.wait(.1):
            if os.getppid() == self.config["parent_pid"] and time.monotonic() < maximum:
                continue
            # Prefer graceful asyncio shutdown, but bound an unresponsive
            # native startup or event loop. Exit only this owned proxy child.
            if not self.stopped.wait(5):
                try:
                    write_private(Path(self.config["output"]) / "transport-emergency-stop.json",
                        {"reason": "parent_exited_or_deadline"})
                finally:
                    os._exit(70)
            return

    def running(self):
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        from mitmproxy import ctx
        ready = False
        # This is a monotonic upper bound even if the wall clock changes.
        maximum = time.monotonic() + min(150, max(0, self.config["deadline"] - time.time()) + 10)
        try:
            while True:
                if os.getppid() != self.config["parent_pid"]:
                    reason = "parent_exited"
                    break
                if time.monotonic() >= maximum or time.time() > self.config["deadline"] + 10:
                    reason = "deadline"
                    break
                if ready_servers(ctx.master.addons.get("proxyserver")):
                    if not ready:
                        write_private(Path(self.config["output"]) / "transport-ready.json",
                            {"transport": TRANSPORT, "local_mode": LOCAL_MODE, "system_proxy_modified": False})
                        ready = True
                elif ready:
                    reason = "routing_stopped"
                    break
                await asyncio.sleep(.1)
            write_private(Path(self.config["output"]) / "transport-stop.json", {"reason": reason})
        finally:
            ctx.master.shutdown()

    def done(self):
        self.stopped.set()
        if self.task is not None:
            self.task.cancel()
