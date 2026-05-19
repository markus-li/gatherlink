# WireGuard Helper

The WireGuard helper is an active helper priority.

It exists to make it easy to run VPNs using Gatherlink as the transport or
underlay. WireGuard-specific behavior, keys, interfaces, and tooling should
remain WireGuard-owned. Gatherlink should generate or coordinate the service
transport around WireGuard, not replace WireGuard tooling.

First scope:

- config generation or guidance for using WireGuard over Gatherlink services
- key/config material handling that respects WireGuard's own tooling
- diagnostics showing Gatherlink service mapping and expected UDP endpoints

Implemented first slice:

- `gatherlink helpers wireguard-plan configs/examples/wireguard-client.json`
  shows the Gatherlink service mapping that WireGuard should use
- the WireGuard peer `Endpoint` should point at the local Gatherlink service
  listen endpoint; Gatherlink then forwards that UDP service to the configured
  WireGuard listen endpoint on the far side
- key helpers delegate to the official `wg` tool for `genkey` and `pubkey`
- planning diagnostics report whether `wg` and `wg-quick` are available without
  invoking privileged interface operations
- Gatherlink does not parse or implement WireGuard packets, routes, firewall
  policy, or interface lifecycle

Library posture:

- prefer WireGuard's own tooling, such as `wg`, `wg-quick`, platform network
  managers, or appliance APIs
- do not add a Python WireGuard protocol library for MVP
- Gatherlink should generate/coordinate config, not reimplement WireGuard

Not-yet scope:

- implementing WireGuard protocol behavior
- replacing `wg`, `wg-quick`, platform network managers, or appliance tooling
- taking over system firewall/routing policy
