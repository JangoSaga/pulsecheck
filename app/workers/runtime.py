import asyncio
import logging
import signal
import sys


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":  # pragma: no cover
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)


log = logging.getLogger("app.workers")
