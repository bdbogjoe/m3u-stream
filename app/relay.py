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


def _ffmpeg_hls_cmd(source_url: str, hls_dir: Path) -> list[str]:
    # Rolling HLS playlist for iOS / Safari, which can't play live fMP4.
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

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        self.send_response(200)
        self.send_header("Content-Type", self._content_type_for(path))
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/hls/"):
            self._serve_hls(path)
            return
        if path.endswith(".mp4"):
            self._serve_ffmpeg_pipe("mp4", "video/mp4")
            return
        # Default: MPEG-TS (legacy /stream and /stream.ts paths)
        self._serve_ffmpeg_pipe("mpegts", "video/mpeg")

    def _serve_hls(self, path: str):
        ensure_hls = self.server.ensure_hls  # type: ignore[attr-defined]
        hls_dir: Path = ensure_hls()
        rel = path[len("/hls/"):]
        # Disallow path traversal — only allow stream.m3u8 and segNNNNN.ts.
        if rel != "stream.m3u8" and not (rel.startswith("seg") and rel.endswith(".ts")):
            self.send_error(404)
            return
        target = hls_dir / rel
        # On a cold start the playlist takes a couple of segments to appear.
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

    def _serve_ffmpeg_pipe(self, kind: str, content_type: str):
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
    """An HTTP relay that streams a single source URL through ffmpeg on demand.

    Paths served on the relay port:
      /stream.ts             MPEG-TS (TV cast, mpv)
      /stream.mp4            fragmented MP4 (Chrome native playback)
      /hls/stream.m3u8       HLS playlist (iOS / Safari native playback)
      /hls/segNNNNN.ts       HLS segments
    """

    def __init__(self, port: int):
        self.port = port
        self._server: _ThreadedTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._source: str | None = None
        self._hls_proc: subprocess.Popen | None = None
        self._hls_dir: Path | None = None
        self._hls_stderr: threading.Thread | None = None
        self._hls_lock = threading.Lock()
        self._hls_last_request_at: float = 0.0
        self._hls_idle_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def source(self) -> str | None:
        return self._source

    def start(self, source_url: str) -> None:
        if self._server is not None:
            self.stop()
        hls_dir = Path(tempfile.mkdtemp(prefix="m3u-stream-hls-"))
        server = _ThreadedTCPServer(("", self.port), _Handler)
        server.source_url = source_url           # type: ignore[attr-defined]
        server.ensure_hls = self._ensure_hls     # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._source = source_url
        self._hls_dir = hls_dir
        self._wait_for_listen()
        # HLS ffmpeg is started lazily on first /hls/ request and stopped
        # again after HLS_IDLE_TIMEOUT seconds without traffic.
        self._hls_idle_thread = threading.Thread(target=self._hls_idle_watcher, daemon=True)
        self._hls_idle_thread.start()

    def stop(self) -> None:
        self._stop_hls()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._thread = None
        self._source = None
        self._hls_idle_thread = None
        if self._hls_dir is not None:
            shutil.rmtree(self._hls_dir, ignore_errors=True)
            self._hls_dir = None

    def _ensure_hls(self) -> Path:
        """Called by the HTTP handler on each /hls/ request. Starts ffmpeg(hls)
        if it isn't already running and bumps the last-activity timestamp."""
        with self._hls_lock:
            self._hls_last_request_at = time.time()
            if self._hls_dir is None or self._source is None:
                raise RuntimeError("relay not started")
            running = self._hls_proc is not None and self._hls_proc.poll() is None
            if not running:
                log.info("starting ffmpeg(hls) on demand for %s", self._source)
                self._start_hls(self._source, self._hls_dir)
            return self._hls_dir

    def _hls_idle_watcher(self) -> None:
        while self._server is not None:
            time.sleep(5)
            with self._hls_lock:
                if self._server is None:
                    return
                if self._hls_proc is None or self._hls_proc.poll() is not None:
                    continue
                if self._hls_last_request_at == 0:
                    continue
                if time.time() - self._hls_last_request_at > HLS_IDLE_TIMEOUT:
                    log.info("HLS idle for %.0fs, stopping ffmpeg(hls)", HLS_IDLE_TIMEOUT)
                    self._stop_hls()

    def _start_hls(self, source_url: str, hls_dir: Path) -> None:
        # Clear any stale segments from a previous run so the new playlist
        # only references files we're actually writing now.
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

        self._hls_stderr = threading.Thread(target=_drain, daemon=True)
        self._hls_stderr.start()

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
