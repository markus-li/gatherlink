from __future__ import annotations

import http.server
import importlib.util
import socketserver
import threading
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("http_probe", Path("tools/http_probe.py"))
assert _SPEC is not None and _SPEC.loader is not None
_HTTP_PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HTTP_PROBE)
fetch_http = _HTTP_PROBE.fetch_http


class _TextHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = b"probe-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """Silence test server logging."""


def test_fetch_http_reads_response_body_from_direct_tcp_endpoint() -> None:
    server = socketserver.TCPServer(("127.0.0.1", 0), _TextHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        response = fetch_http(target_host="127.0.0.1", target_port=port, timeout=2)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    _headers, _separator, body = response.partition(b"\r\n\r\n")
    assert body == b"probe-ok"
