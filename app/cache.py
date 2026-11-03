"""On-disk copies of what a restart would otherwise download again.

Everything here is a cache: a missing, unreadable or corrupt entry only
means the data is fetched as if the process had never run before.
"""
import hashlib
import logging
import os
import tempfile

log = logging.getLogger("m3u-stream.cache")


def cache_dir() -> str:
    """CACHE_DIR, or a directory under the system temp dir. Mount a volume
    there in Docker, or a recreated container starts cold again."""
    path = os.environ.get("CACHE_DIR") or os.path.join(tempfile.gettempdir(), "m3u-stream")
    os.makedirs(path, exist_ok=True)
    return path


def path_for(name: str) -> str:
    return os.path.join(cache_dir(), name)


def key_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def read_text(name: str) -> str | None:
    try:
        with open(path_for(name), "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("cannot read cache %s: %s", name, e)
        return None


def write_text(name: str, text: str) -> None:
    """Atomic, so a crash mid-write never leaves a truncated entry behind."""
    path = path_for(name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("cannot write cache %s: %s", name, e)
