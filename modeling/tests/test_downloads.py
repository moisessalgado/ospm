import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from planting_slots.downloads import DownloadNotFound, download_file, file_md5

PAYLOAD = bytes(range(256)) * 4000  # ~1 MB


def make_server(fail_first=0, truncate_first=False):
    state = {"requests": 0, "ranges": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"] += 1
            if self.path == "/missing":
                self.send_response(404)
                self.end_headers()
                return
            if state["requests"] <= fail_first:
                self.send_response(503)
                self.end_headers()
                return
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            state["ranges"].append(start)
            body = PAYLOAD[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if truncate_first and state["requests"] == 1:
                self.wfile.write(body[: len(body) // 2])  # connection closes short
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def test_download_is_atomic_and_cached(tmp_path):
    server, state = make_server()
    dest = tmp_path / "sub" / "f.bin"
    url = f"http://127.0.0.1:{server.server_port}/f"
    assert download_file(url, dest) == dest
    assert dest.read_bytes() == PAYLOAD and not list(tmp_path.rglob("*.part"))
    download_file(url, dest)  # cached: no new request
    assert state["requests"] == 1
    server.shutdown()


def test_download_retries_server_errors(tmp_path):
    server, state = make_server(fail_first=2)
    dest = tmp_path / "f.bin"
    download_file(f"http://127.0.0.1:{server.server_port}/f", dest, backoff_s=0.01)
    assert dest.read_bytes() == PAYLOAD and state["requests"] == 3
    server.shutdown()


def test_download_resumes_with_range(tmp_path):
    server, state = make_server(truncate_first=True)
    dest = tmp_path / "f.bin"
    download_file(f"http://127.0.0.1:{server.server_port}/f", dest, backoff_s=0.01)
    assert dest.read_bytes() == PAYLOAD
    assert state["ranges"][0] == 0 and state["ranges"][1] > 0  # second attempt asked for the rest
    server.shutdown()


def test_download_404_is_not_retried(tmp_path):
    server, state = make_server()
    with pytest.raises(DownloadNotFound):
        download_file(f"http://127.0.0.1:{server.server_port}/missing", tmp_path / "x", backoff_s=0.01)
    assert state["requests"] == 1 and not (tmp_path / "x").exists()
    server.shutdown()


def test_file_md5(tmp_path):
    path = tmp_path / "a"
    path.write_bytes(b"abc")
    assert file_md5(path) == "900150983cd24fb0d6963f7d28e17f72"
