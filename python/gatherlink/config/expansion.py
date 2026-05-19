"""
Expand minimal user config into explicit runtime config.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from gatherlink.config.models import GatherlinkConfig, ServiceConfig
from gatherlink.config.runtime import (
    RuntimeConfig,
    RuntimeDnsHelperConfig,
    RuntimePathConfig,
    RuntimeServiceConfig,
    RuntimeWireGuardHelperConfig,
)


def _service_by_name(services: list[ServiceConfig]) -> dict[str, ServiceConfig]:
    """Index services after validation has already guaranteed unique names."""
    return {service.name: service for service in services}


def _expand_paths(config: GatherlinkConfig) -> list[RuntimePathConfig]:
    """Copy declared physical paths into the runtime contract."""
    # TODO: Replace this direct copy with discovered interface facts once the
    # physical path validator knows how to inspect Debian network state. Keeping
    # the function now makes that later enrichment a local change.
    return [
        RuntimePathConfig(
            name=path.name,
            interface=path.interface,
            source_ip=path.source_ip,
            gateway=path.gateway,
        )
        for path in config.paths
    ]


def _expand_services(config: GatherlinkConfig) -> list[RuntimeServiceConfig]:
    """Copy services into runtime objects with the protocol made explicit."""
    return [
        RuntimeServiceConfig(
            name=service.name,
            target=service.target,
            listen=service.listen,
        )
        for service in config.services
    ]


def _expand_helpers(config: GatherlinkConfig) -> list[RuntimeWireGuardHelperConfig | RuntimeDnsHelperConfig]:
    """Expand optional helper blocks into ordered runtime helper records."""
    services = _service_by_name(config.services)
    helpers: list[RuntimeWireGuardHelperConfig | RuntimeDnsHelperConfig] = []

    if config.helpers.wireguard:
        service = services[config.helpers.wireguard.service]
        helpers.append(
            RuntimeWireGuardHelperConfig(
                enabled=config.helpers.wireguard.enabled,
                service=service.name,
                service_target=service.target,
                service_listen=service.listen,
            )
        )

    if config.helpers.dns:
        helpers.append(
            RuntimeDnsHelperConfig(
                enabled=config.helpers.dns.enabled,
                listen=config.helpers.dns.listen,
                strategy=config.helpers.dns.strategy,
            )
        )

    return helpers


def expand_config(config: GatherlinkConfig) -> RuntimeConfig:
    """Return the explicit runtime config for a validated user config."""
    return RuntimeConfig(
        schema_version=config.schema_version,
        node=config.node,
        role=config.role,
        peer=config.peer,
        paths=_expand_paths(config),
        services=_expand_services(config),
        helpers=_expand_helpers(config),
        metadata={
            # This metadata helps downstream commands distinguish runtime output
            # from the smaller user-authored JSON without changing the schema.
            "source_model": "GatherlinkConfig",
            "runtime_model": "RuntimeConfig",
        },
    )
