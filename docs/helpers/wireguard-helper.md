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

- `gatherlink helpers wireguard-setup` runs an installer-style setup wizard and
  can also render non-interactive setup files for tests/automation. It writes
  Gatherlink client/server configs, WireGuard config skeletons, an optional
  dual-profile traffic split plan, and a local README.
- `gatherlink helpers wireguard-plan configs/examples/wireguard-client.json`
  shows the Gatherlink service mapping that WireGuard should use
- `gatherlink helpers wireguard-plan
  configs/examples/wireguard-dual-profile-client.json` shows the advanced
  two-tunnel profile: a stable/default service for TCP-like traffic and a
  fast service for UDP/high-throughput traffic
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
- the optional traffic split helper can generate Debian policy-routing rules
  for the dual profile, but sites should prefer owning the reviewed final rules
  in their normal firewall tooling when possible
- `tools/hyperv/run_wireguard_vm_acceptance.sh` proves this contract in the
  two-Debian-VM lab by rendering the WireGuard plan, sending UDP payloads to the
  planned local Gatherlink endpoint, and verifying they exit at the peer-side
  WireGuard UDP target port
- `tools/hyperv/run_relay_wireguard_vm_acceptance.sh` proves the same boundary
  through a three-VM untrusted relay topology with real temporary WireGuard
  interfaces. The runner uses WireGuard's own `wg` tool and lab sudo for the
  interface setup, then curls an HTTP helper over WireGuard while Gatherlink
  carries the UDP endpoint packets through B -> C -> A relay-hop transport.
- `tools/hyperv/run_dual_wireguard_gatherlink_speed.sh` proves the advanced
  dual profile with two real temporary WireGuard interfaces over two
  Gatherlink UDP services. It uses the WireGuard helper plan, captures the
  traffic-split helper dry-run, then sends TCP-style iperf through the stable
  tunnel and UDP-style iperf through the fast tunnel.

Performance posture:

- Current WireGuard-over-Gatherlink performance status and known struggles live
  in [`docs/benchmarks/wireguard-over-gatherlink-status.md`](../benchmarks/wireguard-over-gatherlink-status.md). Keep exact
  benchmark rows in the benchmark docs, not in this helper contract.
- The advanced dual-WireGuard profile is intended for mixed traffic where one
  opaque WireGuard peer flow is not enough information for the scheduler:
  TCP/default traffic uses the stable profile, while UDP/high-throughput traffic
  uses the fast profile.
- The dual profile is not the default because it requires two WireGuard
  interfaces, two Gatherlink services, and local firewall or policy-routing
  decisions.
- WireGuard interface MTU remains WireGuard-owned, but the helper docs should
  mention it because it changes the UDP packet shape that Gatherlink carries.
- Use MTU `1380` as the normal starting point for 1500-byte underlays.
- Test MTU `1280` or `1200` on lossy/jittery mobile or satellite-style path
  sets.
- Avoid using a larger MTU such as `1420` as a blind performance tweak; current
  Hyper-V real-world facsimiles showed that it can hurt TCP-like WireGuard
  traffic over Gatherlink.
- `PersistentKeepalive` is a liveness/NAT setting, not a throughput setting.

Library posture:

- prefer WireGuard's own tooling, such as `wg`, `wg-quick`, platform network
  managers, or appliance APIs
- do not add a Python WireGuard protocol library
- Gatherlink should generate/coordinate config, not reimplement WireGuard

Userspace WireGuard comparison:

- evaluate Mullvad's GotaTun as an optional Rust userspace WireGuard backend
  for labs and advanced helpers
- compare it directly against `wireguard-go` before considering it for normal
  helper use
- keep kernel WireGuard and standard `wg`/`wg-quick` tooling as the normal
  operator path unless benchmark and reliability evidence justify another
  backend
- do not vendor, fork, or implement WireGuard protocol behavior in Gatherlink
  unless a later roadmap explicitly accepts the security and support burden
- document Linux privilege differences, especially capability and `fwmark`
  behavior, before exposing it as an operator choice

Not-yet scope:

- implementing WireGuard protocol behavior
- replacing `wg`, `wg-quick`, platform network managers, or appliance tooling
- silently taking over system firewall/routing policy without operator review
