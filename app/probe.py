import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import requests

from .m3u import Channel

log = logging.getLogger("m3u-stream.probe")


def probe_url(url: str, timeout: float = 4.0) -> bool:
    """Reachability check that actually verifies the stream produces bytes.

    Some IPTV upstreams happily return HTTP 200 with an empty body when
    the channel is broken or the token has been consumed (a HEAD-only
    or status-only check would incorrectly report those as "online" —
    matching what ffmpeg sees when it fails with "Stream ends
    prematurely at 0"). So we follow redirects, do a streaming GET, and
    require at least one non-empty chunk back within `timeout` seconds.
    """
    headers = {"Accept-Encoding": "identity"}
    try:
        with requests.get(url, timeout=timeout, stream=True,
                          allow_redirects=True, headers=headers) as r:
            if r.status_code >= 400:
                return False
            for chunk in r.iter_content(1024):
                if chunk:
                    return True
            return False
    except requests.exceptions.RequestException:
        return False


class Prober:
    """Concurrent reachability probes for a list of Channels.

    Runs one batch on demand. The result dict (`status`) is replaced
    atomically when a batch finishes, so readers only see fully-formed
    snapshots.
    """

    def __init__(self, max_workers: int = 16, timeout: float = 4.0,
                 busy: Callable[[], bool] = lambda: False):
        self.max_workers = max_workers
        self.timeout = timeout
        self._busy = busy
        self._lock = threading.Lock()
        self._status: dict[str, bool] = {}
        self._running = False

    def status(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._status)

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, channels: list[Channel]) -> bool:
        """Kick off a probe in the background. Returns False if one is
        already in flight (the caller can retry later)."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run, args=(list(channels),), daemon=True).start()
        return True

    def _run(self, channels: list[Channel]) -> None:
        # A film library runs to thousands of entries: opening each one every
        # cycle would hammer the provider for no use, as files don't go
        # offline the way live channels do. They stay "not probed".
        channels = [c for c in channels if not c.vod]
        log.info("probing %d channels", len(channels))
        previous = self.status()
        skipped = 0

        def probe(c: Channel):
            # A probe is a streaming connection like any other: on a
            # subscription that allows a single one, it cuts whoever is
            # watching. The channel keeps its last known state instead.
            nonlocal skipped
            if self._busy():
                skipped += 1
                return previous.get(c.id)
            return probe_url(c.url, self.timeout)

        results: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {pool.submit(probe, c): c for c in channels}
            for fut in futs:
                c = futs[fut]
                try:
                    result = fut.result()
                except Exception:
                    result = False
                if result is not None:
                    results[c.id] = result
        ok = sum(1 for v in results.values() if v)
        log.info("probe done: %d/%d online%s", ok, len(results),
                 f", {skipped} skipped while a stream was playing" if skipped else "")
        with self._lock:
            self._status = results
            self._running = False
