"""
Normalized diagnostic event DTOs for Gatherlink.

Diagnostics are a Python-owned control-plane contract. Producers publish
structured facts with stable codes; terminal views, logs, JSONL, and future
sinks render those same facts without inventing one-off message formats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from gatherlink.shared.models import GatherlinkBaseModel

DiagnosticSeverity = Literal["debug", "info", "warning", "error", "critical"]
DiagnosticEventKind = Literal[
    "warning",
    "service_bound",
    "config_reapplied",
    "packet_forwarded",
    "counter_snapshot",
    "shutdown",
    "drop",
    "helper",
    "runtime",
]

STABLE_EVENT_CODES: frozenset[str] = frozenset(
    {
        "config.reapplied",
        "carrier.connect_failed",
        "carrier.datagram_dropped",
        "carrier.datagram_received",
        "carrier.datagram_sent",
        "carrier.ready",
        "carrier.closed",
        "counter.snapshot",
        "crypto.auth_failed",
        "crypto.replay_drop",
        "crypto.unknown_receiver_index",
        "diagnostics.queue_dropped",
        "dns.dnssec_bogus",
        "dns.policy_denied",
        "dns.upstream_failed",
        "helper.time.set_failed",
        "helper.wireguard.plan",
        "helper.stream.closed",
        "helper.stream.denied",
        "helper.stream.invalid_frame",
        "helper.stream.opened",
        "helper.stream.unreachable",
        "helper.status_http.non_loopback_bind",
        "helper.status_http.service_closed",
        "helper.status_http.started",
        "helper.status_http.write_denied",
        "helper.status_http.write_failed",
        "helper.lifecycle.start_failed",
        "helper.lifecycle.started",
        "packet.forwarded",
        "rekey.expired",
        "rekey.rejected",
        "rekey.started",
        "rekey.succeeded",
        "relay.auth_failed",
        "relay.expired_session",
        "relay.generation_stale",
        "relay.limit_exceeded",
        "relay.packet_too_large",
        "relay.replay_drop",
        "relay.unknown_receiver_index",
        "relay.unauthorized_next_hop",
        "runtime.start_failed",
        "runtime.shutdown",
        "service.bound",
        "socks.exit_denied",
        "socks.exit_unreachable",
        "warning",
    }
)


class DiagnosticEvent(GatherlinkBaseModel):
    """One normalized operator or machine-readable diagnostic event."""

    schema_version: int = 1
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    code: str
    kind: DiagnosticEventKind
    severity: DiagnosticSeverity = "info"
    message: str
    node: str | None = None
    service: str | None = None
    path: str | None = None
    helper: str | None = None
    peer: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        code: str = "warning",
        service: str | None = None,
        path: str | None = None,
        helper: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a warning event from structured facts."""
        return cls(
            code=code,
            kind="warning",
            severity="warning",
            message=message,
            service=service,
            path=path,
            helper=helper,
            details=details or {},
        )

    @classmethod
    def service_bound(
        cls,
        *,
        service: str,
        listen: str | None,
        target: str,
        node: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build the first lifecycle event emitted when a UDP service binds."""
        merged_details = {"listen": listen, "target": target}
        if details:
            merged_details.update(details)
        return cls(
            code="service.bound",
            kind="service_bound",
            severity="info",
            message=f"service {service} bound",
            node=node,
            service=service,
            details=merged_details,
        )

    @classmethod
    def config_reapplied(
        cls,
        *,
        node: str | None = None,
        service: str | None = None,
        generation: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a runtime config reload/reapply event."""
        merged_details: dict[str, Any] = {}
        if generation is not None:
            merged_details["generation"] = generation
        if details:
            merged_details.update(details)
        return cls(
            code="config.reapplied",
            kind="config_reapplied",
            severity="info",
            message="runtime config reapplied",
            node=node,
            service=service,
            details=merged_details,
        )

    @classmethod
    def packet_forwarded(
        cls,
        *,
        service: str,
        path: str | None,
        packets: int,
        bytes_forwarded: int,
        node: str | None = None,
    ) -> DiagnosticEvent:
        """Build a packet forwarding event sample or aggregate."""
        return cls(
            code="packet.forwarded",
            kind="packet_forwarded",
            severity="debug",
            message=f"forwarded packets for {service}",
            node=node,
            service=service,
            path=path,
            details={"packets": packets, "bytes": bytes_forwarded},
        )

    @classmethod
    def counter_snapshot(
        cls,
        *,
        counters: dict[str, Any],
        service: str | None = None,
        path: str | None = None,
        node: str | None = None,
    ) -> DiagnosticEvent:
        """Build a periodic counter snapshot event."""
        return cls(
            code="counter.snapshot",
            kind="counter_snapshot",
            severity="debug",
            message="counter snapshot",
            node=node,
            service=service,
            path=path,
            details=counters,
        )

    @classmethod
    def shutdown(
        cls,
        *,
        reason: str,
        node: str | None = None,
        service: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a service or runtime shutdown event."""
        merged_details = {"reason": reason}
        if details:
            merged_details.update(details)
        return cls(
            code="runtime.shutdown",
            kind="shutdown",
            severity="info",
            message=f"shutdown: {reason}",
            node=node,
            service=service,
            details=merged_details,
        )

    @classmethod
    def runtime_start_failed(
        cls,
        *,
        message: str,
        node: str | None = None,
        service: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a structured startup failure event for foreground services."""
        return cls(
            code="runtime.start_failed",
            kind="runtime",
            severity="error",
            message=message,
            node=node,
            service=service,
            details=details or {},
        )

    @classmethod
    def helper_event(
        cls,
        *,
        code: str,
        helper: str,
        message: str,
        severity: DiagnosticSeverity = "info",
        service: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a structured helper diagnostic without inventing ad hoc log text."""
        return cls(
            code=code,
            kind="helper",
            severity=severity,
            message=message,
            service=service,
            helper=helper,
            details=details or {},
        )

    @classmethod
    def drop_event(
        cls,
        *,
        code: str,
        message: str,
        node: str | None = None,
        service: str | None = None,
        path: str | None = None,
        peer: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a local-only silent network drop diagnostic."""
        return cls(
            code=code,
            kind="drop",
            severity="warning",
            message=message,
            node=node,
            service=service,
            path=path,
            peer=peer,
            details=details or {},
        )

    @classmethod
    def rekey_event(
        cls,
        *,
        code: str,
        message: str,
        peer: str | None = None,
        severity: DiagnosticSeverity = "info",
        details: dict[str, Any] | None = None,
    ) -> DiagnosticEvent:
        """Build a Python-owned session lifecycle diagnostic."""
        return cls(
            code=code,
            kind="runtime",
            severity=severity,
            message=message,
            peer=peer,
            details=details or {},
        )

    @property
    def stable_code(self) -> bool:
        """Return whether the event code is part of the documented stable set."""
        return self.code in STABLE_EVENT_CODES
