from __future__ import annotations

from pathlib import Path

from gatherlink.config import expand_config, validate_config_file
from gatherlink.runtime import plan_runtime_start

EXAMPLES = Path("configs/examples")


def test_runtime_plan_is_core_userland_udp_and_non_root() -> None:
    runtime_config = expand_config(validate_config_file(EXAMPLES / "minimal-client.json"))
    plan = plan_runtime_start(runtime_config)

    assert plan.transport_target == "core-userland-udp"
    assert plan.requires_root is False
    assert plan.warnings
    assert "security.mode=none" in plan.warnings[0]
    assert all(step.requires_root is False for step in plan.steps)
    assert all(step.mode in {"core-userland-udp", "core-dataplane"} for step in plan.steps)


def test_runtime_plan_binds_udp_listener_for_core_service() -> None:
    runtime_config = expand_config(validate_config_file(EXAMPLES / "minimal-client.json"))
    plan = plan_runtime_start(runtime_config)

    service_step = next(step for step in plan.steps if step.component == "core-service:udp-main")
    assert service_step.action == "bind_udp_listener"
    assert service_step.details["listen"] == "127.0.0.1:55180"
    assert service_step.details["target"] == "127.0.0.1:51820"
    assert service_step.details["priority"] == "normal"
    assert service_step.details["priority_value"] == 100


def test_runtime_plan_does_not_start_helpers_or_tunnels() -> None:
    runtime_config = expand_config(validate_config_file(EXAMPLES / "wireguard-client.json"))
    plan = plan_runtime_start(runtime_config)

    assert plan.helper_count == 1
    assert all("helper" not in step.component for step in plan.steps)
    assert all("tunnel" not in step.action for step in plan.steps)
    assert plan.steps[0].details["helpers_ignored_by_core"] == 1


def test_runtime_plan_preserves_ipv6_udp_endpoints() -> None:
    runtime_config = expand_config(validate_config_file(EXAMPLES / "minimal-ipv6-client.json"))
    plan = plan_runtime_start(runtime_config)

    service_step = next(step for step in plan.steps if step.component == "core-service:udp-v6")
    assert service_step.details["listen"] == "[::1]:55180"
    assert service_step.details["target"] == "[::1]:51820"
    assert plan.steps[-1].details["paths"][0]["source_ip"] == "2001:db8::10"
    assert plan.steps[-1].details["scheduler"]["mode"] == "round_robin"


def test_runtime_plan_includes_plaintext_warning_details() -> None:
    runtime_config = expand_config(validate_config_file(EXAMPLES / "minimal-client.json"))
    plan = plan_runtime_start(runtime_config)

    core_step = plan.steps[0]
    assert core_step.details["security_mode"] == "none"
    assert core_step.details["warnings"] == plan.warnings
