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
- server-side WireGuard services that accept more than one authenticated
  Gatherlink peer should use `return_mode: "peer-scoped-source"` so WireGuard
  sees a distinct UDP source endpoint for each peer and replies map back to the
  correct authenticated session
- key helpers delegate to the official `wg` tool for `genkey` and `pubkey`
- planning diagnostics report whether `wg` and `wg-quick` are available without
  invoking privileged interface operations
- `wireguard-plan --diagnostics-jsonl ...` emits `helper.wireguard.plan` with
  service mapping and tool availability facts, but no WireGuard private keys or
  generated peer secrets
- Gatherlink does not parse or implement WireGuard packets, routes, firewall
  policy, or interface lifecycle
- `tools/hyperv/run_wireguard_vm_acceptance.sh` proves this contract in the
  two-Debian-VM lab by rendering the WireGuard plan, sending UDP payloads to the
  planned local Gatherlink endpoint, and verifying they exit at the peer-side
  WireGuard UDP target port
- `tools/hyperv/run_relay_wireguard_vm_acceptance.sh` proves the same boundary
  through a three-VM untrusted relay topology with real temporary WireGuard
  interfaces. The runner uses WireGuard's own `wg` tool and lab sudo for the
  interface setup, then curls an HTTP helper over WireGuard while Gatherlink
  carries the UDP endpoint packets through B -> C -> A relay-hop transport.

Library posture:

- prefer WireGuard's own tooling, such as `wg`, `wg-quick`, platform network
  managers, or appliance APIs
- do not add a Python WireGuard protocol library for v0.9
- Gatherlink should generate/coordinate config, not reimplement WireGuard

Not-yet scope:

- implementing WireGuard protocol behavior
- replacing `wg`, `wg-quick`, platform network managers, or appliance tooling
- taking over system firewall/routing policy
