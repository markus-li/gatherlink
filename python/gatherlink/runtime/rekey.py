"""Live rekey coordination for Python-owned authenticated sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gatherlink.config.runtime import RuntimeConfig, RuntimeSecurityConfig
from gatherlink.diagnostics.bus import DiagnosticsBus
from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.secrets.provisioning import TopologyBundleBody
from gatherlink.security.keys import NodeIdentity
from gatherlink.security.rekey_control import (
    PendingRekeyInitiation,
    RekeyControlMessage,
    accept_rekey_initiation_payload,
    complete_rekey_response_payload,
    create_rekey_initiation,
    create_rekey_reject,
)
from gatherlink.security.sessions import (
    DEFAULT_REKEY_AFTER_BYTES,
    DEFAULT_REKEY_AFTER_PACKETS,
    DEFAULT_REKEY_MARGIN_SECONDS,
    DEFAULT_REKEY_OVERLAP_SECONDS,
    AuthenticatedSessionPlan,
    SessionRotationDecision,
    StaticTransportSecurityMaterial,
    plan_session_rotation,
)


@dataclass(frozen=True)
class RekeyRuntimeResult:
    """Outcome of one Python-owned rekey evaluation pass."""

    decision: SessionRotationDecision
    runtime_config: RuntimeConfig
    applied: bool = False
    fail_closed: bool = False


@dataclass(frozen=True)
class RekeyInitiationRuntimeResult:
    """Local rekey request bytes plus pending state kept by Python."""

    pending: PendingRekeyInitiation
    payload: bytes


@dataclass(frozen=True)
class RekeyResponseRuntimeResult:
    """Responder-side response bytes plus optional hot-applied runtime state."""

    response_payload: bytes
    runtime_result: RekeyRuntimeResult


@dataclass(frozen=True)
class LiveRekeyOutboundMessage:
    """Reserved auth/crypto payload Python should ask Rust to transmit."""

    peer_node_id: str
    message_type: str
    payload: bytes


@dataclass(frozen=True)
class LiveRekeyHandleResult:
    """Result of handling one peer rekey payload."""

    outbound: LiveRekeyOutboundMessage | None = None
    runtime_result: RekeyRuntimeResult | None = None
    accepted: bool = True
    reason: str | None = None


@dataclass
class LiveRekeyRuntimeContext:
    """
    Local Python-only context required for autonomous authenticated rekey.

    This context deliberately stays out of Rust. Rust receives only reserved
    service bytes and compiled AEAD replacement facts after Python validates
    identity, topology, receiver-index direction, and expiry.
    """

    local_identity: NodeIdentity
    peer_identity: IdentityPublicRecord
    topology: TopologyBundleBody
    current_session: AuthenticatedSessionPlan
    coordinator: LiveRekeyCoordinator = field(default_factory=lambda: LiveRekeyCoordinator())


class LiveRekeyCoordinator:
    """
    Minimal Python-owned live rekey state machine.

    The coordinator owns retry suppression, pending Noise state, message
    validation, and hot-apply decisions. Rust still only carries bytes on the
    reserved auth/crypto service and later executes compiled AEAD facts.
    """

    def __init__(self) -> None:
        self._pending_by_peer: dict[str, PendingRekeyInitiation] = {}

    def maybe_start(
        self,
        local_identity: NodeIdentity,
        peer_identity: IdentityPublicRecord,
        topology: TopologyBundleBody,
        current_session: AuthenticatedSessionPlan,
        runtime_config: RuntimeConfig,
        status: dict[str, Any],
        *,
        now: datetime | None = None,
        diagnostics: DiagnosticsBus | None = None,
        rekey_margin_seconds: int = DEFAULT_REKEY_MARGIN_SECONDS,
        overlap_seconds: int = DEFAULT_REKEY_OVERLAP_SECONDS,
    ) -> LiveRekeyOutboundMessage | None:
        """
        Start one outbound rekey when policy says it is time.

        A still-valid pending request suppresses duplicate initiations. This is
        deliberately stateful in Python because retries and operator meaning
        belong to the control plane, not to Rust packet execution.
        """
        now = now or datetime.now(UTC)
        pending = self._pending_by_peer.get(peer_identity.node_id)
        if pending is not None and (pending.message.expires_at is None or pending.message.expires_at > now):
            return None
        if pending is not None:
            del self._pending_by_peer[peer_identity.node_id]

        evaluation = evaluate_rekey_from_status(
            current_session,
            runtime_config,
            status,
            now=now,
            diagnostics=diagnostics,
            rekey_margin_seconds=rekey_margin_seconds,
            overlap_seconds=overlap_seconds,
        )
        if not evaluation.decision.should_rekey or evaluation.decision.next_receiver_index is None:
            return None

        initiation = create_runtime_rekey_initiation(
            local_identity,
            peer_identity,
            topology,
            current_session,
            now=now,
            receiver_index=evaluation.decision.next_receiver_index,
        )
        self._pending_by_peer[peer_identity.node_id] = initiation.pending
        return LiveRekeyOutboundMessage(
            peer_node_id=peer_identity.node_id,
            message_type=initiation.pending.message.message_type,
            payload=initiation.payload,
        )

    def handle_peer_payload(
        self,
        local_identity: NodeIdentity,
        payload: bytes,
        topology: TopologyBundleBody,
        current_session: AuthenticatedSessionPlan,
        runtime_config: RuntimeConfig,
        status: dict[str, Any],
        *,
        now: datetime | None = None,
        diagnostics: DiagnosticsBus | None = None,
    ) -> LiveRekeyHandleResult:
        """Handle a peer rekey request, response, or rejection."""
        now = now or datetime.now(UTC)
        try:
            message = RekeyControlMessage.decode(payload)
        except ValueError as exc:
            return LiveRekeyHandleResult(accepted=False, reason=str(exc))

        if message.message_type == "rekey_initiation":
            return self._handle_initiation(
                local_identity,
                payload,
                message,
                topology,
                current_session,
                runtime_config,
                status,
                now=now,
                diagnostics=diagnostics,
            )
        if message.message_type == "rekey_response":
            pending = self._pending_by_peer.get(message.sender_node_id)
            if pending is None:
                return LiveRekeyHandleResult(accepted=False, reason="no pending rekey initiation for response")
            result = complete_runtime_rekey_response(
                local_identity,
                pending,
                payload,
                topology,
                current_session,
                runtime_config,
                status,
                now=now,
                diagnostics=diagnostics,
            )
            if result.applied or result.fail_closed:
                self._pending_by_peer.pop(message.sender_node_id, None)
            return LiveRekeyHandleResult(runtime_result=result, accepted=result.applied and not result.fail_closed)
        if message.message_type == "rekey_reject":
            self._pending_by_peer.pop(message.sender_node_id, None)
            _publish_rekey_diagnostic(
                diagnostics,
                SessionRotationDecision(
                    action="reject",
                    reason=message.reason or "peer rejected rekey",
                    current_receiver_index=message.current_receiver_index,
                ),
                peer=message.sender_node_id,
            )
            return LiveRekeyHandleResult(accepted=False, reason=message.reason or "peer rejected rekey")
        return LiveRekeyHandleResult(accepted=False, reason="unsupported rekey control type")

    def _handle_initiation(
        self,
        local_identity: NodeIdentity,
        payload: bytes,
        message: RekeyControlMessage,
        topology: TopologyBundleBody,
        current_session: AuthenticatedSessionPlan,
        runtime_config: RuntimeConfig,
        status: dict[str, Any],
        *,
        now: datetime,
        diagnostics: DiagnosticsBus | None,
    ) -> LiveRekeyHandleResult:
        """Accept one initiation or return a bounded rejection payload."""
        try:
            response = accept_runtime_rekey_initiation(
                local_identity,
                payload,
                topology,
                current_session,
                runtime_config,
                status,
                now=now,
                diagnostics=diagnostics,
            )
        except ValueError as exc:
            reject = create_rekey_reject(
                local_identity,
                message.sender_node_id,
                current_session,
                str(exc),
                now=now,
            )
            _publish_rekey_diagnostic(
                diagnostics,
                SessionRotationDecision(
                    action="reject",
                    reason=str(exc),
                    current_receiver_index=current_session.receiver_index,
                ),
                peer=message.sender_node_id,
            )
            return LiveRekeyHandleResult(
                outbound=LiveRekeyOutboundMessage(
                    peer_node_id=message.sender_node_id,
                    message_type=reject.message_type,
                    payload=reject.encode(),
                ),
                accepted=False,
                reason=str(exc),
            )
        return LiveRekeyHandleResult(
            outbound=LiveRekeyOutboundMessage(
                peer_node_id=message.sender_node_id,
                message_type="rekey_response",
                payload=response.response_payload,
            ),
            runtime_result=response.runtime_result,
            accepted=response.runtime_result.applied and not response.runtime_result.fail_closed,
        )


def live_rekey_context_from_runtime(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    runtime_config: RuntimeConfig,
) -> LiveRekeyRuntimeContext:
    """
    Build a live rekey context from already verified provisioning inputs.

    This helper refuses to guess. Old authenticated configs without keyless
    session metadata can still execute packets, but they cannot originate or
    accept autonomous live rekey because Python cannot prove the current
    topology and peer binding.
    """
    current_session = authenticated_session_from_runtime_config(runtime_config)
    if current_session is None:
        raise ValueError("runtime security does not contain live-rekey-capable authenticated session metadata")
    local_public = IdentityPublicRecord.from_identity(local_identity)
    if current_session.local_node_id != local_public.node_id:
        raise ValueError("local identity does not match runtime authenticated session")
    if current_session.peer_node_id != peer_identity.node_id:
        raise ValueError("peer identity does not match runtime authenticated session")
    if current_session.topology_generation != topology.generation:
        raise ValueError("topology generation does not match runtime authenticated session")
    topology_node_ids = {node.identity.node_id for node in topology.nodes}
    if local_public.node_id not in topology_node_ids:
        raise ValueError("local identity is not present in topology bundle")
    if peer_identity.node_id not in topology_node_ids:
        raise ValueError("peer identity is not present in topology bundle")
    if local_public.node_id in topology.revoked_node_ids:
        raise ValueError("local identity is revoked in topology bundle")
    if peer_identity.node_id in topology.revoked_node_ids:
        raise ValueError("peer identity is revoked in topology bundle")
    return LiveRekeyRuntimeContext(
        local_identity=local_identity,
        peer_identity=peer_identity,
        topology=topology,
        current_session=current_session,
    )


def authenticated_session_from_runtime_config(runtime_config: RuntimeConfig) -> AuthenticatedSessionPlan | None:
    """
    Reconstruct Python-owned authenticated session policy from runtime metadata.

    Runtime security has always carried the AEAD facts Rust needs. V0.9.3
    authenticated provisioning also records enough keyless metadata for Python
    to evaluate rekey policy later. Static/manual configs and older
    authenticated blocks without metadata return ``None`` instead of guessing.
    """
    security = runtime_config.security
    if security.source_mode != "authenticated" or security.sessions:
        return None
    required = [
        security.local_node_id,
        security.peer_node_id,
        security.topology_generation,
        security.session_role,
        security.session_created_at,
        security.session_expires_at,
        security.send_key,
        security.receive_key,
    ]
    if any(value is None for value in required):
        return None
    return AuthenticatedSessionPlan(
        local_node_id=str(security.local_node_id),
        peer_node_id=str(security.peer_node_id),
        topology_generation=int(security.topology_generation),
        receiver_index=int(security.receiver_index),
        role=security.session_role,  # type: ignore[arg-type]
        created_at=security.session_created_at,  # type: ignore[arg-type]
        expires_at=security.session_expires_at,  # type: ignore[arg-type]
        rekey_after_packets=security.rekey_after_packets or DEFAULT_REKEY_AFTER_PACKETS,
        rekey_after_bytes=security.rekey_after_bytes or DEFAULT_REKEY_AFTER_BYTES,
        security=StaticTransportSecurityMaterial(
            receiver_index=int(security.receiver_index),
            local_receiver_index=int(security.local_receiver_index),
            remote_receiver_index=int(security.remote_receiver_index),
            send_key=security.send_key,  # type: ignore[arg-type]
            receive_key=security.receive_key,  # type: ignore[arg-type]
        ),
    )


def create_runtime_rekey_initiation(
    local_identity: NodeIdentity,
    peer_identity: IdentityPublicRecord,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    *,
    now: datetime | None = None,
    receiver_index: int | None = None,
) -> RekeyInitiationRuntimeResult:
    """Create a rekey initiation payload and retain the pending Noise state."""
    pending = create_rekey_initiation(
        local_identity,
        peer_identity,
        topology,
        current_session,
        now=now,
        receiver_index=receiver_index,
    )
    return RekeyInitiationRuntimeResult(pending=pending, payload=pending.encode())


def accept_runtime_rekey_initiation(
    responder_identity: NodeIdentity,
    payload: bytes,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    runtime_config: RuntimeConfig,
    status: dict[str, Any],
    *,
    now: datetime | None = None,
    receiver_index: int | None = None,
    diagnostics: DiagnosticsBus | None = None,
) -> RekeyResponseRuntimeResult:
    """Accept a peer rekey initiation and hot-apply the responder replacement."""
    accepted = accept_rekey_initiation_payload(
        responder_identity,
        payload,
        topology,
        current_session,
        now=now,
        receiver_index=receiver_index,
    )
    runtime_result = evaluate_rekey_from_status(
        current_session,
        runtime_config,
        status,
        replacement_session=accepted.session,
        now=now,
        diagnostics=diagnostics,
    )
    return RekeyResponseRuntimeResult(response_payload=accepted.encode(), runtime_result=runtime_result)


def complete_runtime_rekey_response(
    local_identity: NodeIdentity,
    pending: PendingRekeyInitiation,
    payload: bytes,
    topology: TopologyBundleBody,
    current_session: AuthenticatedSessionPlan,
    runtime_config: RuntimeConfig,
    status: dict[str, Any],
    *,
    now: datetime | None = None,
    diagnostics: DiagnosticsBus | None = None,
) -> RekeyRuntimeResult:
    """Complete a peer rekey response and hot-apply the initiator replacement."""
    replacement = complete_rekey_response_payload(
        local_identity,
        pending,
        payload,
        topology,
        current_session,
        now=now,
    )
    return evaluate_rekey_from_status(
        current_session,
        runtime_config,
        status,
        replacement_session=replacement,
        now=now,
        diagnostics=diagnostics,
    )


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

    if decision.action == "keep" and replacement_session is None:
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config)
    if decision.fail_closed:
        _publish_rekey_diagnostic(diagnostics, decision)
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config, fail_closed=True)
    if replacement_session is None:
        _publish_rekey_diagnostic(diagnostics, decision)
        return RekeyRuntimeResult(decision=decision, runtime_config=runtime_config)

    return _apply_replacement_session(
        current_session,
        runtime_config,
        replacement_session,
        decision,
        diagnostics=diagnostics,
    )


def _apply_replacement_session(
    current_session: AuthenticatedSessionPlan,
    runtime_config: RuntimeConfig,
    replacement_session: AuthenticatedSessionPlan,
    decision: SessionRotationDecision,
    *,
    diagnostics: DiagnosticsBus | None,
) -> RekeyRuntimeResult:
    """Validate and apply one replacement session or fail closed with diagnostics."""
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
    """
    Compile an authenticated session into the narrow runtime security DTO.

    The AEAD fields are the only values Rust needs for packet execution, but
    Python must keep the keyless session metadata beside them so the next live
    rekey evaluation can prove peer identity, topology generation, expiry, and
    receiver-index direction without rereading secret provisioning files.
    """
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
        local_node_id=session.local_node_id,
        peer_node_id=session.peer_node_id,
        topology_generation=session.topology_generation,
        session_role=session.role,
        session_created_at=session.created_at,
        session_expires_at=session.expires_at,
        rekey_after_packets=session.rekey_after_packets,
        rekey_after_bytes=session.rekey_after_bytes,
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
    services = status.get("service_stats")
    if not isinstance(services, dict):
        # Older unit-level callers and a few pre-IPC helpers used ``services``
        # as the counter map. The live service registry uses ``services`` for
        # names and ``service_stats`` for counters, so prefer the real runtime
        # shape and keep this fallback only for narrow test/compat callers.
        services = status.get("services")
    if not isinstance(services, dict):
        packets = (_optional_int(status.get("tx_packets")) or 0) + (_optional_int(status.get("rx_packets")) or 0)
        bytes_transferred = (_optional_int(status.get("tx_bytes")) or 0) + (_optional_int(status.get("rx_bytes")) or 0)
        return packets, bytes_transferred
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
