"""Apply Python-decoded peer control policy to the local Rust dataplane."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def apply_control_policy_to_dataplane(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    applied_disabled_services: set[str] | None = None,
    logger: Callable[[str], None] | None = None,
) -> int:
    """
    Compile peer-visible control policy into Rust executor state.

    Python decodes control metadata, validates it against local config, and only
    then tells Rust the narrow primitive it should execute. The optional
    ``applied_disabled_services`` set lets long-running services keep logs and
    disable calls idempotent across repeated control frames.
    """
    applied = 0
    applied += _apply_service_scheduler_policies(dataplane, control_metadata, logger=logger)
    applied += _apply_endpoint_stops(
        dataplane,
        control_metadata,
        field_name="service_endpoint_mismatches",
        source="Python policy",
        applied_disabled_services=applied_disabled_services,
        logger=logger,
    )
    applied += _apply_endpoint_stops(
        dataplane,
        control_metadata,
        field_name="service_disables",
        source="peer policy",
        applied_disabled_services=applied_disabled_services,
        logger=logger,
    )
    return applied


def _apply_service_scheduler_policies(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    logger: Callable[[str], None] | None,
) -> int:
    policies = control_metadata.get("service_scheduler_policies")
    if not isinstance(policies, dict):
        return 0
    applied = 0
    for service_id_text, policy in policies.items():
        if not isinstance(policy, dict):
            continue
        try:
            service_id = int(service_id_text)
            fanout = int(policy.get("fanout", 1) or 1)
            fanout_below_bytes = int(policy.get("fanout_below_bytes", 0) or 0)
        except (TypeError, ValueError):
            _log(logger, f"invalid service scheduler policy for service id {service_id_text!r}; ignoring")
            continue
        dataplane.set_service_scheduler(service_id, fanout, fanout_below_bytes)
        applied += 1
    return applied


def _apply_endpoint_stops(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    field_name: str,
    source: str,
    applied_disabled_services: set[str] | None,
    logger: Callable[[str], None] | None,
) -> int:
    stops = control_metadata.get(field_name)
    if not isinstance(stops, dict):
        return 0
    applied = 0
    for service_id_text, reason in stops.items():
        service_key = str(service_id_text)
        if applied_disabled_services is not None and service_key in applied_disabled_services:
            continue
        try:
            service_id = int(service_id_text)
        except (TypeError, ValueError):
            _log(logger, f"invalid disabled service id {service_id_text!r} from {source}; ignoring")
            continue
        if applied_disabled_services is not None:
            applied_disabled_services.add(service_key)
        dataplane.disable_service(service_id, str(reason))
        _log(logger, f"SERVICE DISABLED by {source} id={service_id} reason={reason}")
        applied += 1
    return applied


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is None:
        return
    logger(message)
