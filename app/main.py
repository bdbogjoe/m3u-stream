import logging
import os
import posixpath
import subprocess
import sys
from urllib.parse import urlparse

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    __package__ = "app"

import requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request

from . import dlna, m3u
from .state import AppState

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("m3u-stream")


def _detect_host_ip(target: str) -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", target], text=True)
        for parts in (line.split() for line in out.splitlines()):
            if "src" in parts:
                return parts[parts.index("src") + 1]
    except Exception as e:
        log.warning("ip route detection failed: %s", e)
    return "127.0.0.1"


def _parse_sources(raw: str) -> list[tuple[str, str]]:
    """Parse a comma-separated list of M3U URLs.

    Items may be either a bare URL or `name=url`. The source name for a bare
    URL is derived from the last path segment without the extension.
    """
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, _, url = item.partition("=")
            if url.startswith(("http://", "https://")):
                out.append((name.strip(), url.strip()))
                continue
        path = urlparse(item).path
        name = posixpath.splitext(posixpath.basename(path))[0] or item
        out.append((name, item))
    return out


def _load_all(sources: list[tuple[str, str]]) -> list[m3u.Channel]:
    all_channels: list[m3u.Channel] = []
    for name, url in sources:
        channels = m3u.fetch(url)
        for c in channels:
            c.source = name
            c.id = f"{name}:{c.id}"
        log.info("loaded %d channels from %s (%s)", len(channels), name, url)
        all_channels.extend(channels)
    return all_channels


def create_app() -> Flask:
    m3u_url = os.environ.get("M3U_URL")
    tv_ip = os.environ.get("TV_IP") or None
    if not m3u_url:
        log.error("M3U_URL must be set")
        sys.exit(2)
    if not tv_ip:
        log.warning("TV_IP not set — Cast to TV is disabled")

    relay_port = int(os.environ.get("RELAY_PORT", "8888"))
    host_ip = os.environ.get("HOST_IP") or _detect_host_ip(tv_ip or "1.1.1.1")
    lan_base_url = f"http://{host_ip}:{relay_port}"
    relay_base_url_env = (os.environ.get("RELAY_BASE_URL") or "").rstrip("/")
    log.info("host_ip=%s tv_ip=%s relay_port=%d relay_base_url=%s",
             host_ip, tv_ip or "(disabled)", relay_port, relay_base_url_env or "(off)")

    sources = _parse_sources(m3u_url)
    if not sources:
        log.error("M3U_URL did not yield any URL")
        sys.exit(2)

    state = AppState(relay_port)
    try:
        state.channels = _load_all(sources)
        log.info("loaded %d channels total from %d source(s)",
                 len(state.channels), len(sources))
    except Exception as e:
        log.error("failed to fetch M3U at startup: %s", e)
        sys.exit(2)

    app = Flask(__name__)
    app.config["state"] = state
    app.config["sources"] = sources
    app.config["tv_ip"] = tv_ip
    app.config["host_ip"] = host_ip

    def _ensure_control_url() -> str:
        if not tv_ip:
            raise dlna.DLNAError("Cast disabled: TV_IP is not set")
        if state.control_url is None:
            state.control_url = dlna.discover_control_url(tv_ip)
            log.info("AVTransport control URL: %s", state.control_url)
        return state.control_url

    # LAN-direct URL used for DLNA cast — the TV needs to reach the relay
    # by IP and won't resolve an external/proxied hostname.
    cast_stream_url = f"{lan_base_url}/stream.ts"

    def _is_proxied() -> bool:
        h = request.headers
        return any(h.get(x) for x in ("X-Forwarded-Host", "X-Forwarded-Proto",
                                      "X-Forwarded-For", "Forwarded"))

    def _public_base() -> str:
        if relay_base_url_env and _is_proxied():
            return relay_base_url_env
        return lan_base_url

    def _channel_dto(c):
        return {"id": c.id, "name": c.name, "group": c.group,
                "logo": c.logo, "source": c.source, "url": c.url}

    def _status_dto():
        base = _public_base()
        return {
            "current": _channel_dto(state.current) if state.current else None,
            "casting": state.casting,
            "streaming": state.relay.running,
            "stream_url": f"{base}/stream.ts" if state.relay.running else None,
            "mp4_url": f"{base}/stream.mp4" if state.relay.running else None,
            "cast_enabled": bool(tv_ip),
        }

    def _stop_everything() -> None:
        if state.casting and state.control_url:
            try:
                dlna.stop(state.control_url)
            except Exception as e:
                log.warning("dlna stop failed: %s", e)
        state.casting = False
        state.relay.stop()
        state.current = None

    def _start_relay(channel) -> None:
        if state.relay.running:
            _stop_everything()
        state.relay.start(channel.url)
        state.current = channel

    @app.get("/logo")
    def logo_route():
        url = request.args.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return ("", 400)
        try:
            r = requests.get(url, timeout=5, verify=False, stream=True,
                             headers={"User-Agent": "m3u-stream/1.0"})
        except Exception:
            return ("", 502)
        if r.status_code != 200:
            return ("", 404)
        ct = r.headers.get("Content-Type", "image/png")
        resp = Response(r.iter_content(8192), content_type=ct)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.get("/")
    def index():
        channels = sorted(
            state.channels,
            key=lambda c: (c.source.lower(), c.group.lower(), c.name.lower()),
        )
        groups = sorted(m3u.groups(state.channels), key=str.lower)
        srcs = sorted(m3u.sources(state.channels), key=str.lower)
        return render_template(
            "index.html",
            channels=channels,
            groups=groups,
            sources=srcs,
            status=_status_dto(),
            mp4_url=f"{_public_base()}/stream.mp4",
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, channels=len(state.channels))

    @app.get("/status")
    def status_route():
        return jsonify(ok=True, **_status_dto())

    @app.post("/cast")
    def cast():
        data = request.get_json(silent=True) or {}
        cid = data.get("channel_id")
        if not cid:
            return jsonify(ok=False, error="channel_id required"), 400
        channel = state.channel_by_id(cid)
        if channel is None:
            return jsonify(ok=False, error="unknown channel"), 404
        with state.lock:
            try:
                control_url = _ensure_control_url()
            except Exception as e:
                return jsonify(ok=False, error=f"discovery: {e}"), 502
            try:
                _start_relay(channel)
                dlna.set_uri(control_url, cast_stream_url, channel.name)
                dlna.play(control_url)
                state.casting = True
            except Exception as e:
                _stop_everything()
                log.exception("cast failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, **_status_dto())

    @app.post("/stream")
    def stream_route():
        data = request.get_json(silent=True) or {}
        cid = data.get("channel_id")
        if not cid:
            return jsonify(ok=False, error="channel_id required"), 400
        channel = state.channel_by_id(cid)
        if channel is None:
            return jsonify(ok=False, error="unknown channel"), 404
        with state.lock:
            try:
                _start_relay(channel)
            except Exception as e:
                _stop_everything()
                log.exception("stream start failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, **_status_dto())

    @app.post("/stop")
    def stop_route():
        with state.lock:
            try:
                _stop_everything()
            except Exception as e:
                log.exception("stop failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, **_status_dto())

    @app.post("/reload")
    def reload_route():
        try:
            channels = _load_all(sources)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 502
        with state.lock:
            state.channels = channels
        return jsonify(ok=True, count=len(channels))

    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("WEB_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
