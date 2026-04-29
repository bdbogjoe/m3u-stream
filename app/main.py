import atexit
import base64
import hashlib
import ipaddress
import logging
import os
import posixpath
import secrets
import signal
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
from .epg import EPG
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


def _channel_hash_id(source: str, natural: str) -> str:
    return hashlib.sha1(f"{source}:{natural}".encode("utf-8")).hexdigest()[:10]


def _load_all(sources: list[tuple[str, str]]) -> list[m3u.Channel]:
    all_channels: list[m3u.Channel] = []
    for name, url in sources:
        channels = m3u.fetch(url)
        for c in channels:
            c.source = name
            c.id = _channel_hash_id(name, c.id)
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
    web_port = int(os.environ.get("WEB_PORT", "8080"))
    host_ip = os.environ.get("HOST_IP") or _detect_host_ip(tv_ip or "1.1.1.1")
    lan_base_url = f"http://{host_ip}:{relay_port}"
    relay_base_url_env = (os.environ.get("RELAY_BASE_URL") or "").rstrip("/")
    web_lan_url = f"http://{host_ip}:{web_port}"
    web_public_url = (os.environ.get("WEB_BASE_URL") or "").rstrip("/")

    auth_user = os.environ.get("AUTH_USER", "")
    auth_pass = os.environ.get("AUTH_PASS", "")
    auth_enabled = bool(auth_user and auth_pass)
    default_cidrs = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7"
    cidrs_str = os.environ.get("AUTH_TRUSTED_CIDRS", default_cidrs)
    trusted_nets = []
    for c in cidrs_str.split(","):
        c = c.strip()
        if not c:
            continue
        try:
            trusted_nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError as e:
            log.warning("ignoring invalid CIDR %s: %s", c, e)
    if auth_enabled:
        log.info("basic auth   = enabled (proxied requests only)")
        log.info("auth bypass  = %s", ",".join(str(n) for n in trusted_nets))
    log.info("host_ip       = %s", host_ip)
    log.info("tv_ip         = %s  (cast %s)",
             tv_ip or "-", "enabled" if tv_ip else "disabled")
    log.info("web UI (LAN)  = %s", web_lan_url)
    if web_public_url:
        log.info("web UI (public)= %s", web_public_url)
    log.info("relay (LAN)   = %s/<id>/stream.{ts,mp4,m3u8}", lan_base_url)
    if relay_base_url_env:
        log.info("relay (public)= %s/<id>/stream.{ts,mp4,m3u8}", relay_base_url_env)

    sources = _parse_sources(m3u_url)
    if not sources:
        log.error("M3U_URL did not yield any URL")
        sys.exit(2)

    state = AppState(relay_port, web_url=web_lan_url, web_public_url=web_public_url,
                     auth_user=auth_user, auth_pass=auth_pass,
                     auth_trusted_nets=trusted_nets)
    try:
        state.channels = _load_all(sources)
        log.info("loaded %d channels total from %d source(s)",
                 len(state.channels), len(sources))
    except Exception as e:
        log.error("failed to fetch M3U at startup: %s", e)
        sys.exit(2)

    epg_url = (os.environ.get("EPG_URL") or "").strip()
    epg: EPG | None = EPG(epg_url) if epg_url else None
    if epg:
        log.info("EPG_URL       = %s", epg_url)

    app = Flask(__name__)
    app.config["state"] = state
    app.config["sources"] = sources
    app.config["tv_ip"] = tv_ip
    app.config["host_ip"] = host_ip

    def _shutdown_cleanup():
        # Tell the TV to stop pulling our relay URL — otherwise webOS keeps
        # trying long after the process is gone.
        if state.casting and state.control_url:
            try:
                dlna.stop(state.control_url)
                log.info("shutdown: sent Stop to TV")
            except Exception as e:
                log.warning("shutdown: dlna stop failed: %s", e)
        try:
            state.relay.shutdown()
        except Exception:
            pass

    atexit.register(_shutdown_cleanup)
    # SIGTERM (Docker stop, IDE stop) doesn't trigger atexit on its own.
    def _on_term(signum, _frame):
        _shutdown_cleanup()
        # Re-raise default behaviour so the process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_term)
        except (ValueError, OSError):
            # Werkzeug's reloader runs the app in a child thread where
            # signal handlers can't be installed; not fatal.
            pass

    def _check_basic_auth(header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        u, _, p = decoded.partition(":")
        return secrets.compare_digest(u, auth_user) and secrets.compare_digest(p, auth_pass)

    def _client_ip_trusted(client_ip: str) -> bool:
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(ip in net for net in trusted_nets)

    @app.before_request
    def _require_auth_when_proxied():
        if not auth_enabled:
            return None
        if request.path == "/healthz":
            return None
        proxied = any(request.headers.get(h) for h in
                      ("X-Forwarded-Host", "X-Forwarded-Proto",
                       "X-Forwarded-For", "Forwarded"))
        if not proxied:
            return None
        # Skip auth for clients on the LAN even when reached via the proxy.
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        client_ip = xff or (request.remote_addr or "")
        if client_ip and _client_ip_trusted(client_ip):
            return None
        if _check_basic_auth(request.headers.get("Authorization")):
            return None
        return Response(
            "auth required\n",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="m3u-stream"'},
        )

    def _ensure_control_url() -> str:
        if not tv_ip:
            raise dlna.DLNAError("Cast disabled: TV_IP is not set")
        if state.control_url is None:
            state.control_url = dlna.discover_control_url(tv_ip)
            log.info("AVTransport control URL: %s", state.control_url)
        return state.control_url

    def _is_proxied() -> bool:
        h = request.headers
        return any(h.get(x) for x in ("X-Forwarded-Host", "X-Forwarded-Proto",
                                      "X-Forwarded-For", "Forwarded"))

    def _public_base() -> str:
        if relay_base_url_env and _is_proxied():
            return relay_base_url_env
        return lan_base_url

    def _enc(cid: str) -> str:
        from urllib.parse import quote
        return quote(cid, safe="")

    def _cast_stream_url(channel) -> str:
        # LAN-direct: the TV won't resolve an external/proxied hostname.
        return f"{lan_base_url}/{_enc(channel.id)}/stream.ts"

    def _channel_dto(c):
        base = _public_base()
        cid_enc = _enc(c.id)
        return {
            "id": c.id, "name": c.name, "group": c.group,
            "logo": c.logo, "source": c.source, "url": c.url,
            "watch_mp4_url": f"{base}/{cid_enc}/stream.mp4",
            "watch_hls_url": f"{base}/{cid_enc}/stream.m3u8",
            "current_programme": epg.format_current(c.tvg_id) if epg else None,
        }

    def _status_dto():
        return {
            "current": _channel_dto(state.current) if state.current else None,
            "casting": state.casting,
            "cast_enabled": bool(tv_ip),
        }

    def _stop_cast() -> None:
        if state.casting and state.control_url:
            try:
                dlna.stop(state.control_url)
            except Exception as e:
                log.warning("dlna stop failed: %s", e)
        if state.casting and state.current is not None:
            # Drop the cast channel's relay state so its HLS ffmpeg (if any)
            # is killed and the upstream isn't held open.
            state.relay.stop_channel(state.current.id)
        state.casting = False
        state.current = None

    # 1×1 transparent PNG, returned in place of any logo we couldn't fetch
    # so the browser network panel doesn't fill up with red 4xx/5xx rows.
    _PLACEHOLDER_PNG = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
        b"QVR42mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
    )

    def _placeholder_logo() -> Response:
        resp = Response(_PLACEHOLDER_PNG, content_type="image/png")
        # Cache failures briefly so a transient upstream blip doesn't
        # pin them as broken for a whole day.
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    @app.get("/logo")
    def logo_route():
        url = request.args.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return _placeholder_logo()
        try:
            r = requests.get(url, timeout=5, verify=False, stream=True,
                             headers={"User-Agent": "m3u-stream/1.0"})
        except Exception:
            return _placeholder_logo()
        if r.status_code != 200:
            return _placeholder_logo()
        ct = r.headers.get("Content-Type", "image/png")
        resp = Response(r.iter_content(8192), content_type=ct)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.get("/")
    def index():
        channels_sorted = sorted(
            state.channels,
            key=lambda c: (c.source.lower(), c.group.lower(), c.name.lower()),
        )
        # Pre-build watch URLs server-side so the template can render them
        # directly without computing per-channel URLs in Jinja.
        channels = [_channel_dto(c) for c in channels_sorted]
        groups = sorted(m3u.groups(state.channels), key=str.lower)
        srcs = sorted(m3u.sources(state.channels), key=str.lower)
        return render_template(
            "index.html",
            channels=channels,
            groups=groups,
            sources=srcs,
            status=_status_dto(),
        )

    @app.get("/<name>.m3u")
    def playlist_route(name: str):
        sources_lower = {s.lower(): s for s in m3u.sources(state.channels)}
        if name.lower() == "all":
            wanted: set[str] | None = None
        elif name.lower() in sources_lower:
            wanted = {sources_lower[name.lower()]}
        else:
            parts = name.split("-")
            wanted = set()
            for p in parts:
                if p.lower() not in sources_lower:
                    return Response(f"unknown source: {p}\n",
                                    status=404, content_type="text/plain; charset=utf-8")
                wanted.add(sources_lower[p.lower()])
        chans = sorted(
            state.channels,
            key=lambda c: (c.source.lower(), c.group.lower(), c.name.lower()),
        )
        if wanted is not None:
            chans = [c for c in chans if c.source in wanted]
        base = _public_base()
        q = lambda v: v.replace('"', "'")
        lines = ["#EXTM3U"]
        for c in chans:
            attrs = [f'tvg-id="{c.id}"', f'tvg-name="{q(c.name)}"']
            if c.logo:
                attrs.append(f'tvg-logo="{q(c.logo)}"')
            if c.group:
                attrs.append(f'group-title="{q(c.group)}"')
            lines.append(f"#EXTINF:-1 {' '.join(attrs)},{c.name}")
            lines.append(f"{base}/{_enc(c.id)}/stream.ts")
        body = "\n".join(lines) + "\n"
        return Response(body, content_type="audio/x-mpegurl; charset=utf-8")

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
                # Switch cast target → tear down any previous one first.
                _stop_cast()
                # Some TVs reject SetAVTransportURI with 701 ("Transition not
                # available") when they're already playing something we
                # didn't start. An unconditional Stop forces them back to a
                # state where SetAVTransportURI is accepted.
                try:
                    dlna.stop(control_url)
                except Exception:
                    pass
                dlna.set_uri(control_url, _cast_stream_url(channel), channel.name)
                dlna.play(control_url)
                state.current = channel
                state.casting = True
            except Exception as e:
                _stop_cast()
                log.exception("cast failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, **_status_dto())

    @app.post("/stop")
    def stop_route():
        with state.lock:
            try:
                _stop_cast()
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
