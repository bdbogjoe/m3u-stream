import re
from dataclasses import dataclass, field
from typing import Iterable

import requests


_EXTINF_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@dataclass
class Channel:
    id: str
    name: str
    url: str
    group: str = ""
    logo: str = ""


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
            logo = attrs.get("tvg-logo", "")
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                url = lines[j].strip()
                base = attrs.get("tvg-id") or name or url
                seen_ids[base] = seen_ids.get(base, 0) + 1
                cid = base if seen_ids[base] == 1 else f"{base}#{seen_ids[base]}"
                channels.append(Channel(id=cid, name=name, url=url, group=group, logo=logo))
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
