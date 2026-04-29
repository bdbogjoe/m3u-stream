import http.server
import socket
import socketserver
import subprocess
import threading
import time


class _Handler(http.server.BaseHTTPRequestHandler):
    timeout = 60

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.end_headers()

    def do_GET(self):
        url = self.server.source_url  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "quiet",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", url, "-c", "copy", "-f", "mpegts", "pipe:1",
            ],
            stdout=subprocess.PIPE,
        )
        # Pre-buffer ~18.8KB so the TV doesn't time out before bytes arrive.
        buf = b""
        while len(buf) < 188 * 100:
            chunk = proc.stdout.read(188 * 10) if proc.stdout else b""
            if not chunk:
                break
            buf += chunk
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
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
    """An HTTP relay that streams a single source URL through ffmpeg on demand."""

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
