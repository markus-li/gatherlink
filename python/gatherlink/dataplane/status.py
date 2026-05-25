"""Convert Rust dataplane snapshots into Python status shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gatherlink.control import metadata as control_metadata_helpers


def named_rust_control_metadata(control_metadata: object, runtime_config: Any) -> dict[str, object]:
    """Convert Rust path-id keyed control metadata into the generic Python monitor shape."""
    output = control_metadata_helpers.empty_control_metadata()
    if not isinstance(control_metadata, dict):
        return output
    path_names_by_id = {str(path.scheduler.path_id): path.name for path in runtime_config.paths}
    for counter_name in ["sent", "received"]:
        counter = control_metadata.get(counter_name)
        if isinstance(counter, dict):
            output[counter_name].update(counter)
    path_metadata = control_metadata.get("path_metadata")
    if isinstance(path_metadata, dict):
        output["path_metadata"] = {
            path_names_by_id.get(str(path_id), str(path_id)): path_name for path_id, path_name in path_metadata.items()
        }
        output["path_metadata_count"] = len(output["path_metadata"])
    service_metadata = control_metadata.get("service_metadata")
    if isinstance(service_metadata, dict):
        output["service_metadata"] = dict(service_metadata)
        output["service_metadata_count"] = len(output["service_metadata"])
    service_endpoint_assertions = control_metadata.get("service_endpoint_assertions")
    if isinstance(service_endpoint_assertions, dict):
        output["service_endpoint_assertions"] = dict(service_endpoint_assertions)
        output["service_endpoint_assertion_count"] = len(output["service_endpoint_assertions"])
    service_disables = control_metadata.get("service_disables")
    if isinstance(service_disables, dict):
        output["service_disables"] = dict(service_disables)
        output["service_disable_count"] = len(output["service_disables"])
    for field_name in ["path_capacity", "path_latency", "path_control", "path_pressure"]:
        keyed = control_metadata.get(field_name)
        if isinstance(keyed, dict):
            output[field_name] = {
                path_names_by_id.get(str(path_id), str(path_id)): value for path_id, value in keyed.items()
            }
            output[f"{field_name}_count"] = len(output[field_name])
    path_mtu = control_metadata.get("path_mtu")
    if isinstance(path_mtu, dict):
        output["path_mtu"] = {
            path_names_by_id.get(str(path_id), str(path_id)): value for path_id, value in path_mtu.items()
        }
        output["path_mtu_count"] = len(output["path_mtu"])
    sink_time = control_metadata.get("sink_time")
    if isinstance(sink_time, dict):
        path_id = sink_time.get("path_id")
        sink_unix_us = sink_time.get("sink_unix_us")
        sink_internal_us = sink_time.get("sink_internal_us")
        ntp_state = sink_time.get("ntp_state")
        if sink_unix_us is not None and sink_internal_us is not None:
            output["sink_time"].update(
                {
                    "role": "syncing-to-sink",
                    "system_unix_us": control_metadata_helpers.system_unix_us(),
                    "gatherlink_unix_us": int(sink_unix_us),
                    "sink_sent_unix_us": int(sink_unix_us),
                    "sink_sent_internal_us": int(sink_internal_us),
                    "received_at": datetime.now(UTC).isoformat(),
                    "path": path_names_by_id.get(str(path_id)),
                    "ntp_state": control_metadata_helpers.decode_ntp_state(int(ntp_state or 0)),
                }
            )
    return output


def merge_control_metadata(target: dict[str, object], source: dict[str, object]) -> None:
    """Overlay Python-decoded reserved-service metadata onto Rust's raw counters."""
    for key, value in source.items():
        target[key] = value


def merge_disabled_service_errors(control_metadata: dict[str, object], disabled_services: dict[str, object]) -> None:
    """Expose Rust service safety stops in the shared control metadata shape."""
    mismatches = control_metadata["service_endpoint_mismatches"]
    assert isinstance(mismatches, dict)
    service_disables = control_metadata["service_disables"]
    assert isinstance(service_disables, dict)
    for service_id, reason in disabled_services.items():
        reason_text = str(reason)
        if "peer disabled service" in reason_text:
            service_disables[str(service_id)] = reason_text
        else:
            mismatches[str(service_id)] = reason_text
    control_metadata["service_endpoint_mismatch_count"] = len(mismatches)
    control_metadata["service_disable_count"] = len(service_disables)


def named_rust_path_stats(snapshot: dict[str, object], runtime_config: Any) -> dict[str, dict[str, int]]:
    """Map Rust path-id keyed counters back to configured path names for monitoring."""
    path_names_by_id = {str(path.scheduler.path_id): path.name for path in runtime_config.paths}
    raw_paths = snapshot.get("path_stats", {})
    if not isinstance(raw_paths, dict):
        return {}
    named: dict[str, dict[str, int]] = {}
    for path_id, counters in raw_paths.items():
        name = path_names_by_id.get(str(path_id), f"path-id:{path_id}")
        named[name] = {key: int(value) for key, value in dict(counters).items()}
    return named


def named_rust_service_stats(snapshot: dict[str, object]) -> dict[str, dict[str, int]]:
    """Return Rust service counters in a Python-owned, monitor-friendly shape."""
    raw_services = snapshot.get("services", {})
    if not isinstance(raw_services, dict):
        return {}
    named: dict[str, dict[str, int]] = {}
    for service_name, counters in raw_services.items():
        if isinstance(counters, dict):
            named[str(service_name)] = {key: int(value) for key, value in counters.items()}
    return named


def named_rust_service_path_stats(
    snapshot: dict[str, object], runtime_config: Any
) -> dict[str, dict[str, dict[str, int]]]:
    """
    Map Rust service/path intersection counters back to configured path names.

    The Rust side only reports facts keyed by the compact numeric path ids it
    executes. Python owns naming and scheduler meaning, so this converter keeps
    the operator/scheduler-facing shape tied to configured path names.
    """
    raw_service_paths = snapshot.get("service_path_stats", {})
    if not isinstance(raw_service_paths, dict):
        return {}
    path_names_by_id = {str(path.scheduler.path_id): path.name for path in runtime_config.paths}
    named: dict[str, dict[str, dict[str, int]]] = {}
    for service_name, raw_paths in raw_service_paths.items():
        if not isinstance(raw_paths, dict):
            continue
        service_rows: dict[str, dict[str, int]] = {}
        for path_id, counters in raw_paths.items():
            if not isinstance(counters, dict):
                continue
            path_name = path_names_by_id.get(str(path_id), f"path-id:{path_id}")
            service_rows[path_name] = {key: int(value) for key, value in counters.items()}
        named[str(service_name)] = service_rows
    return named
