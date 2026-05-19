"""
Expand minimal user config into explicit runtime config.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from base64 import b64decode

from gatherlink.config.models import USER_SERVICE_ID_START, GatherlinkConfig, ServiceConfig
from gatherlink.config.runtime import (
    RuntimeConfig,
    RuntimeDnsHelperConfig,
    RuntimeDnsUpstreamConfig,
    RuntimePathConfig,
    RuntimePathRelayHopConfig,
    RuntimePathSchedulerConfig,
    RuntimeSecurityConfig,
    RuntimeSecuritySessionConfig,
    RuntimeServiceConfig,
    RuntimeSocks5HelperConfig,
    RuntimeTcpForwardHelperConfig,
    RuntimeWireGuardHelperConfig,
)
from gatherlink.scheduling.compiler import compile_scheduler, compile_service_priority


def _service_by_name(services: list[ServiceConfig]) -> dict[str, ServiceConfig]:
    """Index services after validation has already guaranteed unique names."""
    return {service.name: service for service in services}


def _expand_paths(
    config: GatherlinkConfig,
    scheduler_paths: list[RuntimePathSchedulerConfig],
    security: RuntimeSecurityConfig,
) -> list[RuntimePathConfig]:
    """Copy declared physical paths into the runtime contract."""
    # TODO(path-interface-discovery): Replace this direct copy with discovered interface facts once the
    # physical path validator knows how to inspect Debian network state. Keeping
    # the function now makes that later enrichment a local change.
    expanded: list[RuntimePathConfig] = []
    for index, path in enumerate(config.paths):
        scheduler = scheduler_paths[index]
        if security.packet_overhead:
            if scheduler.mtu <= security.packet_overhead:
                raise ValueError(
                    f"path {path.name} MTU {scheduler.mtu} cannot carry security overhead {security.packet_overhead}"
                )
            scheduler = scheduler.model_copy(update={"mtu": scheduler.mtu - security.packet_overhead})
        expanded.append(
            RuntimePathConfig(
                name=path.name,
                interface=path.interface,
                source_ip=path.source_ip,
                gateway=path.gateway,
                transport_bind=path.transport_bind,
                transport_remote=path.transport_remote,
                scheduler=scheduler,
                relay=(
                    RuntimePathRelayHopConfig(
                        relay_receiver_index=path.relay.relay_receiver_index,
                        send_key=b64decode(path.relay.send_key, validate=True),
                    )
                    if path.relay is not None
                    else None
                ),
            )
        )
    return expanded


def _expand_security(config: GatherlinkConfig, service_ids_by_name: dict[str, int]) -> RuntimeSecurityConfig:
    """Compile user-facing security config into runtime key bytes."""
    if config.security.mode == "none":
        return RuntimeSecurityConfig(mode="none", source_mode="none")
    if config.security.sessions:
        return RuntimeSecurityConfig(
            mode="static",
            source_mode=config.security.mode,
            sessions=[
                RuntimeSecuritySessionConfig(
                    name=session.name,
                    local_receiver_index=session.local_receiver_index,
                    remote_receiver_index=session.remote_receiver_index,
                    send_key=b64decode(session.send_key, validate=True),
                    receive_key=b64decode(session.receive_key, validate=True),
                    service_ids=[service_ids_by_name[name] for name in session.services],
                )
                for session in config.security.sessions
            ],
        )
    if config.security.send_key is None or config.security.receive_key is None:
        raise ValueError(f"security.mode={config.security.mode} requires send_key and receive_key")
    runtime_mode = "static" if config.security.mode == "authenticated" else config.security.mode
    return RuntimeSecurityConfig(
        mode=runtime_mode,
        source_mode=config.security.mode,
        receiver_index=config.security.receiver_index,
        local_receiver_index=config.security.local_receiver_index or config.security.receiver_index,
        remote_receiver_index=config.security.remote_receiver_index or config.security.receiver_index,
        send_key=b64decode(config.security.send_key, validate=True),
        receive_key=b64decode(config.security.receive_key, validate=True),
    )


def _expand_services(config: GatherlinkConfig, service_ids: list[int]) -> list[RuntimeServiceConfig]:
    """Copy services into runtime objects with the protocol made explicit."""
    return [
        RuntimeServiceConfig(
            service_id=service_ids[index],
            service_id_explicit=service.service_id is not None,
            name=service.name,
            target=service.target,
            listen=service.listen,
            priority=service.priority,
            priority_value=compile_service_priority(service.priority),
            return_mode=service.return_mode,
            scheduler_fanout=service.scheduler_fanout,
            scheduler_fanout_below_bytes=service.scheduler_fanout_below_bytes,
        )
        for index, service in enumerate(config.services)
    ]


def _allocate_service_ids(services: list[ServiceConfig]) -> list[int]:
    """
    Assign deterministic user/application service ids while respecting explicit ids.

    The config validator rejects duplicate explicit ids and reserved ids. This
    allocator also skips explicit ids when filling automatic ids, so mixed
    explicit/implicit service lists never collide before they reach Rust.
    """
    explicit_ids = {service.service_id for service in services if service.service_id is not None}
    next_service_id = USER_SERVICE_ID_START
    assigned: list[int] = []
    for service in services:
        if service.service_id is not None:
            assigned.append(service.service_id)
            continue

        while next_service_id in explicit_ids:
            next_service_id += 1
        if next_service_id > 65535:
            raise ValueError("too many services for the u16 user service id range")
        assigned.append(next_service_id)
        next_service_id += 1
    return assigned


def _expand_helpers(
    config: GatherlinkConfig,
) -> list[
    RuntimeWireGuardHelperConfig | RuntimeDnsHelperConfig | RuntimeSocks5HelperConfig | RuntimeTcpForwardHelperConfig
]:
    """Expand optional helper blocks into ordered runtime helper records."""
    services = _service_by_name(config.services)
    helpers: list[
        RuntimeWireGuardHelperConfig
        | RuntimeDnsHelperConfig
        | RuntimeSocks5HelperConfig
        | RuntimeTcpForwardHelperConfig
    ] = []

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
                upstreams=[
                    RuntimeDnsUpstreamConfig(
                        name=upstream.name,
                        address=upstream.address,
                        port=upstream.port,
                        kind=upstream.kind,
                        timeout_seconds=upstream.timeout_seconds,
                    )
                    for upstream in config.helpers.dns.upstreams
                ],
            )
        )
    if config.helpers.socks5:
        service = services[config.helpers.socks5.service]
        helpers.append(
            RuntimeSocks5HelperConfig(
                enabled=config.helpers.socks5.enabled,
                service=config.helpers.socks5.service,
                service_target=service.target,
                service_listen=service.listen,
                listen=config.helpers.socks5.listen,
                allow_hosts=config.helpers.socks5.allow_hosts,
                allow_ports=config.helpers.socks5.allow_ports,
                connection_timeout_seconds=config.helpers.socks5.connection_timeout_seconds,
            )
        )
    if config.helpers.tcp_forward:
        service = services[config.helpers.tcp_forward.service]
        helpers.append(
            RuntimeTcpForwardHelperConfig(
                enabled=config.helpers.tcp_forward.enabled,
                service=config.helpers.tcp_forward.service,
                service_target=service.target,
                service_listen=service.listen,
                listen=config.helpers.tcp_forward.listen,
                target=config.helpers.tcp_forward.target,
                connect_timeout_seconds=config.helpers.tcp_forward.connect_timeout_seconds,
                idle_timeout_seconds=config.helpers.tcp_forward.idle_timeout_seconds,
            )
        )

    return helpers


def expand_config(config: GatherlinkConfig) -> RuntimeConfig:
    """Return the explicit runtime config for a validated user config."""
    scheduler = compile_scheduler(config)
    service_ids = _allocate_service_ids(config.services)
    service_ids_by_name = {service.name: service_ids[index] for index, service in enumerate(config.services)}
    security = _expand_security(config, service_ids_by_name)
    return RuntimeConfig(
        schema_version=config.schema_version,
        node=config.node,
        role=config.role,
        peer=config.peer,
        security=security,
        paths=_expand_paths(config, scheduler.paths, security),
        services=_expand_services(config, service_ids),
        scheduler=scheduler,
        helpers=_expand_helpers(config),
        metadata={
            # This metadata helps downstream commands distinguish runtime output
            # from the smaller user-authored JSON without changing the schema.
            "source_model": "GatherlinkConfig",
            "runtime_model": "RuntimeConfig",
        },
    )
