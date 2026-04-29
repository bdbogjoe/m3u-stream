import http.server
import logging
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("m3u-stream.relay")

HLS_IDLE_TIMEOUT = 30.0   # stop ffmpeg(hls) after this many seconds without a request
HLS_INITIAL_WAIT = 10.0   # wait this long for the first playlist to appear


def _ffmpeg_cmd(source_url: str, output: str) -> list[str]:
    base = [
        "ffmpeg", "-loglevel", "warning", "-nostdin",
        "-user_agent", "Mozilla/5.0 (compatible; m3u-stream/1.0)",
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
        "-user_agent", "Mozilla/5.0 (compatible; m3u-stream/1.0)",
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

    def _send_no_stream(self) -> None:
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        web_url = relay.web_url
        wants_html = "text/html" in self.headers.get("Accept", "")
        if wants_html:
            from html import escape
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>m3u-stream — no stream</title>"
                "<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;"
                "padding:2rem;line-height:1.5}a{color:#6ce}</style>"
                "<h1>No stream is currently running</h1>"
                f"<p>Open <a href=\"{escape(web_url)}\">{escape(web_url)}</a>, "
                "pick a channel, and start a stream (Cast to TV / Start stream / "
                "Watch in browser) first.</p>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            body = (
                "No stream is currently running.\n\n"
                f"Open {web_url} , pick a channel, and start a stream "
                "(Cast to TV / Start stream / Watch in browser) first.\n"
            ).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        self.send_response(503)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "5")
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        if relay.source is None:
            self._send_no_stream()
            return
        self.send_response(200)
        self.send_header("Content-Type", self._content_type_for(path))
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        if relay.source is None:
            self._send_no_stream()
            return
        if path.startswith("/hls/"):
            self._serve_hls(path, relay)
            return
        if path.endswith(".mp4"):
            self._serve_ffmpeg_pipe(relay.source, "mp4", "video/mp4")
            return
        self._serve_ffmpeg_pipe(relay.source, "mpegts", "video/mpeg")

    def _serve_hls(self, path: str, relay: "Relay"):
        hls_dir = relay.ensure_hls()
        rel = path[len("/hls/"):]
        if rel != "stream.m3u8" and not (rel.startswith("seg") and rel.endswith(".ts")):
            self.send_error(404)
            return
        target = hls_dir / rel
        wait = HLS_INITIAL_WAIT if rel == "stream.m3u8" else 5.0
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
        try:
            self.wfile.write(data)
        except Exception:
            pass

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
            try:
                self.wfile.write(body)
            except Exception:
                pass
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
    """Long-running HTTP relay. The TCP server is always listening so that
    a client hitting /stream.ts /stream.mp4 /hls/* before any stream has
    been started gets a friendly 503 instead of a connection error.

    Paths served on the relay port:
      /stream.ts             MPEG-TS (TV cast, mpv)
      /stream.mp4            fragmented MP4 (Chrome native playback)
      /hls/stream.m3u8       HLS playlist (iOS / Safari native playback)
      /hls/segNNNNN.ts       HLS segments
    """

    def __init__(self, port: int, web_url: str = ""):
        self.port = port
        self.web_url = web_url or "the m3u-stream web UI"
        self._source: str | None = None
        self._hls_dir = Path(tempfile.mkdtemp(prefix="m3u-stream-hls-"))
        self._hls_proc: subprocess.Popen | None = None
        self._hls_lock = threading.Lock()
        self._hls_last_request_at: float = 0.0

        server = _ThreadedTCPServer(("", port), _Handler)
        server.relay = self  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._wait_for_listen()

        # Idle watcher to stop ffmpeg(hls) when no /hls/ traffic for a while.
        threading.Thread(target=self._hls_idle_watcher, daemon=True).start()

    @property
    def running(self) -> bool:
        return self._source is not None

    @property
    def source(self) -> str | None:
        return self._source

    def start(self, source_url: str) -> None:
        """Set / switch the upstream source. The HTTP server stays up
        regardless; only the source URL changes."""
        if self._source == source_url:
            return
        # If the previous source was different, kill its HLS ffmpeg so the
        # next /hls/ request spawns a fresh one for the new source.
        self._stop_hls()
        self._source = source_url
        log.info("relay source = %s", source_url)

    def stop(self) -> None:
        """Clear the source. Subsequent /stream.* requests will get 503."""
        if self._source is None and self._hls_proc is None:
            return
        log.info("relay source cleared")
        self._source = None
        self._stop_hls()

    def shutdown(self) -> None:
        """Final teardown — used when the process is exiting."""
        self.stop()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        shutil.rmtree(self._hls_dir, ignore_errors=True)

    def ensure_hls(self) -> Path:
        """Called by the HTTP handler on each /hls/ request. Starts ffmpeg(hls)
        if it isn't already running for the current source, and bumps the
        last-activity timestamp."""
        with self._hls_lock:
            self._hls_last_request_at = time.time()
            if self._source is None:
                raise RuntimeError("no source set")
            running = self._hls_proc is not None and self._hls_proc.poll() is None
            if not running:
                log.info("starting ffmpeg(hls) on demand for %s", self._source)
                self._start_hls(self._source, self._hls_dir)
            return self._hls_dir

    def _hls_idle_watcher(self) -> None:
        while True:
            time.sleep(5)
            with self._hls_lock:
                if self._hls_proc is None or self._hls_proc.poll() is not None:
                    continue
                if self._hls_last_request_at == 0:
                    continue
                if time.time() - self._hls_last_request_at > HLS_IDLE_TIMEOUT:
                    log.info("HLS idle for %.0fs, stopping ffmpeg(hls)", HLS_IDLE_TIMEOUT)
                    self._stop_hls()

    def _start_hls(self, source_url: str, hls_dir: Path) -> None:
        for f in hls_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        cmd = _ffmpeg_hls_cmd(source_url, hls_dir)
        self._hls_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        def _drain():
            assert self._hls_proc and self._hls_proc.stderr
            for line in self._hls_proc.stderr:
                log.warning("ffmpeg(hls): %s", line.rstrip().decode(errors="replace"))

        threading.Thread(target=_drain, daemon=True).start()

    def _stop_hls(self) -> None:
        if self._hls_proc is not None:
            try:
                self._hls_proc.terminate()
                try:
                    self._hls_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._hls_proc.kill()
            except Exception:
                pass
        self._hls_proc = None

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
