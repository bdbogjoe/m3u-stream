import gzip
import io
import logging
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("m3u-stream.epg")


@dataclass(frozen=True)
class Programme:
    start: datetime
    stop: datetime
    title: str


def _parse_time(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S %z")
    except (ValueError, TypeError):
        return None


class EPG:
    """In-memory XMLTV index keyed by `<channel id>`.

    A background thread fetches and parses the XMLTV file every `ttl`
    seconds. `current(tvg_id)` returns the programme whose [start, stop)
    interval contains "now", or None.
    """

    def __init__(self, url: str, ttl_seconds: int = 6 * 3600):
        self.url = url
        self.ttl = ttl_seconds
        self._programmes: dict[str, list[Programme]] = {}
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        threading.Thread(target=self._refresher, daemon=True).start()

    def _refresher(self) -> None:
        while True:
            try:
                self._refresh()
                self._loaded.set()
            except Exception as e:
                log.warning("refresh failed: %s", e)
                # back off a minute on failure rather than waiting the full TTL
                time.sleep(60)
                continue
            time.sleep(self.ttl)

    def _refresh(self) -> None:
        log.info("fetching EPG from %s", self.url)
        req = urllib.request.Request(self.url, headers={"User-Agent": "m3u-stream-epg/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        progs: dict[str, list[Programme]] = {}
        now = datetime.now(timezone.utc)
        for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
            if elem.tag == "programme":
                start = _parse_time(elem.get("start", ""))
                stop = _parse_time(elem.get("stop", ""))
                cid = elem.get("channel", "")
                # Only keep programmes that could still be "current" or
                # "upcoming" — anything that already ended is dead weight.
                if start and stop and cid and stop > now:
                    title = (elem.findtext("title") or "").strip()
                    if title:
                        progs.setdefault(cid, []).append(Programme(start, stop, title))
                elem.clear()
            elif elem.tag == "channel":
                elem.clear()
        for plist in progs.values():
            plist.sort(key=lambda p: p.start)
        with self._lock:
            self._programmes = progs
        log.info("EPG loaded: %d channels", len(progs))

    def current(self, tvg_id: str) -> Programme | None:
        if not tvg_id:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(tvg_id, [])
        for p in progs:
            if p.start <= now < p.stop:
                return p
        return None

    def format_current(self, tvg_id: str) -> str | None:
        p = self.current(tvg_id)
        if p is None:
            return None
        return f"{p.title} · until {p.stop.astimezone().strftime('%H:%M')}"
