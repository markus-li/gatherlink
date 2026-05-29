"""
WireGuard-over-Gatherlink setup generation.

This module is intentionally Python/control-plane code. It helps operators
choose paths, render Gatherlink configs, and render WireGuard config skeletons.
WireGuard still owns its own protocol, keys, interfaces, routes, and firewall
policy; Rust still only receives validated Gatherlink runtime facts.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gatherlink.config.validation import validate_config_dict
from gatherlink.helpers.traffic_split import TrafficSplitPlan, render_commands
from gatherlink.platform.debian import DebianCompatibilityBackend, default_debian_backend

WireGuardSetupModel = Literal["split", "single"]
InterfaceRole = Literal["wan", "lan", "management", "ignore"]
SecuritySetupMode = Literal["static", "none"]


@dataclass(frozen=True)
class InterfaceChoice:
    """One local interface and the role selected by the operator."""

    name: str
    role: InterfaceRole


@dataclass(frozen=True)
class WireGuardSetupPath:
    """One Gatherlink carrier path in the generated two-node setup."""

    name: str
    interface: str
    client_bind: str
    client_remote: str
    server_bind: str
    server_remote: str
    mtu: int = 1200
    tx_capacity_bps: int | None = None
    rx_capacity_bps: int | None = None

    def client_config(self) -> dict[str, object]:
        """Return the path as it appears in the source/client config."""
        return _path_config(
            name=self.name,
            interface=self.interface,
            transport_bind=self.client_bind,
            transport_remote=self.client_remote,
            mtu=self.mtu,
            tx_capacity_bps=self.tx_capacity_bps,
            rx_capacity_bps=self.rx_capacity_bps,
        )

    def server_config(self) -> dict[str, object]:
        """Return the path as it appears in the sink/server config."""
        return _path_config(
            name=self.name,
            interface=self.interface,
            transport_bind=self.server_bind,
            transport_remote=self.server_remote,
            mtu=self.mtu,
            tx_capacity_bps=self.rx_capacity_bps,
            rx_capacity_bps=self.tx_capacity_bps,
        )


@dataclass(frozen=True)
class WireGuardSetupRequest:
    """Operator choices for generated WireGuard-over-Gatherlink files."""

    model: WireGuardSetupModel = "split"
    client_node: str = "node-a"
    server_node: str = "node-b"
    paths: list[WireGuardSetupPath] = field(default_factory=list)
    security: SecuritySetupMode = "static"
    local_only: bool = False
    wg_client_interface: str = "wg-gl"
    wg_server_interface: str = "wg-gl"
    wg_stable_client_interface: str = "wg-gl-stable"
    wg_stable_server_interface: str = "wg-gl-stable"
    wg_fast_client_interface: str = "wg-gl-fast"
    wg_fast_server_interface: str = "wg-gl-fast"
    wg_client_address: str = "10.90.0.2/24"
    wg_server_address: str = "10.90.0.1/24"
    wg_stable_client_address: str = "10.90.10.2/24"
    wg_stable_server_address: str = "10.90.10.1/24"
    wg_fast_client_address: str = "10.90.20.2/24"
    wg_fast_server_address: str = "10.90.20.1/24"
    allowed_ips_client: str = "10.90.0.0/24"
    allowed_ips_server: str = "10.90.0.2/32"
    allowed_ips_stable_client: str = "10.90.10.0/24"
    allowed_ips_stable_server: str = "10.90.10.2/32"
    allowed_ips_fast_client: str = "10.90.20.0/24"
    allowed_ips_fast_server: str = "10.90.20.2/32"
    client_wg_listen_port: int = 51820
    server_wg_listen_port: int = 51820
    stable_client_wg_listen_port: int = 51820
    stable_server_wg_listen_port: int = 51820
    fast_client_wg_listen_port: int = 51821
    fast_server_wg_listen_port: int = 51821
    client_service_host: str = "127.0.0.1"
    server_service_host: str = "127.0.0.1"
    stable_client_service_port: int = 55180
    fast_client_service_port: int = 55181
    single_client_service_port: int = 55180

    def normalized_paths(self) -> list[WireGuardSetupPath]:
        """Return explicit paths, or a conservative local two-path default."""
        if self.paths:
            return self.paths
        return default_local_paths(2)


@dataclass(frozen=True)
class GeneratedWireGuardSetup:
    """All files and operator notes produced by the setup helper."""

    request: WireGuardSetupRequest
    files: dict[str, str]
    warnings: list[str]
    next_steps: list[str]

    def write(self, output_dir: Path, *, force: bool = False) -> list[Path]:
        """Write generated files and return their paths."""
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for relative_name, content in self.files.items():
            path = output_dir / relative_name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not force:
                raise FileExistsError(f"{path} already exists; pass --force to overwrite generated files")
            path.write_text(content, encoding="utf-8")
            written.append(path)
        return written


def discover_network_interfaces(
    *, backend: DebianCompatibilityBackend | None = None
) -> list[InterfaceChoice]:
    """Discover Debian interfaces for the interactive wizard."""
    backend = backend or default_debian_backend()
    result = backend.ip(["-o", "link", "show"], check=False)
    if result.returncode != 0:
        return []
    interfaces: list[InterfaceChoice] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        name = parts[1].strip().split("@", 1)[0]
        if name == "lo":
            continue
        interfaces.append(InterfaceChoice(name=name, role="ignore"))
    return interfaces


def default_local_paths(count: int) -> list[WireGuardSetupPath]:
    """Return localhost paths suitable for first-run smoke and docs examples."""
    if count < 1:
        raise ValueError("path count must be at least 1")
    return [
        WireGuardSetupPath(
            name=f"path-{chr(ord('a') + index)}",
            interface="lo",
            client_bind=f"127.0.0.1:{56001 + index}",
            client_remote=f"127.0.0.1:{57001 + index}",
            server_bind=f"127.0.0.1:{57001 + index}",
            server_remote=f"127.0.0.1:{56001 + index}",
            tx_capacity_bps=100_000_000,
            rx_capacity_bps=100_000_000,
        )
        for index in range(count)
    ]


def parse_setup_path(value: str) -> WireGuardSetupPath:
    """
    Parse a CLI path descriptor.

    Format:
    ``name=iface,client_bind=HOST:PORT,server_bind=HOST:PORT[,mtu=1200][,tx=...][,rx=...]``.
    Remote endpoints are derived from the opposite bind unless explicitly set.
    """
    if "=" not in value:
        raise ValueError("path must start with name=interface")
    first, *rest = value.split(",")
    name, interface = first.split("=", 1)
    values: dict[str, str] = {}
    for item in rest:
        if "=" not in item:
            raise ValueError(f"path option {item!r} must be key=value")
        key, option_value = item.split("=", 1)
        values[key.strip()] = option_value.strip()
    client_bind = values.get("client_bind")
    server_bind = values.get("server_bind")
    if not client_bind or not server_bind:
        raise ValueError("path requires client_bind and server_bind")
    return WireGuardSetupPath(
        name=name.strip(),
        interface=interface.strip(),
        client_bind=client_bind,
        server_bind=server_bind,
        client_remote=values.get("client_remote", server_bind),
        server_remote=values.get("server_remote", client_bind),
        mtu=int(values.get("mtu", "1200")),
        tx_capacity_bps=_optional_int(values.get("tx")),
        rx_capacity_bps=_optional_int(values.get("rx")),
    )


def generate_wireguard_setup(request: WireGuardSetupRequest) -> GeneratedWireGuardSetup:
    """Generate Gatherlink configs, WireGuard skeletons, and operator commands."""
    paths = request.normalized_paths()
    client_config, server_config = _gatherlink_configs(request, paths)
    validate_config_dict(client_config)
    validate_config_dict(server_config)

    files = {
        "gatherlink-client.json": _json(client_config),
        "gatherlink-server.json": _json(server_config),
        "README.md": _readme(request),
    }
    files.update(_wireguard_configs(request))
    if request.model == "split":
        plan = TrafficSplitPlan(stable_interface=request.wg_stable_client_interface, fast_interface=request.wg_fast_client_interface)
        files["traffic-split-plan.sh"] = "#!/usr/bin/env bash\nset -euo pipefail\n\n" + render_commands(plan.apply_commands()) + "\n"
    warnings = [
        "WireGuard keys, interfaces, routes, and firewall policy remain WireGuard/operator-owned.",
        "Review generated files before use; placeholder WireGuard keys must be replaced.",
    ]
    if request.security == "static":
        warnings.append("Generated Gatherlink static AEAD keys are for lab/manual setup; use authenticated provisioning for production.")
    if request.security == "none":
        warnings.append("Generated Gatherlink config has security.mode=none; use only for localhost labs.")
    next_steps = [
        "gatherlink config validate gatherlink-server.json",
        "gatherlink config validate gatherlink-client.json",
        "gatherlink doctor --config gatherlink-server.json",
        "gatherlink doctor --config gatherlink-client.json",
        "gatherlink helpers wireguard-plan gatherlink-client.json",
        "start the Gatherlink server before the client",
        "start WireGuard with your normal wg/wg-quick tooling",
        "send a ping or curl over the WireGuard interface after WireGuard is up",
        "check gatherlink services status core.wg-server core.wg-client",
        "watch gatherlink services monitor core.wg-server core.wg-client",
        "stop with gatherlink services close core.wg-client core.wg-server",
    ]
    return GeneratedWireGuardSetup(request=request, files=files, warnings=warnings, next_steps=next_steps)


def render_setup_summary(setup: GeneratedWireGuardSetup) -> str:
    """Render a concise terminal summary after generation."""
    lines = [
        "WireGuard-over-Gatherlink setup generated",
        f"model: {setup.request.model}",
        f"paths: {len(setup.request.normalized_paths())}",
        "files:",
    ]
    lines.extend(f"  - {name}" for name in sorted(setup.files))
    lines.append("warnings:")
    lines.extend(f"  - {warning}" for warning in setup.warnings)
    lines.append("next:")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(setup.next_steps, start=1))
    return "\n".join(lines)


def _gatherlink_configs(
    request: WireGuardSetupRequest, paths: list[WireGuardSetupPath]
) -> tuple[dict[str, object], dict[str, object]]:
    service_client, service_server, helpers = _service_configs(request)
    client_security, server_security = _security_pair(request)
    client = {
        "schema_version": 1,
        "node": request.client_node,
        "peer": request.server_node,
        "scheduler": {"mode": "coordinated_adaptive", "traffic_bias": "tcp" if request.model == "split" else "auto"},
        "paths": [path.client_config() for path in paths],
        "services": service_client,
        "helpers": {"wireguard": helpers},
        "security": client_security,
    }
    server = {
        "schema_version": 1,
        "node": request.server_node,
        "role": "server",
        "peer": request.client_node,
        "scheduler": {"mode": "coordinated_adaptive", "traffic_bias": "tcp" if request.model == "split" else "auto"},
        "paths": [path.server_config() for path in paths],
        "services": service_server,
        "security": server_security,
    }
    return client, server


def _service_configs(
    request: WireGuardSetupRequest,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    if request.model == "single":
        service = "wireguard-main"
        client_service = {
            "name": service,
            "listen": f"{request.client_service_host}:{request.single_client_service_port}",
            "target": f"{request.server_service_host}:{request.server_wg_listen_port}",
            "priority": "high",
            "traffic_class": "tcp_ordered",
        }
        server_service = {
            "name": service,
            "target": f"{request.server_service_host}:{request.server_wg_listen_port}",
            "return_mode": "peer-scoped-source",
            "priority": "high",
            "traffic_class": "tcp_ordered",
        }
        return [client_service], [server_service], {"enabled": True, "service": service}

    stable = "wireguard-stable"
    fast = "wireguard-fast"
    client_services = [
        {
            "name": stable,
            "listen": f"{request.client_service_host}:{request.stable_client_service_port}",
            "target": f"{request.server_service_host}:{request.stable_server_wg_listen_port}",
            "priority": "high",
            "traffic_class": "tcp_ordered",
            "scheduler_path_policy": "single_best_path",
            "scheduler_poll_batch_packets": 128,
        },
        {
            "name": fast,
            "listen": f"{request.client_service_host}:{request.fast_client_service_port}",
            "target": f"{request.server_service_host}:{request.fast_server_wg_listen_port}",
            "priority": "bulk",
            "traffic_class": "udp_bulk",
            "scheduler_path_policy": "weighted_round_robin",
        },
    ]
    server_services = [
        {
            "name": stable,
            "target": f"{request.server_service_host}:{request.stable_server_wg_listen_port}",
            "return_mode": "peer-scoped-source",
            "priority": "high",
            "traffic_class": "tcp_ordered",
            "scheduler_path_policy": "single_best_path",
            "scheduler_poll_batch_packets": 128,
        },
        {
            "name": fast,
            "target": f"{request.server_service_host}:{request.fast_server_wg_listen_port}",
            "return_mode": "peer-scoped-source",
            "priority": "bulk",
            "traffic_class": "udp_bulk",
            "scheduler_path_policy": "weighted_round_robin",
        },
    ]
    return client_services, server_services, {
        "enabled": True,
        "mode": "dual_profile",
        "stable_service": stable,
        "fast_service": fast,
    }


def _wireguard_configs(request: WireGuardSetupRequest) -> dict[str, str]:
    if request.model == "single":
        return {
            "wireguard-client.conf": _wg_config(
                interface_address=request.wg_client_address,
                listen_port=request.client_wg_listen_port,
                peer_endpoint=f"{request.client_service_host}:{request.single_client_service_port}",
                peer_allowed_ips=request.allowed_ips_client,
                role_note="client single profile",
            ),
            "wireguard-server.conf": _wg_config(
                interface_address=request.wg_server_address,
                listen_port=request.server_wg_listen_port,
                peer_endpoint=f"{request.client_service_host}:{request.single_client_service_port}",
                peer_allowed_ips=request.allowed_ips_server,
                role_note="server single profile",
            ),
        }
    return {
        "wireguard-stable-client.conf": _wg_config(
            interface_address=request.wg_stable_client_address,
            listen_port=request.stable_client_wg_listen_port,
            peer_endpoint=f"{request.client_service_host}:{request.stable_client_service_port}",
            peer_allowed_ips=request.allowed_ips_stable_client,
            role_note="client stable/TCP profile",
        ),
        "wireguard-stable-server.conf": _wg_config(
            interface_address=request.wg_stable_server_address,
            listen_port=request.stable_server_wg_listen_port,
            peer_endpoint=f"{request.client_service_host}:{request.stable_client_service_port}",
            peer_allowed_ips=request.allowed_ips_stable_server,
            role_note="server stable/TCP profile",
        ),
        "wireguard-fast-client.conf": _wg_config(
            interface_address=request.wg_fast_client_address,
            listen_port=request.fast_client_wg_listen_port,
            peer_endpoint=f"{request.client_service_host}:{request.fast_client_service_port}",
            peer_allowed_ips=request.allowed_ips_fast_client,
            role_note="client fast/UDP profile",
        ),
        "wireguard-fast-server.conf": _wg_config(
            interface_address=request.wg_fast_server_address,
            listen_port=request.fast_server_wg_listen_port,
            peer_endpoint=f"{request.client_service_host}:{request.fast_client_service_port}",
            peer_allowed_ips=request.allowed_ips_fast_server,
            role_note="server fast/UDP profile",
        ),
    }


def _wg_config(*, interface_address: str, listen_port: int, peer_endpoint: str, peer_allowed_ips: str, role_note: str) -> str:
    return "\n".join(
        [
            f"# Gatherlink generated WireGuard skeleton: {role_note}",
            "# Replace private/public keys with WireGuard-owned key material.",
            "[Interface]",
            "PrivateKey = <replace-with-wireguard-private-key>",
            f"Address = {interface_address}",
            f"ListenPort = {listen_port}",
            "MTU = 1380",
            "",
            "[Peer]",
            "PublicKey = <replace-with-peer-public-key>",
            f"Endpoint = {peer_endpoint}",
            f"AllowedIPs = {peer_allowed_ips}",
            "PersistentKeepalive = 25",
            "",
        ]
    )


def _security_pair(request: WireGuardSetupRequest) -> tuple[dict[str, object], dict[str, object]]:
    if request.security == "none":
        return {"mode": "none"}, {"mode": "none"}
    send_key = _random_key()
    receive_key = _random_key()
    return (
        {"mode": "static", "receiver_index": 101, "send_key": send_key, "receive_key": receive_key},
        {"mode": "static", "receiver_index": 101, "send_key": receive_key, "receive_key": send_key},
    )


def _path_config(
    *,
    name: str,
    interface: str,
    transport_bind: str,
    transport_remote: str,
    mtu: int,
    tx_capacity_bps: int | None,
    rx_capacity_bps: int | None,
) -> dict[str, object]:
    scheduler: dict[str, object] = {"mtu": mtu}
    if tx_capacity_bps is not None:
        scheduler["tx_capacity_bps"] = tx_capacity_bps
    if rx_capacity_bps is not None:
        scheduler["rx_capacity_bps"] = rx_capacity_bps
    return {
        "name": name,
        "interface": interface,
        "transport_bind": transport_bind,
        "transport_remote": transport_remote,
        "scheduler": scheduler,
    }


def _readme(request: WireGuardSetupRequest) -> str:
    if request.model == "split":
        wg_files = "\n".join(
            [
                "- `wireguard-stable-client.conf` and `wireguard-stable-server.conf` for TCP/default traffic",
                "- `wireguard-fast-client.conf` and `wireguard-fast-server.conf` for UDP/high-throughput traffic",
                "- optional `traffic-split-plan.sh` for Debian nftables/policy-routing review",
            ]
        )
    else:
        wg_files = "- `wireguard-client.conf` and `wireguard-server.conf` for the single WireGuard tunnel"
    return f"""# Generated WireGuard-over-Gatherlink Setup

Model: `{request.model}`

Files:

- `gatherlink-client.json`
- `gatherlink-server.json`
{wg_files}

Start shape:

```bash
gatherlink config validate gatherlink-server.json
gatherlink config validate gatherlink-client.json
gatherlink doctor --config gatherlink-server.json
gatherlink doctor --config gatherlink-client.json
gatherlink helpers wireguard-plan gatherlink-client.json
gatherlink run start gatherlink-server.json --name core.wg-server --scheduler-reapply-interval 5
gatherlink run start gatherlink-client.json --name core.wg-client --scheduler-reapply-interval 5
gatherlink services status core.wg-server core.wg-client
gatherlink services monitor core.wg-server core.wg-client --once
```

WireGuard still owns keys, interfaces, routes, and firewall policy. Replace
placeholder WireGuard keys before running `wg-quick`.

Traffic check:

```bash
# After replacing WireGuard keys and starting WireGuard, send normal traffic
# through the WireGuard interface, for example ping or curl to a routed test host.
ping -c 3 <wireguard-reachable-address>
curl http://<wireguard-reachable-test-host>/
```

Cleanup:

```bash
gatherlink services close core.wg-client core.wg-server
```
"""


def _json(data: dict[str, object]) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def _random_key() -> str:
    import base64

    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None
