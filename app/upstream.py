"""HTTP upstream reader with automatic reconnect.

The IPTV upstream this project relays from closes the HTTP response every few
seconds. This reader transparently reopens the connection and continues
yielding bytes. Deduplication of replayed content is handled by the TS-aware
filter (see app/tsdedup.py) — this module is intentionally a thin reconnect
loop that does not touch payload semantics.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator, Optional

log = logging.getLogger("m3u-stream.upstream")

READ_CHUNK = 65536
READ_TIMEOUT = 30.0
RECONNECT_BACKOFF_BASE = 0.25
RECONNECT_BACKOFF_MAX = 5.0
RECONNECT_MAX_ATTEMPTS = 12
# Give up if we open the upstream this many times in a row and each response
# yields zero bytes — usually means the channel is dead but the HTTP server
# still answers 200. Bailing out lets the consumer (ffmpeg) wind down so
# downstream client-disconnect handling can run.
EMPTY_BODY_GIVE_UP = 5
# The upstream drops the connection about every 28s. Opening the next one
# before that happens means its replayed head is already downloaded when the
# current one goes, so the consumer never runs dry.
OVERLAP_AFTER = 18.0
OVERLAP_BUFFER_MAX = 16 << 20


class _Prefetch:
    """Opens and buffers the next upstream connection in the background."""

    def __init__(self, opener: Callable[[], object]) -> None:
        self._opener = opener
        self.resp = None
        self.error: Optional[BaseException] = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            resp = self._opener()
        except BaseException as e:
            self.error = e
            return
        self.resp = resp
        while not self._stop.is_set():
            try:
                chunk = resp.read(READ_CHUNK)
            except Exception:
                break
            if not chunk:
                break
            with self._lock:
                if len(self._buf) + len(chunk) > OVERLAP_BUFFER_MAX:
                    break  # keep the connection, just stop filling
                self._buf.extend(chunk)

    def take(self):
        """Stop buffering and hand over the response plus what was read."""
        self._stop.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
        return self.resp, data

    def abort(self) -> None:
        self._stop.set()
        resp = self.resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


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
        consecutive_empty = 0
        resp = None
        carry = b""          # already fetched from the pre-opened connection
        while not self._stop:
            if resp is None:
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
            bytes_this_session = 0
            prefetch: Optional[_Prefetch] = None
            started = time.monotonic()

            if carry:
                bytes_this_session += len(carry)
                yield carry
                carry = b""

            try:
                while not self._stop:
                    try:
                        chunk = resp.read(READ_CHUNK)
                    except Exception as e:
                        log.warning("upstream read error: %s", e)
                        break
                    if not chunk:
                        break
                    bytes_this_session += len(chunk)
                    yield chunk
                    if (prefetch is None
                            and time.monotonic() - started >= OVERLAP_AFTER):
                        prefetch = _Prefetch(self._open)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                if self._resp is resp:
                    self._resp = None
            resp = None

            if self._stop:
                if prefetch is not None:
                    prefetch.abort()
                return

            # Hand over to the connection opened while the last one was alive:
            # its replayed head is already downloaded, so there is no gap.
            if prefetch is not None:
                # take() joins the worker first: asking for .resp before that
                # would call a still-opening connection a failure.
                new_resp, ready = prefetch.take()
                if new_resp is not None:
                    resp, carry = new_resp, ready
                    consecutive_empty = 0
                    log.info("upstream handed over to pre-opened connection "
                             "(%d bytes ready)", len(carry))
                    continue
                log.warning("upstream pre-open failed (%s), falling back",
                            prefetch.error)
                prefetch.abort()

            if bytes_this_session == 0:
                consecutive_empty += 1
                if consecutive_empty >= EMPTY_BODY_GIVE_UP:
                    log.error("upstream produced %d consecutive empty responses; giving up",
                              consecutive_empty)
                    return
            else:
                consecutive_empty = 0
            log.info("upstream connection closed, reconnecting")
            time.sleep(RECONNECT_BACKOFF_BASE)
