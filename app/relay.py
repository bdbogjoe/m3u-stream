import http.server
import logging
import socket
import socketserver
import subprocess
import threading
import time

log = logging.getLogger("m3u-stream.relay")


def _ffmpeg_cmd(source_url: str, output: str) -> list[str]:
    base = [
        "ffmpeg", "-loglevel", "warning", "-nostdin",
        "-user_agent", "Mozilla/5.0 (compatible; m3u-stream/1.0)",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", source_url,
    ]
    if output == "mp4":
        # Fragmented MP4 for direct browser playback. Video copied (assumes H.264);
        # audio re-encoded to AAC so MP2/AC3 sources still work in Chrome.
        return base + [
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
    # Default: MPEG-TS (TV cast / mpv)
    return base + ["-c", "copy", "-f", "mpegts", "pipe:1"]


class _Handler(http.server.BaseHTTPRequestHandler):
    timeout = 60

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Type, Content-Length")

    def _output_kind(self) -> str:
        path = self.path.split("?", 1)[0]
        if path.endswith(".mp4"):
            return "mp4"
        return "mpegts"

    def _content_type(self, kind: str) -> str:
        return "video/mp4" if kind == "mp4" else "video/mpeg"

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        kind = self._output_kind()
        self.send_response(200)
        self.send_header("Content-Type", self._content_type(kind))
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        kind = self._output_kind()
        url = self.server.source_url  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            _ffmpeg_cmd(url, kind),
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

        # Pre-buffer a small amount so the client doesn't time out before bytes arrive.
        # MPEG-TS packets are 188 bytes; for fMP4 we just want any moov fragment to land.
        target = 188 * 100 if kind != "mp4" else 32 * 1024
        buf = b""
        while len(buf) < target:
            chunk = proc.stdout.read(8192) if proc.stdout else b""
            if not chunk:
                break
            buf += chunk

        if not buf:
            err = b"".join(stderr_buf[-20:]).decode(errors="replace") or "(no ffmpeg output)"
            log.error("relay produced no bytes for %s (kind=%s)\n%s", url, kind, err)
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
        self.send_header("Content-Type", self._content_type(kind))
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
    """An HTTP relay that streams a single source URL through ffmpeg on demand.

    Two paths are served on the same port:
      /stream.ts  → MPEG-TS (TV cast, mpv)
      /stream.mp4 → fragmented MP4 (Chrome native playback)
    """

    def __init__(self, port: int):
        self.port = port
        self._server: _ThreadedTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._source: str | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def source(self) -> str | None:
        return self._source

    def start(self, source_url: str) -> None:
        if self._server is not None:
            self.stop()
        server = _ThreadedTCPServer(("", self.port), _Handler)
        server.source_url = source_url  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._source = source_url
        self._wait_for_listen()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._thread = None
        self._source = None

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
