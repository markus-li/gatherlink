"""Live rekey coordination for Python-owned authenticated sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gatherlink.config.runtime import RuntimeConfig, RuntimeSecurityConfig
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.security.sessions import (
    DEFAULT_REKEY_MARGIN_SECONDS,
    DEFAULT_REKEY_OVERLAP_SECONDS,
    AuthenticatedSessionPlan,
    SessionRotationDecision,
    plan_session_rotation,
)


@dataclass(frozen=True)
class RekeyRuntimeResult:
    """Outcome of one Python-owned rekey evaluation pass."""

    decision: SessionRotationDecision
    runtime_config: RuntimeConfig
    applied: bool = False
    fail_closed: bool = False


def evaluate_rekey_from_status(
    current_session: AuthenticatedSessionPlan,
    runtime_config: RuntimeConfig,
    status: dict[str, Any],
    *,
    replacement_session: AuthenticatedSessionPlan | None = None,
    now: datetime | None = None,
    diagnostics: DiagnosticsBus | None = None,
    rekey_margin_seconds: int = DEFAULT_REKEY_MARGIN_SECONDS,
    overlap_seconds: int = DEFAULT_REKEY_OVERLAP_SECONDS,
) -> RekeyRuntimeResult:
    """
    Evaluate one live rekey pass from service status and optional replacement.

    Python owns the timing, topology, identity, and diagnostics here. Rust only
    receives the resulting low-level AEAD facts after the replacement session is
    already validated against the currently trusted peer and topology.
    """
    now = now or datetime.now(UTC)
    packets, bytes_transferred = _traffic_totals(status)
    decision = plan_session_rotation(
        current_session,
        now=now,
        packets=packets,
        bytes_transferred=bytes_transferred,
        observed_topology_generation=_optional_int(status.get("topology_generation")),
        observed_peer_node_id=_optional_str(status.get("peer_node_id")),
        rekey_margin_seconds=rekey_margin_seconds,
        overlap_seconds=overlap_seconds,
    )

    if decision.action == "keep":
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config)
    if decision.fail_closed:
        _publish_rekey_diagnostic(diagnostics, decision)
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config, fail_closed=True)
    if replacement_session is None:
        _publish_rekey_diagnostic(diagnostics, decision)
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config)

    validation_error = _replacement_validation_error(current_session, replacement_session)
    if validation_error is not None:
        rejected = SessionRotationDecision(
            action="reject",
            reason=validation_error,
            current_receiver_index=current_session.receiver_index,
        )
        _publish_rekey_diagnostic(diagnostics, rejected)
        return RekeyRuntimeResult(decision=rejected, runtime_config=runtime_config, fail_closed=True)

    updated = runtime_config.model_copy(update={"security": runtime_security_from_session(replacement_session)})
    _publish_rekey_diagnostic(
        diagnostics,
        SessionRotationDecision(
            action="rekey",
            reason="replacement session hot-applied",
            current_receiver_index=current_session.receiver_index,
            next_receiver_index=replacement_session.receiver_index,
            overlap_until=decision.overlap_until,
        ),
        code="rekey.succeeded",
        message="session rekey succeeded",
        peer=replacement_session.peer_node_id,
    )
    return RekeyRuntimeResult(decision=decision, runtime_config=updated, applied=True)


def runtime_security_from_session(session: AuthenticatedSessionPlan) -> RuntimeSecurityConfig:
    """Compile an authenticated session into the narrow runtime security DTO."""
    if session.security is None:
        raise ValueError("replacement session has no compiled transport security")
    return RuntimeSecurityConfig(
        mode="static",
        source_mode="authenticated",
        receiver_index=session.security.remote_receiver_index or session.receiver_index,
        local_receiver_index=session.security.local_receiver_index or session.receiver_index,
        remote_receiver_index=session.security.remote_receiver_index or session.receiver_index,
        send_key=session.security.send_key,
        receive_key=session.security.receive_key,
    )


def _replacement_validation_error(
    current_session: AuthenticatedSessionPlan,
    replacement_session: AuthenticatedSessionPlan,
) -> str | None:
    """Return why a replacement must fail closed, or ``None`` when it is safe to apply."""
    if replacement_session.security is None:
        return "replacement session has no compiled transport security"
    if replacement_session.peer_node_id != current_session.peer_node_id:
        return "replacement peer identity disagrees with current session"
    if replacement_session.topology_generation != current_session.topology_generation:
        return "replacement topology generation disagrees with current session"
    if replacement_session.receiver_index == current_session.receiver_index:
        return "replacement receiver index must differ from current session"
    return None


def _publish_rekey_diagnostic(
    diagnostics: DiagnosticsBus | None,
    decision: SessionRotationDecision,
    *,
    code: str | None = None,
    message: str | None = None,
    peer: str | None = None,
) -> None:
    """Publish a stable rekey event without making diagnostics mandatory."""
    if diagnostics is None:
        return
    event_code = code or {
        "rekey": "rekey.started",
        "reject": "rekey.rejected",
        "expired": "rekey.expired",
    }.get(decision.action, "rekey.started")
    event_message = message or {
        "rekey": "session rekey started",
        "reject": "session rekey rejected",
        "expired": "session expired before replacement completed",
    }.get(decision.action, "session rekey started")
    diagnostics.publish(
        DiagnosticEvent.rekey_event(
            code=event_code,
            message=event_message,
            peer=peer,
            severity="warning" if decision.fail_closed else "info",
            details=decision.export_public_summary(),
        )
    )


def _traffic_totals(status: dict[str, Any]) -> tuple[int, int]:
    """Return aggregate packet and byte counters from the shared status shape."""
    services = status.get("services")
    if not isinstance(services, dict):
        return 0, 0
    packets = 0
    bytes_transferred = 0
    for counters in services.values():
        if not isinstance(counters, dict):
            continue
        packets += _optional_int(counters.get("packets")) or 0
        bytes_transferred += _optional_int(counters.get("bytes")) or 0
    return packets, bytes_transferred


def _optional_int(value: object) -> int | None:
    """Return a non-negative integer from loose status metadata."""
    if value is None:
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _optional_str(value: object) -> str | None:
    """Return a non-empty string from loose status metadata."""
    if value is None:
        return None
    converted = str(value)
    return converted or None
