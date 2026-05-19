"""Small HTTP status helper for local Gatherlink node discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gatherlink.runtime.services import ServiceRegistry


@dataclass(frozen=True)
class StatusHttpConfig:
    """Configuration for the local status HTTP helper."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 8765


def gather_status_payload(config: StatusHttpConfig, *, registry: ServiceRegistry | None = None) -> dict[str, Any]:
    """Return local Gatherlink process records, including hidden service records."""
    registry = registry or ServiceRegistry()
    services = registry.list()
    return {
        "listen": {"host": config.listen_host, "port": config.listen_port},
        "service_count": len(services),
        "services": [
            {
                "name": service.name,
                "kind": service.kind,
                "manager": service.manager,
                "state": service.status_label(),
                "pid": service.current_pid(),
                "systemd_unit": service.systemd_unit,
                "hidden": ".hidden" in service.name,
                "metadata": service.metadata,
            }
            for service in services
        ],
    }


def render_status_text(payload: dict[str, Any]) -> str:
    """Render a compact human-readable status page."""
    listen = payload["listen"]
    lines = [
        "Gatherlink local status",
        f"listening={listen['host']}:{listen['port']}",
        f"services={payload['service_count']}",
    ]
    for service in payload["services"]:
        hidden = " hidden=true" if service["hidden"] else ""
        lines.append(
            f"- {service['name']} kind={service['kind']} state={service['state']} "
            f"pid={service['pid']}{hidden}"
        )
    return "\n".join(lines) + "\n"


def run_status_http_server(config: StatusHttpConfig) -> None:
    """Run the status HTTP helper in the foreground."""
    server = build_status_http_server(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_status_http_server(config: StatusHttpConfig) -> ThreadingHTTPServer:
    """Build the status HTTP server without starting its serving loop."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = gather_status_payload(config)
            if self.path in {"/", "/text"}:
                _write_response(self, "text/plain; charset=utf-8", render_status_text(payload).encode("utf-8"))
                return
            if self.path == "/json":
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                _write_response(self, "application/json; charset=utf-8", body)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown status endpoint")

        def log_message(self, _format: str, *_args: object) -> None:
            # The helper is often used in test labs; avoid writing stdlib access
            # logs unless/ until the diagnostics bus owns this helper too.
            return

    return ThreadingHTTPServer((config.listen_host, config.listen_port), Handler)


def _write_response(handler: BaseHTTPRequestHandler, content_type: str, body: bytes) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
