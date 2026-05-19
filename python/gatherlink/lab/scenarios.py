"""Extensible local lab scenario models and planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from gatherlink.config.models import SecurityConfig
from gatherlink.shared.models import GatherlinkBaseModel

LabScenarioKind = Literal[
    "local-dual-path",
    "local-multi-path",
    "ipv6-dual-path",
    "path-failure",
    "raw-udp-blocked-wss",
    "mtu-mismatch",
    "receiver-metrics-loss",
    "peer-failover",
    "dns-helper-racing",
    "bootstrap-candidates",
    "same-subnet-gateway-validation",
    "long-running-soak",
]
LabPlanStatus = Literal["supported", "not_implemented"]
LabShapeSide = Literal["local", "remote", "both"]
DEFAULT_REORDER_MAX_HOLD = "150ms"
DEFAULT_REORDER_MIN_HOLD = "2ms"


class LabShapeConfig(GatherlinkBaseModel):
    """Optional Linux `tc` shaping requested for one simulated path."""

    rate: str | None = None
    delay: str | None = None
    jitter: str | None = None
    loss: str | None = None
    reorder: str | None = None
    limit: int | None = None
    mtu: int | None = None
    state: Literal["up", "down"] | None = None
    blackhole: bool = False
    recovery_after: str | None = None


class LabPathConfig(GatherlinkBaseModel):
    """One simulated lab path between the client and server nodes."""

    name: str
    family: Literal["ipv4", "ipv6"] = "ipv4"
    client_address: str
    server_address: str
    subnet: str
    default_max_speed: str | None = None
    shape: LabShapeConfig = Field(default_factory=LabShapeConfig)


class LabNodeConfig(GatherlinkBaseModel):
    """One Gatherlink process the lab should eventually launch."""

    name: str
    role: Literal["client", "server"]
    config_path: str
    run_as: str = "current-user"


class LabTrafficConfig(GatherlinkBaseModel):
    """Normal UDP traffic generator and sink settings for the lab."""

    generator: Literal["iperf3", "socat", "nc", "builtin"] = "iperf3"
    listen: str
    target: str
    duration_seconds: int = 10
    bandwidth: str | None = None


class LabReorderPolicyConfig(GatherlinkBaseModel):
    """Python-owned reorder policy for one node pair."""

    node_pair: str
    max_hold: str = DEFAULT_REORDER_MAX_HOLD


class LabScenarioConfig(GatherlinkBaseModel):
    """
    Declarative lab scenario config.

    The model intentionally accepts future scenario kinds and feature names now.
    Planning reports unsupported pieces as `not_implemented` so docs, configs,
    and future tests can evolve without silently pretending a feature exists.
    """

    schema_version: int = 1
    name: str
    scenario: LabScenarioKind
    description: str | None = None
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    runtime_dir: str = ".lab"
    nodes: list[LabNodeConfig] = Field(default_factory=list)
    paths: list[LabPathConfig] = Field(default_factory=list)
    traffic: LabTrafficConfig
    reorder_policies: list[LabReorderPolicyConfig] = Field(default_factory=list)
    profiles: dict[str, dict[str, LabShapeConfig]] = Field(default_factory=dict)
    network_modes: dict[str, LabNetworkModeConfig] = Field(default_factory=dict)
    future_features: list[str] = Field(default_factory=list)


class LabShapeTargetConfig(GatherlinkBaseModel):
    """One path and side selected by a standalone shaping config."""

    path: str
    side: LabShapeSide = "both"
    shape: LabShapeConfig = Field(default_factory=LabShapeConfig)
    clear: bool = False


class LabShapeProfileConfig(GatherlinkBaseModel):
    """Standalone live shaping config that can be applied to a running lab."""

    schema_version: int = 1
    name: str
    description: str | None = None
    targets: list[LabShapeTargetConfig] = Field(default_factory=list)


class LabNetworkModeConfig(GatherlinkBaseModel):
    """Named lab network behavior made from one or more shaping targets."""

    description: str | None = None
    targets: list[LabShapeTargetConfig] = Field(default_factory=list)


class LabPlanStep(GatherlinkBaseModel):
    """One planned lab action and whether it exists yet."""

    order: int
    action: str
    status: LabPlanStatus
    requires_root: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class LabPlan(GatherlinkBaseModel):
    """Inspectable plan for a lab scenario."""

    name: str
    scenario: LabScenarioKind
    supported: bool
    warnings: list[str] = Field(default_factory=list)
    steps: list[LabPlanStep] = Field(default_factory=list)


def load_lab_scenario_file(path: Path) -> LabScenarioConfig:
    """Load a lab scenario JSON file."""
    return LabScenarioConfig(**json.loads(path.read_text(encoding="utf-8")))


def load_lab_shape_profile_file(path: Path) -> LabShapeProfileConfig:
    """Load a standalone lab shaping profile JSON file."""
    return LabShapeProfileConfig(**json.loads(path.read_text(encoding="utf-8")))


def plan_lab_scenario(config: LabScenarioConfig) -> LabPlan:
    """Return a lab plan without mutating the host."""
    warnings = _security_warnings(config)
    steps = [
        LabPlanStep(
            order=10,
            action="validate_scenario_config",
            status="supported",
            details={"schema_version": config.schema_version, "runtime_dir": config.runtime_dir},
        ),
        LabPlanStep(
            order=20,
            action="render_topology",
            status="supported",
            details={
                "nodes": [node.export_dict() for node in config.nodes],
                "paths": [path.export_dict() for path in config.paths],
            },
        ),
        LabPlanStep(
            order=30,
            action="setup_network_namespaces_and_veths",
            status="supported",
            requires_root=True,
            details={"paths": [path.name for path in config.paths]},
        ),
        LabPlanStep(
            order=40,
            action="apply_tc_shaping",
            status="supported",
            requires_root=True,
            details={"shaped_paths": [path.name for path in config.paths if path.shape != LabShapeConfig()]},
        ),
        LabPlanStep(
            order=50,
            action="launch_lab_service_unprivileged",
            status="supported",
            details={"nodes": [node.name for node in config.nodes]},
        ),
        LabPlanStep(
            order=60,
            action="run_builtin_udp_traffic_check",
            status="supported",
            details=config.traffic.export_dict(),
        ),
        LabPlanStep(
            order=70,
            action="compile_reorder_policy",
            status="supported",
            details={
                "default_max_hold": DEFAULT_REORDER_MAX_HOLD,
                "default_minimum_hold": DEFAULT_REORDER_MIN_HOLD,
                "node_pairs": [policy.export_dict() for policy in config.reorder_policies],
            },
        ),
    ]
    steps.extend(_future_feature_steps(config, first_order=100))
    return LabPlan(
        name=config.name,
        scenario=config.scenario,
        supported=all(step.status == "supported" for step in steps),
        warnings=warnings,
        steps=steps,
    )


def _security_warnings(config: LabScenarioConfig) -> list[str]:
    """Return lab warnings owned by Python presentation logic."""
    if config.security.mode == "none":
        return [
            "WARNING: security.mode=none; lab traffic is unauthenticated and unencrypted.",
            "WARNING: Gatherlink processes must still run unprivileged.",
        ]
    return []


def _future_feature_steps(config: LabScenarioConfig, *, first_order: int) -> list[LabPlanStep]:
    """Represent requested future scenarios without pretending they are implemented."""
    steps: list[LabPlanStep] = []
    for offset, feature in enumerate(config.future_features):
        steps.append(
            LabPlanStep(
                order=first_order + offset,
                action=f"future_feature:{feature}",
                status="not_implemented",
                details={"feature": feature},
            )
        )
    if config.scenario not in {"local-dual-path", "local-multi-path"}:
        steps.append(
            LabPlanStep(
                order=first_order + len(config.future_features),
                action=f"scenario:{config.scenario}",
                status="not_implemented",
                details={"scenario": config.scenario},
            )
        )
    return steps
