"""
Expand minimal user config into explicit runtime config.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from base64 import b64decode

from gatherlink.config.models import (
    USER_SERVICE_ID_START,
    GatherlinkConfig,
    SchedulerPolicy,
    ServiceConfig,
    ServiceTrafficClass,
)
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
from gatherlink.scheduling.policies import (
    FLOWLET_ADAPTIVE_DEFAULT_IDLE_US,
    FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US,
    FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS,
)


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
                carrier=path.carrier,
                source_ip=path.source_ip,
                gateway=path.gateway,
                transport_bind=path.transport_bind,
                transport_remote=path.transport_remote,
                carrier_max_datagram_size=path.carrier_max_datagram_size,
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


def _expand_services(
    config: GatherlinkConfig,
    service_ids: list[int],
    *,
    effective_scheduler_mode: SchedulerPolicy | None = None,
) -> list[RuntimeServiceConfig]:
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
            traffic_class=_service_traffic_class(config, service),
            return_mode=service.return_mode,
            scheduler_poll_batch_packets=service.scheduler_poll_batch_packets,
            **compile_service_scheduler_primitives(
                config,
                service,
                effective_scheduler_mode=effective_scheduler_mode,
            ),
        )
        for index, service in enumerate(config.services)
    ]


def _service_traffic_class(config: GatherlinkConfig, service: ServiceConfig) -> ServiceTrafficClass:
    """
    Return the Python-owned service traffic class for scheduler policy.

    Explicit service config wins. Helper-derived values are defaults so
    WireGuard split profiles can benefit from class-aware scheduling without
    making Rust inspect or understand helper payloads.
    """
    if service.traffic_class != "unknown":
        return service.traffic_class
    wireguard = config.helpers.wireguard
    if wireguard is None or not wireguard.enabled:
        return "unknown"
    if wireguard.mode == "dual_profile":
        if service.name == wireguard.stable_service:
            return "tcp_ordered"
        if service.name == wireguard.fast_service:
            return "udp_bulk"
        return "unknown"
    if service.name == wireguard.service:
        return "tcp_ordered"
    return "unknown"


def compile_service_scheduler_primitives(
    config: GatherlinkConfig,
    service: ServiceConfig,
    *,
    effective_scheduler_mode: SchedulerPolicy | None = None,
) -> dict[str, object]:
    """
    Compile Python-owned service scheduling policy into Rust primitives.

    This helper is shared by initial config expansion and hot scheduler reapply.
    That matters for coordinated/adaptive scheduling: Python may promote the
    effective path policy to `flowlet_adaptive` after telemetry arrives, and the
    matching per-service flowlet primitives must change at the same boundary.
    """
    return {
        "scheduler_fanout": service.scheduler_fanout,
        "scheduler_fanout_below_bytes": service.scheduler_fanout_below_bytes,
        "scheduler_flowlet_idle_us": _service_flowlet_idle_us(
            config,
            service,
            effective_scheduler_mode=effective_scheduler_mode,
        ),
        "scheduler_flowlet_max_hold_us": _service_flowlet_max_hold_us(
            config,
            service,
            effective_scheduler_mode=effective_scheduler_mode,
        ),
        "scheduler_path_run_datagrams": _service_path_run_datagrams(
            config,
            service,
            effective_scheduler_mode=effective_scheduler_mode,
        ),
        "scheduler_path_policy": service.scheduler_path_policy,
        "scheduler_allowed_path_ids": _service_allowed_path_ids(config, service),
        "scheduler_path_weights": _service_path_weights(config, service),
    }


def _service_allowed_path_ids(config: GatherlinkConfig, service: ServiceConfig) -> list[int]:
    """Compile service path-name eligibility into compact runtime path ids."""
    if not service.scheduler_allowed_paths:
        return []
    path_ids_by_name = {path.name: index for index, path in enumerate(config.paths)}
    missing = [name for name in service.scheduler_allowed_paths if name not in path_ids_by_name]
    if missing:
        raise ValueError(f"service {service.name!r} references unknown scheduler_allowed_paths: {', '.join(missing)}")
    return [path_ids_by_name[name] for name in service.scheduler_allowed_paths]


def _service_path_weights(config: GatherlinkConfig, service: ServiceConfig) -> list[tuple[int, int]]:
    """Compile service-specific path weights into compact runtime path-id pairs."""
    if not service.scheduler_path_weights:
        return []
    path_ids_by_name = {path.name: index for index, path in enumerate(config.paths)}
    allowed_names = set(service.scheduler_allowed_paths)
    compiled: list[tuple[int, int]] = []
    for path_name, weight in service.scheduler_path_weights.items():
        if path_name not in path_ids_by_name:
            raise ValueError(f"service {service.name!r} references unknown scheduler_path_weights path: {path_name}")
        if allowed_names and path_name not in allowed_names:
            raise ValueError(
                f"service {service.name!r} scheduler_path_weights contains {path_name!r}, "
                "which is not in scheduler_allowed_paths"
            )
        if weight < 1 or weight > 65535:
            raise ValueError(
                f"service {service.name!r} scheduler_path_weights[{path_name!r}] must be in range 1..65535"
            )
        compiled.append((path_ids_by_name[path_name], int(weight)))
    return compiled


def _service_flowlet_idle_us(
    config: GatherlinkConfig,
    service: ServiceConfig,
    *,
    effective_scheduler_mode: SchedulerPolicy | None = None,
) -> int:
    """
    Compile flowlet policy into the service primitive Rust already executes.

    Explicit per-service config wins. The global `flowlet_adaptive` policy only
    fills in conservative defaults when the service did not opt into its own
    flowlet timing.
    """
    if service.scheduler_flowlet_idle_us:
        return service.scheduler_flowlet_idle_us
    if _service_effective_mode(config, effective_scheduler_mode) == "flowlet_adaptive":
        return FLOWLET_ADAPTIVE_DEFAULT_IDLE_US
    return 0


def _service_flowlet_max_hold_us(
    config: GatherlinkConfig,
    service: ServiceConfig,
    *,
    effective_scheduler_mode: SchedulerPolicy | None = None,
) -> int:
    """Return the compiled maximum hold for the flowlet service primitive."""
    if service.scheduler_flowlet_max_hold_us:
        return service.scheduler_flowlet_max_hold_us
    if _service_effective_mode(config, effective_scheduler_mode) == "flowlet_adaptive":
        return FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US
    return 0


def _service_path_run_datagrams(
    config: GatherlinkConfig,
    service: ServiceConfig,
    *,
    effective_scheduler_mode: SchedulerPolicy | None = None,
) -> int:
    """Return the compiled hot-burst path run bound for service scheduling."""
    if service.scheduler_path_run_datagrams:
        return service.scheduler_path_run_datagrams
    if _service_effective_mode(config, effective_scheduler_mode) == "flowlet_adaptive":
        return FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS
    return 0


def _service_effective_mode(
    config: GatherlinkConfig,
    effective_scheduler_mode: SchedulerPolicy | None,
) -> SchedulerPolicy:
    """
    Return the scheduler mode service primitives should mirror.

    Coordinated/adaptive is a Python meta-policy. Its current concrete decision
    is supplied by the scheduler coordinator during hot reapply; without that
    decision, initial expansion preserves the user-configured defaults.
    """
    return effective_scheduler_mode or config.scheduler.mode


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
        if config.helpers.wireguard.mode == "dual_profile":
            for traffic_class, service_name in (
                ("stable", config.helpers.wireguard.stable_service),
                ("fast", config.helpers.wireguard.fast_service),
            ):
                if service_name is None:
                    continue
                service = services[service_name]
                helpers.append(
                    RuntimeWireGuardHelperConfig(
                        enabled=config.helpers.wireguard.enabled,
                        profile="dual_profile",
                        traffic_class=traffic_class,
                        service=service.name,
                        service_target=service.target,
                        service_listen=service.listen,
                    )
                )
        else:
            service_name = config.helpers.wireguard.service
            if service_name is not None:
                service = services[service_name]
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
