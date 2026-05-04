"""HTTP upstream reader with automatic reconnect.

The IPTV upstream this project relays from closes the HTTP response every few
seconds. This reader transparently reopens the connection and continues
yielding bytes. Deduplication of replayed content is handled by the TS-aware
filter (see app/tsdedup.py) — this module is intentionally a thin reconnect
loop that does not touch payload semantics.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional

log = logging.getLogger("m3u-stream.upstream")

READ_CHUNK = 65536
READ_TIMEOUT = 30.0
RECONNECT_BACKOFF_BASE = 0.25
RECONNECT_BACKOFF_MAX = 5.0
RECONNECT_MAX_ATTEMPTS = 12


class UpstreamReader:
    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.headers.setdefault("Accept-Encoding", "identity")
        self.headers.setdefault("User-Agent", "m3u-stream/1.0")
        self._stop = False
        self._resp = None  # type: ignore[assignment]

    def close(self) -> None:
        self._stop = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _open(self):
        req = urllib.request.Request(self.url, headers=self.headers)
        return urllib.request.urlopen(req, timeout=READ_TIMEOUT)

    def stream(self) -> Iterator[bytes]:
        attempt = 0
        while not self._stop:
            try:
                resp = self._open()
            except (urllib.error.URLError, OSError) as e:
                attempt += 1
                if attempt > RECONNECT_MAX_ATTEMPTS:
                    log.error("upstream open failed permanently: %s", e)
                    return
                delay = min(RECONNECT_BACKOFF_BASE * (2 ** min(attempt, 5)),
                            RECONNECT_BACKOFF_MAX)
                log.warning("upstream open failed (%s), retrying in %.2fs", e, delay)
                time.sleep(delay)
                continue

            self._resp = resp
            attempt = 0

            try:
                while not self._stop:
                    try:
                        chunk = resp.read(READ_CHUNK)
                    except Exception as e:
                        log.warning("upstream read error: %s", e)
                        break
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                if self._resp is resp:
                    self._resp = None

            if self._stop:
                return
            log.info("upstream connection closed, reconnecting")
            time.sleep(RECONNECT_BACKOFF_BASE)
