"""Experimental local HTTP status/API helper for Gatherlink node discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote, urlparse

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.persistence import redact_secrets
from gatherlink.runtime.services import ServiceRegistry

EXPERIMENTAL_REST_LABEL = "EXPERIMENTAL"
DEFAULT_WRITE_WINDOW_SECONDS = 3600
LOOPBACK_HOSTNAMES = frozenset({"localhost"})


@dataclass(frozen=True)
class StatusHttpConfig:
    """
    Configuration for the experimental local status HTTP helper.

    TODO(status-http-helper): Keep this helper intentionally conservative. It is useful for local
    automation and future UI work, but it must not become a long-lived remote
    management surface.
    """

    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    allow_non_loopback: bool = False
    write_window_seconds: int = DEFAULT_WRITE_WINDOW_SECONDS
    started_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate bind policy and normalize helper timing."""
        if self.listen_port < 0 or self.listen_port > 65535:
            raise ValueError("status HTTP listen port must be between 0 and 65535")
        if self.write_window_seconds < 0:
            raise ValueError("status HTTP write window must be zero or greater")
        if not self.allow_non_loopback and not is_loopback_host(self.listen_host):
            raise ValueError(
                "status HTTP helper binds to loopback only by default; pass the explicit danger flag "
                "to bind a non-loopback address"
            )
        if self.started_at is not None:
            return
        object.__setattr__(self, "started_at", datetime.now(UTC))

    @property
    def write_expires_at(self) -> datetime:
        """Return the UTC time when writable experimental APIs stop working."""
        assert self.started_at is not None
        return self.started_at + timedelta(seconds=self.write_window_seconds)

    @property
    def writes_enabled(self) -> bool:
        """Return whether writable APIs are still within their explicit CLI window."""
        return datetime.now(UTC) < self.write_expires_at


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is loopback-only."""
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def gather_status_payload(config: StatusHttpConfig, *, registry: ServiceRegistry | None = None) -> dict[str, Any]:
    """Return local Gatherlink process records, including hidden service records."""
    registry = registry or ServiceRegistry()
    services = registry.list()
    assert config.started_at is not None
    return {
        "api": {
            "label": EXPERIMENTAL_REST_LABEL,
            "stability": "experimental",
            "read_only_after": config.write_expires_at.isoformat(),
            "write_window_seconds": config.write_window_seconds,
            "writes_enabled": config.writes_enabled,
            "writes_implemented": True,
            "write_endpoints": ["POST /v1/services/{name}/close"],
        },
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
                "metadata": redact_secrets(service.metadata),
            }
            for service in services
        ],
    }


def render_status_text(payload: dict[str, Any]) -> str:
    """Render a compact human-readable status page."""
    listen = payload["listen"]
    api = payload["api"]
    lines = [
        f"Gatherlink local status ({api['label']})",
        f"listening={listen['host']}:{listen['port']}",
        f"services={payload['service_count']}",
        f"writes_enabled={api['writes_enabled']} writes_implemented={api['writes_implemented']}",
        f"write_window_expires={api['read_only_after']}",
    ]
    for service in payload["services"]:
        hidden = " hidden=true" if service["hidden"] else ""
        lines.append(
            f"- {service['name']} kind={service['kind']} state={service['state']} pid={service['pid']}{hidden}"
        )
    return "\n".join(lines) + "\n"


def run_status_http_server(config: StatusHttpConfig, *, diagnostics_bus: DiagnosticsBus | None = None) -> None:
    """Run the status HTTP helper in the foreground."""
    publish_status_http_start(config, diagnostics_bus=diagnostics_bus)
    server = build_status_http_server(config, diagnostics_bus=diagnostics_bus)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def publish_status_http_start(config: StatusHttpConfig, *, diagnostics_bus: DiagnosticsBus | None = None) -> None:
    """Publish structured startup facts for the experimental status helper."""
    if diagnostics_bus is None:
        return
    details = {
        "listen_host": config.listen_host,
        "listen_port": config.listen_port,
        "write_window_seconds": config.write_window_seconds,
        "write_expires_at": config.write_expires_at.isoformat(),
        "writes_enabled": config.writes_enabled,
        "allow_non_loopback": config.allow_non_loopback,
    }
    diagnostics_bus.publish(
        DiagnosticEvent.helper_event(
            code="helper.status_http.started",
            helper="status-http",
            message="experimental status HTTP helper started",
            details=details,
        )
    )
    if config.allow_non_loopback:
        diagnostics_bus.publish(
            DiagnosticEvent.helper_event(
                code="helper.status_http.non_loopback_bind",
                helper="status-http",
                severity="warning",
                message="experimental status HTTP helper bound outside loopback",
                details=details,
            )
        )


def build_status_http_server(
    config: StatusHttpConfig,
    *,
    registry: ServiceRegistry | None = None,
    diagnostics_bus: DiagnosticsBus | None = None,
) -> ThreadingHTTPServer:
    """Build the status HTTP server without starting its serving loop."""
    service_registry = registry or ServiceRegistry()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = gather_status_payload(config, registry=service_registry)
            if self.path in {"/", "/text"}:
                _write_response(self, "text/plain; charset=utf-8", render_status_text(payload).encode("utf-8"))
                return
            if self.path in {"/json", "/v1/status"}:
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                _write_response(self, "application/json; charset=utf-8", body)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown status endpoint")

        def do_POST(self) -> None:
            if not config.writes_enabled:
                _publish_status_http_write_event(
                    diagnostics_bus,
                    code="helper.status_http.write_denied",
                    severity="warning",
                    message="experimental status HTTP write window expired",
                    path=self.path,
                )
                self.send_error(HTTPStatus.FORBIDDEN, "experimental write window expired")
                return
            parsed = urlparse(self.path)
            prefix = "/v1/services/"
            suffix = "/close"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                service_name = unquote(parsed.path[len(prefix) : -len(suffix)])
                self._close_service(service_name)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown experimental write endpoint")

        def _close_service(self, service_name: str) -> None:
            try:
                service = service_registry.close(service_name)
            except ValueError as exc:
                _publish_status_http_write_event(
                    diagnostics_bus,
                    code="helper.status_http.write_failed",
                    severity="warning",
                    message="experimental status HTTP service close failed",
                    path=self.path,
                    service=service_name,
                    error=str(exc),
                )
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            body = json.dumps(
                {
                    "ok": True,
                    "action": "close",
                    "service": service.name,
                    "state": "stopped",
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            _publish_status_http_write_event(
                diagnostics_bus,
                code="helper.status_http.service_closed",
                message="experimental status HTTP closed a registered service",
                path=self.path,
                service=service.name,
            )
            _write_response(self, "application/json; charset=utf-8", body)

        def log_message(self, _format: str, *_args: object) -> None:
            # The helper is often used in test labs; avoid writing stdlib access
            # logs unless/ until the diagnostics bus owns this helper too.
            return

    return ThreadingHTTPServer((config.listen_host, config.listen_port), Handler)


def _publish_status_http_write_event(
    diagnostics_bus: DiagnosticsBus | None,
    *,
    code: str,
    message: str,
    severity: str = "info",
    **details: object,
) -> None:
    """Publish structured write API facts without making HTTP depend on logging text."""
    if diagnostics_bus is None:
        return
    diagnostics_bus.publish(
        DiagnosticEvent.helper_event(
            code=code,
            helper="status-http",
            severity=severity,
            message=message,
            details=details,
        )
    )


def _write_response(handler: BaseHTTPRequestHandler, content_type: str, body: bytes) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
