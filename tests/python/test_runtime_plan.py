from __future__ import annotations

from pathlib import Path

from gatherlink.config import expand_config, validate_config_file
from gatherlink.config.models import GatherlinkConfig
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
    assert service_step.details["return_mode"] == "fixed"
    assert service_step.details["service_id"] == 256
    assert service_step.details["service_id_explicit"] is False


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


def test_runtime_plan_warns_when_service_id_is_explicit() -> None:
    runtime_config = expand_config(
        GatherlinkConfig(
            schema_version=1,
            role="client",
            peer="remote",
            services=[
                {
                    "name": "udp-main",
                    "service_id": 300,
                    "listen": "127.0.0.1:55180",
                    "target": "127.0.0.1:51820",
                }
            ],
        )
    )

    plan = plan_runtime_start(runtime_config)

    assert any("explicit service_id is not recommended" in warning for warning in plan.warnings)
    service_step = next(step for step in plan.steps if step.component == "core-service:udp-main")
    assert service_step.details["service_id"] == 300
    assert service_step.details["service_id_explicit"] is True


def test_runtime_plan_warns_when_static_security_is_used() -> None:
    config = validate_config_file(EXAMPLES / "windows-two-node-a.json")
    config = config.model_copy(update={"security": config.security.model_copy(update={"mode": "static"})})
    runtime_config = expand_config(config)

    plan = plan_runtime_start(runtime_config)

    assert any("security.mode=static is lab/manual" in warning for warning in plan.warnings)


def test_runtime_plan_does_not_warn_for_authenticated_security_source() -> None:
    config = validate_config_file(EXAMPLES / "windows-two-node-a.json")
    config = config.model_copy(update={"security": config.security.model_copy(update={"mode": "authenticated"})})
    runtime_config = expand_config(config)

    plan = plan_runtime_start(runtime_config)

    assert runtime_config.security.mode == "static"
    assert runtime_config.security.source_mode == "authenticated"
    assert not any("security.mode=static is lab/manual" in warning for warning in plan.warnings)
