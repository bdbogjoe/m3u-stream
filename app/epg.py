import gzip
import io
import logging
import re
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("m3u-stream.epg")

_NORM_RE = re.compile(r"[^a-z0-9]+")
# Common quality / variant suffixes that show up on the M3U side but rarely
# in the XMLTV display-name (and vice-versa). Strip iteratively from the
# end so "France 2 HD +4" → "France 2 HD" → "France 2".
_SUFFIX_RE = re.compile(r"\s+(?:hd|uhd|fhd|4k|sd|hevc|\+\d+)\s*$", re.IGNORECASE)


def _norm(name: str) -> str:
    # Strip accents/diacritics first so "Chérie 25" → "cherie25" matches
    # both the M3U side and an XMLTV display-name with the same accents.
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return _NORM_RE.sub("", ascii_only.lower())


def _strip_suffix(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX_RE.sub("", name).strip()
    return name


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
        self._name_to_id: dict[str, str] = {}
        self._raw_xml: bytes = b""
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
        raw_xml = data
        progs: dict[str, list[Programme]] = {}
        names: dict[str, str] = {}
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
                cid = elem.get("id", "")
                if cid:
                    for d in elem.findall("display-name"):
                        text = (d.text or "").strip()
                        if not text:
                            continue
                        # First channel wins on collision; XMLTV typically
                        # lists more "canonical" names earlier in the file.
                        names.setdefault(_norm(text), cid)
                        names.setdefault(_norm(_strip_suffix(text)), cid)
                    # Also map the id itself in case the M3U name matches it.
                    names.setdefault(_norm(cid), cid)
                    names.setdefault(_norm(_strip_suffix(cid)), cid)
                elem.clear()
        for plist in progs.values():
            plist.sort(key=lambda p: p.start)
        with self._lock:
            self._programmes = progs
            self._name_to_id = names
            self._raw_xml = raw_xml
        log.info("EPG loaded: %d channels with programmes, %d display-names",
                 len(progs), len(names))

    def _current_for_id(self, cid: str) -> Programme | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(cid, [])
        for p in progs:
            if p.start <= now < p.stop:
                return p
        return None

    def current(self, tvg_id: str = "", channel_name: str = "") -> Programme | None:
        if tvg_id:
            p = self._current_for_id(tvg_id)
            if p is not None:
                return p
        if channel_name:
            with self._lock:
                cid = self._name_to_id.get(_norm(channel_name))
            if cid:
                return self._current_for_id(cid)
        return None

    def format_current(self, tvg_id: str = "", channel_name: str = "") -> str | None:
        p = self.current(tvg_id=tvg_id, channel_name=channel_name)
        if p is None:
            return None
        return f"{p.title} · until {p.stop.astimezone().strftime('%H:%M')}"

    def id_for(self, channel_name: str) -> str | None:
        """Return the XMLTV channel id matching a channel name, or None."""
        if not channel_name:
            return None
        with self._lock:
            cid = self._name_to_id.get(_norm(channel_name))
            if cid is not None:
                return cid
            return self._name_to_id.get(_norm(_strip_suffix(channel_name)))

    def raw_xml(self) -> bytes:
        """Return the cached XMLTV bytes (uncompressed) for serving as-is."""
        with self._lock:
            return self._raw_xml
