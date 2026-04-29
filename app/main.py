import logging
import os
import subprocess
import sys

from flask import Flask, jsonify, render_template, request

from . import dlna, m3u
from .state import AppState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("m3u-stream")


def _detect_host_ip(tv_ip: str) -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", tv_ip], text=True)
        for parts in (line.split() for line in out.splitlines()):
            if "src" in parts:
                return parts[parts.index("src") + 1]
    except Exception as e:
        log.warning("ip route detection failed: %s", e)
    return "127.0.0.1"


def create_app() -> Flask:
    m3u_url = os.environ.get("M3U_URL")
    tv_ip = os.environ.get("TV_IP")
    if not m3u_url or not tv_ip:
        log.error("M3U_URL and TV_IP must be set")
        sys.exit(2)

    relay_port = int(os.environ.get("RELAY_PORT", "18888"))
    host_ip = os.environ.get("HOST_IP") or _detect_host_ip(tv_ip)
    log.info("host_ip=%s tv_ip=%s relay_port=%d", host_ip, tv_ip, relay_port)

    state = AppState(relay_port)
    try:
        state.channels = m3u.fetch(m3u_url)
        log.info("loaded %d channels", len(state.channels))
    except Exception as e:
        log.error("failed to fetch M3U at startup: %s", e)
        sys.exit(2)

    app = Flask(__name__)
    app.config["state"] = state
    app.config["m3u_url"] = m3u_url
    app.config["tv_ip"] = tv_ip
    app.config["host_ip"] = host_ip

    def _ensure_control_url() -> str:
        if state.control_url is None:
            state.control_url = dlna.discover_control_url(tv_ip)
            log.info("AVTransport control URL: %s", state.control_url)
        return state.control_url

    def _channel_dto(c):
        return {"id": c.id, "name": c.name, "group": c.group, "logo": c.logo}

    @app.get("/")
    def index():
        groups = m3u.groups(state.channels)
        current_id = state.current.id if state.current else None
        return render_template(
            "index.html",
            channels=state.channels,
            groups=groups,
            current_id=current_id,
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, channels=len(state.channels))

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
                if state.relay.running:
                    try:
                        dlna.stop(control_url)
                    except Exception as e:
                        log.warning("stop before re-cast failed: %s", e)
                    state.relay.stop()
                state.relay.start(channel.url)
                stream_url = f"http://{host_ip}:{relay_port}/stream"
                dlna.set_uri(control_url, stream_url, channel.name)
                dlna.play(control_url)
                state.current = channel
            except Exception as e:
                state.relay.stop()
                state.current = None
                log.exception("cast failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True, current=_channel_dto(channel))

    @app.post("/stop")
    def stop_route():
        with state.lock:
            try:
                if state.control_url:
                    try:
                        dlna.stop(state.control_url)
                    except Exception as e:
                        log.warning("dlna stop failed: %s", e)
                state.relay.stop()
                state.current = None
            except Exception as e:
                log.exception("stop failed")
                return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=True)

    @app.post("/reload")
    def reload_route():
        try:
            channels = m3u.fetch(m3u_url)
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
