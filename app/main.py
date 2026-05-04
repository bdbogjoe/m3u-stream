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
import threading
import time
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

    epg_urls = [u.strip() for u in (os.environ.get("EPG_URL") or "").split(",") if u.strip()]
    epg: EPG | None = EPG(epg_urls) if epg_urls else None
    if epg:
        log.info("EPG_URL       = %s", ", ".join(epg_urls))

    state.prober.start(state.channels)
    probe_interval = int(os.environ.get("PROBE_INTERVAL", "600"))
    if probe_interval > 0:
        log.info("probe interval = %ds", probe_interval)
        def _periodic_probe():
            while True:
                time.sleep(probe_interval)
                state.prober.start(state.channels)
        threading.Thread(target=_periodic_probe, daemon=True).start()

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

    def _public_web_base() -> str:
        if web_public_url and _is_proxied():
            return web_public_url
        return web_lan_url

    def _enc(cid: str) -> str:
        from urllib.parse import quote
        return quote(cid, safe="")

    def _cast_stream_url(channel) -> str:
        # LAN-direct: the TV won't resolve an external/proxied hostname.
        return f"{lan_base_url}/{_enc(channel.id)}/stream.ts"

    def _channel_dto(c, probe: dict[str, bool] | None = None):
        return {
            "id": c.id, "name": c.name, "group": c.group,
            "logo": c.logo, "source": c.source, "url": c.url,
            "current_programme": (
                epg.format_current(tvg_id=c.tvg_id, channel_name=c.name) if epg else None
            ),
            # True / False / None (not yet probed)
            "online": (probe.get(c.id) if probe is not None else None),
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
        probe = state.prober.status()
        channels = [_channel_dto(c, probe) for c in channels_sorted]
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
        # Keep online + not-yet-probed (None); drop only the ones we've
        # definitively confirmed offline so external players don't waste
        # time on dead URLs.
        probe = state.prober.status()
        chans = [c for c in chans if probe.get(c.id) is not False]
        base = _public_base()
        q = lambda v: v.replace('"', "'")
        header = "#EXTM3U"
        if epg:
            header += f' x-tvg-url="{_public_web_base()}/epg.xml"'
        lines = [header]
        for c in chans:
            attrs = []
            # Prefer the EPG-matched id so the tvg-id in our output keys
            # the same channel as our /epg.xml. Fall back to the source
            # tvg-id only if the EPG can't match by name.
            epg_id = epg.id_for(c.name) if epg else None
            tvg_id = epg_id or c.tvg_id
            if tvg_id:
                attrs.append(f'tvg-id="{q(tvg_id)}"')
            attrs.append(f'tvg-name="{q(c.name)}"')
            if c.logo:
                attrs.append(f'tvg-logo="{q(c.logo)}"')
            if c.group:
                attrs.append(f'group-title="{q(c.group)}"')
            lines.append(f"#EXTINF:-1 {' '.join(attrs)},{c.name}")
            lines.append(f"{base}/{_enc(c.id)}/stream.ts")
        body = "\n".join(lines) + "\n"
        return Response(body, content_type="audio/x-mpegurl; charset=utf-8")

    @app.get("/<cid>/watch")
    def watch_route(cid: str):
        channel = state.channel_by_id(cid)
        if channel is None:
            return Response("unknown channel\n", status=404,
                            content_type="text/plain; charset=utf-8")
        base = _public_base()
        cid_enc = _enc(channel.id)
        current = (epg.format_current(tvg_id=channel.tvg_id, channel_name=channel.name)
                   if epg else None)
        schedule = (epg.format_upcoming(tvg_id=channel.tvg_id, channel_name=channel.name, n=6)
                    if epg else [])
        return render_template(
            "watch.html",
            channel=channel,
            current_programme=current,
            schedule=schedule,
            mp4_url=f"{base}/{cid_enc}/stream.mp4",
            hls_url=f"{base}/{cid_enc}/stream.m3u8",
        )

    @app.get("/epg")
    def epg_grid_route():
        if not epg:
            return Response("EPG not configured (set EPG_URL)\n", status=404,
                            content_type="text/plain; charset=utf-8")
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        import math as _math
        SLOT_MIN = 30
        SLOT_COUNT = 16  # 8 hours
        now = _dt.now(_tz.utc)
        local_now = now.astimezone()
        window_start_local = local_now.replace(
            minute=(local_now.minute // SLOT_MIN) * SLOT_MIN,
            second=0, microsecond=0,
        )
        window_start = window_start_local.astimezone(_tz.utc)
        window_end = window_start + _td(minutes=SLOT_MIN * SLOT_COUNT)
        slot_labels = [
            (window_start_local + _td(minutes=SLOT_MIN * i)).strftime("%H:%M")
            for i in range(SLOT_COUNT)
        ]
        rows = []
        for ch in state.channels:
            progs = epg.schedule(tvg_id=ch.tvg_id, channel_name=ch.name)
            programmes = []
            for p in progs:
                if p.stop <= window_start or p.start >= window_end:
                    continue
                start_min = (p.start - window_start).total_seconds() / 60
                stop_min = (p.stop - window_start).total_seconds() / 60
                # Grid columns: col 1 is channel, cols 2..(SLOT_COUNT+1) are slots,
                # col SLOT_COUNT+2 is the trailing line (exclusive end).
                col_start = max(2, _math.floor(start_min / SLOT_MIN) + 2)
                col_end = min(SLOT_COUNT + 2, _math.ceil(stop_min / SLOT_MIN) + 2)
                if col_end <= col_start:
                    col_end = col_start + 1
                programmes.append({
                    "title": p.title,
                    "time": p.start.astimezone().strftime("%H:%M"),
                    "col_start": col_start,
                    "col_end": col_end,
                    "is_current": p.start <= now < p.stop,
                })
            if programmes:
                rows.append({"channel": ch, "programmes": programmes})
        # "Now" position expressed in slot-widths past the start of the timeline.
        now_slots = (now - window_start).total_seconds() / 60 / SLOT_MIN
        groups = sorted(m3u.groups(state.channels), key=str.lower)
        srcs = sorted(m3u.sources(state.channels), key=str.lower)
        return render_template(
            "epg_grid.html",
            rows=rows,
            slot_labels=slot_labels,
            slot_count=SLOT_COUNT,
            now_slots=now_slots,
            groups=groups,
            sources=srcs,
        )

    @app.get("/<cid>/epg")
    def channel_epg_route(cid: str):
        channel = state.channel_by_id(cid)
        if channel is None:
            return Response("unknown channel\n", status=404,
                            content_type="text/plain; charset=utf-8")
        progs = (epg.schedule(tvg_id=channel.tvg_id, channel_name=channel.name)
                 if epg else [])
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        by_day: list[tuple[str, list[dict]]] = []
        last_key = None
        for p in progs:
            local_start = p.start.astimezone()
            day_key = local_start.strftime("%Y-%m-%d")
            day_label = local_start.strftime("%A %d %B")
            if day_key != last_key:
                by_day.append((day_label, []))
                last_key = day_key
            by_day[-1][1].append({
                "time": local_start.strftime("%H:%M"),
                "title": p.title,
                "is_current": p.start <= now < p.stop,
            })
        return render_template(
            "epg_channel.html",
            channel=channel,
            by_day=by_day,
            has_epg=epg is not None,
        )

    @app.get("/epg.xml")
    def epg_route():
        if not epg:
            return Response("EPG not configured (set EPG_URL)\n",
                            status=404, content_type="text/plain; charset=utf-8")
        data = epg.raw_xml()
        if not data:
            return Response("EPG not loaded yet, try again in a few seconds\n",
                            status=503, content_type="text/plain; charset=utf-8",
                            headers={"Retry-After": "5"})
        resp = Response(data, content_type="application/xml; charset=utf-8")
        resp.headers["Cache-Control"] = "public, max-age=900"
        return resp

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
        state.prober.start(channels)
        return jsonify(ok=True, count=len(channels))

    @app.post("/probe")
    def probe_route():
        if state.prober.start(state.channels):
            return jsonify(ok=True, started=True, channels=len(state.channels))
        return jsonify(ok=True, started=False, reason="probe already running")

    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("WEB_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
