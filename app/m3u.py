import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

import requests


_EXTINF_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_SCHEME = re.compile(r'https?://')

# Logo hosts known to never deliver an image from this app's vantage point
# (auth-walled, geo-blocked, broken, etc.). Channels whose tvg-logo points at
# any of these (or a subdomain) get an empty logo so we don't waste a /logo
# round-trip per render.
_BLOCKED_LOGO_HOSTS = ("free.fr",)


def _clean_url(url: str) -> str:
    """Some M3U feeds concatenate a base URL without a separating slash,
    producing values like `http://host:portHTTPS://real/logo.png`.
    Detect a second scheme and keep the trailing real URL."""
    if not url:
        return url
    matches = list(_SCHEME.finditer(url))
    if len(matches) > 1:
        return url[matches[-1].start():]
    return url


def _clean_logo(url: str) -> str:
    cleaned = _clean_url(url)
    if not cleaned:
        return cleaned
    host = (urlparse(cleaned).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _BLOCKED_LOGO_HOSTS):
        return ""
    return cleaned


@dataclass
class Channel:
    id: str
    name: str
    url: str
    group: str = ""
    logo: str = ""
    source: str = ""
    tvg_id: str = ""


def parse(text: str) -> list[Channel]:
    channels: list[Channel] = []
    lines = text.splitlines()
    i = 0
    seen_ids: dict[str, int] = {}
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            attrs = dict(_EXTINF_ATTR.findall(line))
            name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            group = attrs.get("group-title", "")
            logo = _clean_logo(attrs.get("tvg-logo", ""))
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                url = lines[j].strip()
                tvg_id = attrs.get("tvg-id", "")
                base = tvg_id or name or url
                seen_ids[base] = seen_ids.get(base, 0) + 1
                cid = base if seen_ids[base] == 1 else f"{base}#{seen_ids[base]}"
                channels.append(Channel(id=cid, name=name, url=url, group=group,
                                        logo=logo, tvg_id=tvg_id))
                i = j
        i += 1
    return channels


def fetch(url: str, timeout: int = 15) -> list[Channel]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return parse(r.text)


def groups(channels: Iterable[Channel]) -> list[str]:
    seen: dict[str, None] = {}
    for c in channels:
        if c.group:
            seen.setdefault(c.group, None)
    return list(seen.keys())


def sources(channels: Iterable[Channel]) -> list[str]:
    seen: dict[str, None] = {}
    for c in channels:
        if c.source:
            seen.setdefault(c.source, None)
    return list(seen.keys())
