import logging
import threading
from concurrent.futures import ThreadPoolExecutor

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

    def __init__(self, max_workers: int = 16, timeout: float = 4.0):
        self.max_workers = max_workers
        self.timeout = timeout
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
        log.info("probing %d channels", len(channels))
        results: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {pool.submit(probe_url, c.url, self.timeout): c for c in channels}
            for fut in futs:
                c = futs[fut]
                try:
                    results[c.id] = fut.result()
                except Exception:
                    results[c.id] = False
        ok = sum(1 for v in results.values() if v)
        log.info("probe done: %d/%d online", ok, len(results))
        with self._lock:
            self._status = results
            self._running = False
