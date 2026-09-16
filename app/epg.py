import gzip
import io
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

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
    category: str = ""


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

    def __init__(self, urls: str | list[str], ttl_seconds: int = 6 * 3600,
                 wanted: Optional[Callable[[], set[str]]] = None,
                 aliases_path: str = ""):
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        self.urls = list(urls)
        self.ttl = ttl_seconds
        # Called at each refresh to get the normalised names we actually serve.
        # Channels matching none of them are dropped, which is most of a
        # nationwide XMLTV feed. None disables filtering entirely.
        self._wanted = wanted
        # Re-read at every refresh so the file can be edited without a restart.
        self._aliases_path = aliases_path
        self._aliases: dict[str, str] = {}
        self._programmes: dict[str, list[Programme]] = {}
        self._name_to_id: dict[str, str] = {}
        # The merged XMLTV is written to disk, not held in memory: it weighs
        # well over 100 MB and is only ever streamed back out verbatim.
        self._xml_path = os.path.join(tempfile.gettempdir(), "m3u-stream-epg.xml")
        self._xml_ready = False
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

    def _load_aliases(self) -> dict[str, str]:
        """Read the alias file: one `M3U name = EPG name or id` per line.

        Feeds and playlists rarely name a channel the same way — "BeIn FR 1 HD"
        against "beIN SPORTS 1" — and no normalisation rule separates those
        safely from genuinely distinct channels like "Canal+" and "Canal+ Sport".
        Explicit aliases keep the ambiguous cases under the user's control.
        """
        path = self._aliases_path
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.warning("EPG aliases: cannot read %s: %s", path, e)
            return {}
        out: dict[str, str] = {}
        for lineno, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            left, sep, right = line.partition("=")
            left, right = left.strip(), right.strip()
            if not sep or not left or not right:
                log.warning("EPG aliases: ignoring line %d (expected 'name = target'): %s",
                            lineno, line)
                continue
            out[_norm(left)] = right
        if out:
            log.info("EPG aliases: %d entries from %s", len(out), path)
        return out

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
        # Some XMLTV feeds (notably epgshare01's FR1) publish each programme
        # twice — once with UTC timestamps, once with the local-tz form. Both
        # parse to the same datetime, so we de-dup on (channel, start instant).
        seen: set[tuple[str, datetime]] = set()
        now = datetime.now(timezone.utc)
        ok_urls = 0
        aliases = self._load_aliases()
        wanted = self._wanted() if self._wanted is not None else None
        if wanted:
            # Alias targets must survive the filter, or the alias resolves to a
            # channel we just dropped.
            for target in aliases.values():
                wanted.add(_norm(target))
                wanted.add(_norm(_strip_suffix(target)))
        if not wanted:
            # No channels loaded yet (or no filter): keep everything rather
            # than silently serving an empty guide.
            wanted = None
        keep_ids: set[str] = set()
        kept = dropped = 0

        tmp_path = self._xml_path + ".tmp"
        with open(tmp_path, "wb") as out:
            out.write(b'<?xml version="1.0" encoding="utf-8"?>\n<tv>\n')
            for url in self.urls:
                try:
                    data = self._fetch_one(url)
                except Exception as e:
                    log.warning("EPG fetch failed for %s: %s", url, e)
                    continue
                ok_urls += 1
                filtering = wanted is not None
                channels_seen = 0
                context = ET.iterparse(io.BytesIO(data), events=("start", "end"))
                _, root = next(context)
                for event, elem in context:
                    if event != "end":
                        continue
                    if elem.tag == "programme":
                        if filtering and channels_seen == 0:
                            # XMLTV's DTD puts <channel> before <programme>. A
                            # feed that doesn't leaves us no way to know which
                            # ids to keep, so don't filter it at all.
                            log.warning("EPG %s lists programmes before channels; "
                                        "keeping every channel from this source", url)
                            filtering = False
                        start = _parse_time(elem.get("start", ""))
                        stop = _parse_time(elem.get("stop", ""))
                        cid = elem.get("channel", "")
                        # Only keep programmes that could still be "current" or
                        # "upcoming" — anything that already ended is dead weight.
                        if (start and stop and cid and stop > now
                                and (not filtering or cid in keep_ids)):
                            key = (cid, start)
                            if key not in seen:
                                seen.add(key)
                                title = (elem.findtext("title") or "").strip()
                                if title:
                                    # Pick the best category: prefer lang="fr",
                                    # else first non-empty <category>.
                                    category = ""
                                    for cat in elem.findall("category"):
                                        text = (cat.text or "").strip()
                                        if not text:
                                            continue
                                        if cat.get("lang") == "fr":
                                            category = text
                                            break
                                        if not category:
                                            category = text
                                    progs.setdefault(cid, []).append(
                                        Programme(start, stop, title, category))
                                    out.write(ET.tostring(elem, encoding="utf-8"))
                    elif elem.tag == "channel":
                        channels_seen += 1
                        cid = elem.get("id", "")
                        if cid:
                            texts = [(d.text or "").strip() for d in elem.findall("display-name")]
                            texts = [x for x in texts if x]
                            keep = not filtering
                            if filtering:
                                for text in texts + [cid]:
                                    if (_norm(text) in wanted
                                            or _norm(_strip_suffix(text)) in wanted):
                                        keep = True
                                        break
                            if keep:
                                kept += 1
                                keep_ids.add(cid)
                                for text in texts:
                                    # First feed/name wins on collision; XMLTV
                                    # typically lists more "canonical" names first.
                                    names.setdefault(_norm(text), cid)
                                    names.setdefault(_norm(_strip_suffix(text)), cid)
                                # Also map the id itself in case the M3U name matches it.
                                names.setdefault(_norm(cid), cid)
                                names.setdefault(_norm(_strip_suffix(cid)), cid)
                                out.write(ET.tostring(elem, encoding="utf-8"))
                            else:
                                dropped += 1
                    else:
                        continue
                    # Drop the element and detach it from the root, so peak
                    # memory stays flat instead of growing with the feed.
                    elem.clear()
                    root.clear()
            out.write(b"</tv>\n")

        if ok_urls == 0:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise RuntimeError("no EPG source could be fetched")
        for plist in progs.values():
            plist.sort(key=lambda p: p.start)
        os.replace(tmp_path, self._xml_path)
        size_mb = os.path.getsize(self._xml_path) / (1024 * 1024)
        with self._lock:
            self._programmes = progs
            self._name_to_id = names
            self._aliases = aliases
            self._xml_ready = True
        log.info("EPG loaded: %d channels with programmes, %d display-names from %d/%d "
                 "source(s); channels kept %d, dropped %d; xml %.1f MB on disk",
                 len(progs), len(names), ok_urls, len(self.urls), kept, dropped, size_mb)

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

    def _schedule_for_id(self, cid: str) -> list[Programme]:
        """All programmes for `cid` that haven't ended yet, sorted by start."""
        now = datetime.now(timezone.utc)
        with self._lock:
            progs = self._programmes.get(cid, [])
        return [p for p in progs if p.stop > now]

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

    def schedule(self, tvg_id: str = "", channel_name: str = "") -> list[Programme]:
        if tvg_id:
            out = self._schedule_for_id(tvg_id)
            if out:
                return out
        cid = self.id_for(channel_name)
        if cid:
            return self._schedule_for_id(cid)
        return []

    def id_for(self, channel_name: str) -> str | None:
        """Return the XMLTV channel id matching a channel name, or None."""
        if not channel_name:
            return None
        with self._lock:
            # An explicit alias wins over name matching.
            target = self._aliases.get(_norm(channel_name))
            if target is None:
                target = self._aliases.get(_norm(_strip_suffix(channel_name)))
            if target is not None:
                if target in self._programmes:
                    return target          # the alias names an XMLTV id directly
                cid = self._name_to_id.get(_norm(target))
                if cid is not None:
                    return cid
                cid = self._name_to_id.get(_norm(_strip_suffix(target)))
                if cid is not None:
                    return cid
            cid = self._name_to_id.get(_norm(channel_name))
            if cid is not None:
                return cid
            return self._name_to_id.get(_norm(_strip_suffix(channel_name)))

    def raw_xml_path(self) -> str | None:
        """Path of the merged XMLTV on disk, or None if not built yet."""
        with self._lock:
            return self._xml_path if self._xml_ready else None
