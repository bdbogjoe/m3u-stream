import base64
import hashlib
import http.server
import ipaddress
import logging
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("m3u-stream.relay")

HLS_IDLE_TIMEOUT = 30.0   # stop ffmpeg(hls) after this many seconds without a request
HLS_INITIAL_WAIT = 10.0   # wait this long for the first playlist to appear

_HLS_RE = re.compile(r"^/([^/]+)/(stream\.m3u8|seg\d+\.ts)$")
_STREAM_RE = re.compile(r"^/([^/]+)/stream\.(mp4|ts)$")


def _is_ios_ua(ua: str) -> bool:
    return any(x in ua for x in ("iPhone", "iPad", "iPod", "CPU OS", "iPhone OS"))


def _safe_dirname(channel_id: str) -> str:
    return hashlib.sha1(channel_id.encode("utf-8")).hexdigest()[:16]


def _ffmpeg_cmd(source_url: str, output: str) -> list[str]:
    base = [
        "ffmpeg", "-loglevel", "warning", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", source_url,
    ]
    if output == "mp4":
        return base + [
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
    return base + ["-c", "copy", "-f", "mpegts", "pipe:1"]


def _ffmpeg_hls_cmd(source_url: str, hls_dir: Path) -> list[str]:
    return [
        "ffmpeg", "-loglevel", "warning", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", source_url,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments+omit_endlist",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(hls_dir / "seg%05d.ts"),
        str(hls_dir / "stream.m3u8"),
    ]


class _StreamCtx:
    def __init__(self, source_url: str, hls_dir: Path):
        self.source_url = source_url
        self.hls_dir = hls_dir
        self.hls_proc: subprocess.Popen | None = None
        self.hls_last_request_at: float = 0.0


class _Handler(http.server.BaseHTTPRequestHandler):
    timeout = 60

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Type, Content-Length")

    def _content_type_for(self, path: str) -> str:
        if path.endswith(".mp4"):
            return "video/mp4"
        if path.endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if path.endswith(".ts"):
            return "video/mp2t"
        return "video/mpeg"

    def _is_proxied(self) -> bool:
        return any(self.headers.get(h) for h in
                   ("X-Forwarded-Host", "X-Forwarded-Proto",
                    "X-Forwarded-For", "Forwarded"))

    def _auth_ok(self, relay: "Relay") -> bool:
        if not relay.auth_enabled or not self._is_proxied():
            return True
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            try:
                ip = ipaddress.ip_address(xff)
            except ValueError:
                ip = None
            if ip is not None and any(ip in net for net in relay.auth_trusted_nets):
                return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        u, _, p = decoded.partition(":")
        return (secrets.compare_digest(u, relay.auth_user)
                and secrets.compare_digest(p, relay.auth_pass))

    def _send_auth_required(self) -> None:
        body = b"auth required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="m3u-stream"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def _send_no_stream(self) -> None:
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        proxied = self._is_proxied()
        web_url = relay.web_public_url if proxied and relay.web_public_url else relay.web_url
        wants_html = "text/html" in self.headers.get("Accept", "")
        if wants_html:
            from html import escape
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>m3u-stream</title>"
                "<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;"
                "padding:2rem;line-height:1.5}a{color:#6ce}</style>"
                "<h1>m3u-stream relay</h1>"
                f"<p>Open <a href=\"{escape(web_url)}\">{escape(web_url)}</a> to pick a channel.</p>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            body = (
                "m3u-stream relay.\n"
                f"Open {web_url} to pick a channel.\n"
            ).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        self.send_response(503)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "5")
        self._send_cors_headers()
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        if not self._auth_ok(relay):
            self._send_auth_required()
            return
        path = self.path.split("?", 1)[0]
        if _HLS_RE.match(path) or _STREAM_RE.match(path):
            self.send_response(200)
            self.send_header("Content-Type", self._content_type_for(path))
            self._send_cors_headers()
            self.end_headers()
            return
        self._send_no_stream()

    def do_GET(self):
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        if not self._auth_ok(relay):
            self._send_auth_required()
            return
        path = self.path.split("?", 1)[0]

        m = _HLS_RE.match(path)
        if m:
            channel_id = urllib.parse.unquote(m.group(1))
            self._serve_hls(channel_id, m.group(2), relay)
            return

        m = _STREAM_RE.match(path)
        if m:
            channel_id = urllib.parse.unquote(m.group(1))
            fmt = m.group(2)
            # iOS WebKit can't play live fMP4 → redirect to per-channel HLS.
            if fmt == "mp4" and _is_ios_ua(self.headers.get("User-Agent", "")):
                enc = urllib.parse.quote(channel_id, safe="")
                self.send_response(302)
                self.send_header("Location", f"/{enc}/stream.m3u8")
                self.send_header("Cache-Control", "no-store")
                self._send_cors_headers()
                self.end_headers()
                return
            ctx = relay.get_or_create_stream(channel_id)
            if ctx is None:
                self.send_error(404, "unknown channel")
                return
            kind = "mp4" if fmt == "mp4" else "mpegts"
            ct = "video/mp4" if fmt == "mp4" else "video/mpeg"
            self._serve_ffmpeg_pipe(ctx.source_url, kind, ct)
            return

        self._send_no_stream()

    def _serve_hls(self, channel_id: str, rel: str, relay: "Relay"):
        ctx = relay.ensure_hls(channel_id)
        if ctx is None:
            self.send_error(404, "unknown channel")
            return
        wait = HLS_INITIAL_WAIT if rel == "stream.m3u8" else 5.0
        target = ctx.hls_dir / rel
        if not target.exists():
            deadline = time.time() + wait
            while time.time() < deadline and not target.exists():
                time.sleep(0.1)
        if not target.exists():
            self.send_error(404)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", self._content_type_for(rel))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if rel.endswith(".m3u8") else "public, max-age=10")
        self._send_cors_headers()
        self.end_headers()
        try: self.wfile.write(data)
        except Exception: pass

    def _serve_ffmpeg_pipe(self, source_url: str, kind: str, content_type: str):
        proc = subprocess.Popen(
            _ffmpeg_cmd(source_url, kind),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr_buf: list[bytes] = []

        def _drain_stderr():
            while True:
                line = proc.stderr.readline() if proc.stderr else b""
                if not line:
                    return
                stderr_buf.append(line)
                log.warning("ffmpeg: %s", line.rstrip().decode(errors="replace"))

        threading.Thread(target=_drain_stderr, daemon=True).start()

        target = 188 * 100 if kind != "mp4" else 32 * 1024
        buf = b""
        while len(buf) < target:
            chunk = proc.stdout.read(8192) if proc.stdout else b""
            if not chunk:
                break
            buf += chunk

        if not buf:
            err = b"".join(stderr_buf[-20:]).decode(errors="replace") or "(no ffmpeg output)"
            log.error("relay produced no bytes for %s (kind=%s)\n%s", source_url, kind, err)
            body = f"ffmpeg failed:\n{err}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            try: self.wfile.write(body)
            except Exception: pass
            proc.kill()
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(buf)
            self.wfile.flush()
            while True:
                chunk = proc.stdout.read(65536) if proc.stdout else b""
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            proc.kill()

    def log_message(self, *args):
        pass


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Relay:
    """Long-running multi-channel HTTP relay.

    Paths served on the relay port:
      /<id>/stream.ts         MPEG-TS for the channel with the given id
      /<id>/stream.mp4        Fragmented MP4 (iOS clients are 302'd to HLS)
      /<id>/stream.m3u8       HLS playlist
      /<id>/segNNNNN.ts       HLS segments

    The TCP server is always listening; a request for an unknown path
    returns a 503 friendly page pointing at the web UI.
    """

    def __init__(self, port: int, web_url: str = "", web_public_url: str = "",
                 auth_user: str = "", auth_pass: str = "",
                 auth_trusted_nets=None,
                 resolve_url: Optional[Callable[[str], Optional[str]]] = None):
        self.port = port
        self.web_url = web_url or "the m3u-stream web UI"
        self.web_public_url = web_public_url
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.auth_enabled = bool(auth_user and auth_pass)
        self.auth_trusted_nets = list(auth_trusted_nets or [])
        self.resolve_url = resolve_url or (lambda _id: None)

        self._base_hls_dir = Path(tempfile.mkdtemp(prefix="m3u-stream-hls-"))
        self._streams: dict[str, _StreamCtx] = {}
        self._lock = threading.Lock()

        server = _ThreadedTCPServer(("", port), _Handler)
        server.relay = self  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._wait_for_listen()
        threading.Thread(target=self._idle_watcher, daemon=True).start()

    def get_or_create_stream(self, channel_id: str) -> _StreamCtx | None:
        with self._lock:
            ctx = self._streams.get(channel_id)
            if ctx is not None:
                return ctx
            url = self.resolve_url(channel_id)
            if not url:
                return None
            hls_dir = self._base_hls_dir / _safe_dirname(channel_id)
            hls_dir.mkdir(parents=True, exist_ok=True)
            ctx = _StreamCtx(url, hls_dir)
            self._streams[channel_id] = ctx
            log.info("relay added channel %s", channel_id)
            return ctx

    def ensure_hls(self, channel_id: str) -> _StreamCtx | None:
        with self._lock:
            ctx = self._streams.get(channel_id)
            if ctx is None:
                url = self.resolve_url(channel_id)
                if not url:
                    return None
                hls_dir = self._base_hls_dir / _safe_dirname(channel_id)
                hls_dir.mkdir(parents=True, exist_ok=True)
                ctx = _StreamCtx(url, hls_dir)
                self._streams[channel_id] = ctx
            ctx.hls_last_request_at = time.time()
            running = ctx.hls_proc is not None and ctx.hls_proc.poll() is None
            if not running:
                log.info("starting ffmpeg(hls) for channel %s", channel_id)
                self._start_hls(ctx)
            return ctx

    def shutdown(self) -> None:
        with self._lock:
            for ctx in self._streams.values():
                self._stop_hls(ctx)
            self._streams.clear()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        shutil.rmtree(self._base_hls_dir, ignore_errors=True)

    # Cast / status helpers used by the rest of the app
    def stop_channel(self, channel_id: str) -> None:
        """Tear down a single channel's HLS and remove its state."""
        with self._lock:
            ctx = self._streams.pop(channel_id, None)
        if ctx is None:
            return
        self._stop_hls(ctx)
        shutil.rmtree(ctx.hls_dir, ignore_errors=True)
        log.info("relay removed channel %s", channel_id)

    def _idle_watcher(self) -> None:
        while True:
            time.sleep(5)
            now = time.time()
            to_clean: list[str] = []
            with self._lock:
                for cid, ctx in list(self._streams.items()):
                    if ctx.hls_proc is None or ctx.hls_proc.poll() is not None:
                        # HLS not running for this channel — leave the StreamCtx
                        # in place, it's just URL bookkeeping.
                        continue
                    if ctx.hls_last_request_at == 0:
                        continue
                    if now - ctx.hls_last_request_at > HLS_IDLE_TIMEOUT:
                        log.info("HLS idle for channel %s, stopping ffmpeg", cid)
                        self._stop_hls(ctx)
                        to_clean.append(cid)
            for cid in to_clean:
                # Drop the cached state so next request rebuilds with a fresh
                # source URL lookup (the upstream may have moved).
                self.stop_channel(cid)

    def _start_hls(self, ctx: _StreamCtx) -> None:
        for f in ctx.hls_dir.iterdir():
            try: f.unlink()
            except OSError: pass
        cmd = _ffmpeg_hls_cmd(ctx.source_url, ctx.hls_dir)
        ctx.hls_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        def _drain():
            assert ctx.hls_proc and ctx.hls_proc.stderr
            for line in ctx.hls_proc.stderr:
                log.warning("ffmpeg(hls): %s", line.rstrip().decode(errors="replace"))

        threading.Thread(target=_drain, daemon=True).start()

    def _stop_hls(self, ctx: _StreamCtx) -> None:
        if ctx.hls_proc is not None:
            try:
                ctx.hls_proc.terminate()
                try: ctx.hls_proc.wait(timeout=2)
                except subprocess.TimeoutExpired: ctx.hls_proc.kill()
            except Exception:
                pass
        ctx.hls_proc = None

    def _wait_for_listen(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                try:
                    s.connect(("127.0.0.1", self.port))
                    return
                except OSError:
                    time.sleep(0.05)
        raise RuntimeError(f"relay did not start listening on port {self.port}")
