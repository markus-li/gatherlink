# WireGuard Helper

Use this when WireGuard should run over Gatherlink instead of directly over the
network.

Gatherlink does not replace WireGuard. WireGuard still owns keys, interfaces,
routes, firewall rules, and `wg` or `wg-quick` lifecycle.

## Plan The Mapping

1. Create or edit a Gatherlink config with a WireGuard helper section.
2. Ask Gatherlink what WireGuard should point at:

```bash
gatherlink helpers wireguard-plan configs/examples/wireguard-client.json
```

3. In the WireGuard peer config, point `Endpoint` at the local Gatherlink UDP
   service listen address shown by the plan.

For a WireGuard-server-style node where more than one Gatherlink peer reaches
the same local WireGuard listener, set that Gatherlink service to
`return_mode: "peer-scoped-source"`. Gatherlink then gives WireGuard one
app-facing UDP source socket per authenticated peer, so WireGuard replies are
sent back through the right session while all peers can still share the same
sink carrier port.

## Run

1. Start the Gatherlink core service on both sides.
2. Start WireGuard with your normal WireGuard tooling:

```bash
sudo wg-quick up wg0
```

3. Check Gatherlink counters:

```bash
gatherlink services monitor core.node-a core.node-b --once
```

4. Check WireGuard the normal way:

```bash
sudo wg show
```

## Common Scenario

Use Gatherlink for unstable multi-path WAN transport, then let WireGuard provide
the VPN interface on top. Keep firewall and routing policy in your normal Linux
or WireGuard setup.

## MTU Tuning

WireGuard interface MTU is the main WireGuard-side knob that affects
WireGuard-over-Gatherlink throughput. It changes the packet shape that
Gatherlink sees; it does not change Gatherlink routing, security, or service
mapping.

Start with MTU `1380` on normal 1500-byte underlay paths. If the path set is
lossy or jittery, test MTU `1280`. For UDP-first testing on very uneven mobile
or satellite-style profiles, MTU `1200` can be worth testing too. Avoid assuming
MTU `1420` is better just because it is larger; in the Hyper-V real-world
facsimiles it hurt TCP-like WireGuard traffic badly.

`PersistentKeepalive` is still useful for NAT/liveness behavior, but it is not a
throughput tuning knob. Keep it only where the normal WireGuard deployment would
need it.

For current performance status, known struggles, and benchmark interpretation,
see `docs/benchmarks/wireguard-over-gatherlink-status.md`.
