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

    A background thread fetches and parses one or more XMLTV files every
    `ttl` seconds and merges their channels and programmes. `current(tvg_id)`
    returns the programme whose [start, stop) interval contains "now", or None.

    On id collision across feeds, the first feed wins; later feeds only fill
    in channels the earlier ones don't cover.
    """

    def __init__(self, urls: str | list[str], ttl_seconds: int = 6 * 3600):
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        self.urls = list(urls)
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

    def _fetch_one(self, url: str) -> bytes:
        log.info("fetching EPG from %s", url)
        req = urllib.request.Request(url, headers={"User-Agent": "m3u-stream-epg/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data

    def _refresh(self) -> None:
        progs: dict[str, list[Programme]] = {}
        names: dict[str, str] = {}
        merged_root = ET.Element("tv")
        # Some XMLTV feeds (notably epgshare01's FR1) publish each programme
        # twice — once with UTC timestamps, once with the local-tz form. Both
        # parse to the same datetime, so we de-dup on (channel, start instant).
        seen: set[tuple[str, datetime]] = set()
        now = datetime.now(timezone.utc)
        ok_urls = 0
        for url in self.urls:
            try:
                data = self._fetch_one(url)
            except Exception as e:
                log.warning("EPG fetch failed for %s: %s", url, e)
                continue
            ok_urls += 1
            for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
                if elem.tag == "programme":
                    start = _parse_time(elem.get("start", ""))
                    stop = _parse_time(elem.get("stop", ""))
                    cid = elem.get("channel", "")
                    # Only keep programmes that could still be "current" or
                    # "upcoming" — anything that already ended is dead weight.
                    if start and stop and cid and stop > now:
                        key = (cid, start)
                        if key not in seen:
                            seen.add(key)
                            title = (elem.findtext("title") or "").strip()
                            if title:
                                progs.setdefault(cid, []).append(Programme(start, stop, title))
                            merged_root.append(elem)
                            continue  # don't clear — element is now owned by merged_root
                    elem.clear()
                elif elem.tag == "channel":
                    cid = elem.get("id", "")
                    if cid:
                        for d in elem.findall("display-name"):
                            text = (d.text or "").strip()
                            if not text:
                                continue
                            # First feed/name wins on collision; XMLTV typically
                            # lists more "canonical" names earlier in the file.
                            names.setdefault(_norm(text), cid)
                            names.setdefault(_norm(_strip_suffix(text)), cid)
                        # Also map the id itself in case the M3U name matches it.
                        names.setdefault(_norm(cid), cid)
                        names.setdefault(_norm(_strip_suffix(cid)), cid)
                        merged_root.append(elem)
                        continue  # don't clear — element is now owned by merged_root
                    elem.clear()
        if ok_urls == 0:
            raise RuntimeError("no EPG source could be fetched")
        for plist in progs.values():
            plist.sort(key=lambda p: p.start)
        raw_xml = ET.tostring(merged_root, encoding="utf-8", xml_declaration=True)
        with self._lock:
            self._programmes = progs
            self._name_to_id = names
            self._raw_xml = raw_xml
        log.info("EPG loaded: %d channels with programmes, %d display-names from %d/%d source(s)",
                 len(progs), len(names), ok_urls, len(self.urls))

    def _current_for_id(self, cid: str) -> Programme | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(cid, [])
        for p in progs:
            if p.start <= now < p.stop:
                return p
        return None

    def _next_for_id(self, cid: str) -> Programme | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(cid, [])
        # Programmes are sorted by start time at refresh time.
        for p in progs:
            if p.start > now:
                return p
        return None

    def _upcoming_for_id(self, cid: str, n: int) -> list[Programme]:
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(cid, [])
        out: list[Programme] = []
        for p in progs:
            if p.start > now:
                out.append(p)
                if len(out) >= n:
                    break
        return out

    def current(self, tvg_id: str = "", channel_name: str = "") -> Programme | None:
        if tvg_id:
            p = self._current_for_id(tvg_id)
            if p is not None:
                return p
        cid = self.id_for(channel_name)
        if cid:
            return self._current_for_id(cid)
        return None

    def next(self, tvg_id: str = "", channel_name: str = "") -> Programme | None:
        if tvg_id:
            p = self._next_for_id(tvg_id)
            if p is not None:
                return p
        cid = self.id_for(channel_name)
        if cid:
            return self._next_for_id(cid)
        return None

    def format_current(self, tvg_id: str = "", channel_name: str = "") -> str | None:
        p = self.current(tvg_id=tvg_id, channel_name=channel_name)
        if p is None:
            return None
        return f"{p.title} · until {p.stop.astimezone().strftime('%H:%M')}"

    def format_next(self, tvg_id: str = "", channel_name: str = "") -> str | None:
        p = self.next(tvg_id=tvg_id, channel_name=channel_name)
        if p is None:
            return None
        return f"{p.title} · at {p.start.astimezone().strftime('%H:%M')}"

    def upcoming(self, tvg_id: str = "", channel_name: str = "", n: int = 6) -> list[Programme]:
        if tvg_id:
            out = self._upcoming_for_id(tvg_id, n)
            if out:
                return out
        cid = self.id_for(channel_name)
        if cid:
            return self._upcoming_for_id(cid, n)
        return []

    def format_upcoming(self, tvg_id: str = "", channel_name: str = "", n: int = 6) -> list[tuple[str, str]]:
        return [
            (p.start.astimezone().strftime("%H:%M"), p.title)
            for p in self.upcoming(tvg_id=tvg_id, channel_name=channel_name, n=n)
        ]

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
