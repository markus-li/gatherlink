"""
Build non-privileged core runtime startup plans.

The first Gatherlink test target is intentionally plain userland UDP traffic:
no TUN device, no firewall mutation, no policy routing, no helper-owned tunnels,
and no root permissions. This planner is for core transport only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from gatherlink.config.runtime import RuntimeConfig
from gatherlink.shared.models import GatherlinkBaseModel


class RuntimePlanStep(GatherlinkBaseModel):
    """One ordered action the supervisor will eventually execute or dry-run."""

    order: int
    component: str
    action: str
    mode: Literal["core-userland-udp", "core-dataplane"]
    requires_root: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimePlan(GatherlinkBaseModel):
    """An ordered, inspectable core startup plan derived from RuntimeConfig."""

    node: str
    role: str
    transport_target: Literal["core-userland-udp"] = "core-userland-udp"
    requires_root: bool = False
    helper_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    steps: list[RuntimePlanStep] = Field(default_factory=list)


def _plan_service_steps(config: RuntimeConfig, *, first_order: int) -> list[RuntimePlanStep]:
    """Plan core userland UDP service listeners and remote emitters."""
    steps: list[RuntimePlanStep] = []
    order = first_order
    for service in config.services:
        # The MVP path is a normal UDP socket path. Helpers may consume the same
        # runtime config later, but core planning never starts tunnels, DNS
        # helpers, route setup, or firewall setup.
        steps.append(
            RuntimePlanStep(
                order=order,
                component=f"core-service:{service.name}",
                action="bind_udp_listener" if service.listen else "register_udp_target",
                mode="core-userland-udp",
                details={
                    "listen": service.listen,
                    "target": service.target,
                    "protocol": service.protocol,
                    "priority": service.priority,
                    "priority_value": service.priority_value,
                },
            )
        )
        order += 10
    return steps


def build_runtime_plan(config: RuntimeConfig) -> RuntimePlan:
    """Build the core MVP startup plan without helper or privileged capabilities."""
    warnings = _runtime_warnings(config)
    steps = [
        RuntimePlanStep(
            order=10,
            component="core-supervisor",
            action="load_runtime_config",
            mode="core-userland-udp",
            details={
                "schema_version": config.schema_version,
                "security_mode": config.security.mode,
                "warnings": warnings,
                # This reminder is intentionally present in dry-run output so
                # helper-heavy configs do not imply that core owns tunneling.
                "helpers_ignored_by_core": len(config.helpers),
            },
        )
    ]
    steps.extend(_plan_service_steps(config, first_order=20))
    steps.append(
        RuntimePlanStep(
            order=1000,
            component="core-dataplane",
            action="start_userland_udp_transport",
            mode="core-dataplane",
            details={
                "paths": [path.export_dict() for path in config.paths],
                "scheduler": config.scheduler.export_dict(),
                "services": [service.name for service in config.services],
            },
        )
    )
    return RuntimePlan(
        node=config.node,
        role=config.role,
        helper_count=len(config.helpers),
        warnings=warnings,
        steps=sorted(steps, key=lambda step: step.order),
    )


def _runtime_warnings(config: RuntimeConfig) -> list[str]:
    """Return Python-owned operator warnings for runtime state."""
    if config.security.mode == "none":
        return [
            "WARNING: security.mode=none; traffic is unauthenticated and unencrypted.",
            "WARNING: use only in local labs or controlled debugging.",
        ]
    return []
