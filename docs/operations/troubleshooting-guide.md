# Troubleshooting Guide

Use this for scenario-based diagnosis. Start with `doctor`, then use status,
monitor, logs, and diagnostics JSONL.

## First Check

```bash
gatherlink doctor --config node-a.json
gatherlink services list
gatherlink services monitor core.node-a core.node-b --once
```

If `doctor` fails, fix that first. Packet debugging is usually noise until the
config, state layout, service registry, and Rust binding are healthy.

## Service Does Not Start

1. Validate config.

```bash
gatherlink config validate node-a.json
```

2. Render the startup plan.

```bash
gatherlink run plan node-a.json
```

3. Start with explicit diagnostics.

```bash
gatherlink run start node-a.json \
  --name core.node-a \
  --diagnostics-jsonl .gatherlink/services/core.node-a/diagnostics.jsonl
```

Check for `runtime.start_failed` in diagnostics and the service log.

## No Packets Arrive

1. Confirm both services are running.
2. Confirm source TX counters increase.
3. Confirm sink RX or delivered counters increase.
4. Check path socket bind and remote addresses in `gatherlink run plan`.
5. Check host firewall and UDP reachability between carrier endpoints.

If source TX increases but sink RX stays flat, suspect carrier path reachability.
If sink RX increases but delivery stays flat, suspect service target, replay,
crypto, or fragment handling.

## Crypto Or Session Drops

Silent drops are expected for invalid encrypted packets. The peer should not get
an error response.

Check local counters and diagnostics for:

- `crypto.auth_failed`
- `crypto.replay_drop`
- `crypto.unknown_receiver_index`
- `relay.auth_failed`
- `relay.replay_drop`
- `relay.unknown_receiver_index`

Common causes are mismatched receiver indexes, stale generation, wrong session
keys, expired relay authorization, or replayed packets.

## Helper Is Running But Traffic Fails

1. Check the helper service log.
2. Check helper diagnostics JSONL.
3. Confirm allow lists match the exact host and port.
4. Confirm the helper uses Gatherlink transport, not `--lab-direct`, outside
   local smoke tests.

Useful event codes:

- `helper.lifecycle.started`
- `helper.lifecycle.start_failed`
- `helper.stream.denied`
- `helper.stream.unreachable`
- `socks.exit_denied`
- `socks.exit_unreachable`

## DNS Does Not Resolve

Direct DNS upstreams and Gatherlink-tunnel DNS upstreams are implemented. If DNS
does not resolve, check the helper process first, then the local Gatherlink UDP
service endpoint it uses as its tunnel upstream. Historical VM proof belongs in
the release notes and lab evidence.

Check:

```bash
gatherlink helpers dns-serve \
  --listen 127.0.0.1:5353 \
  --upstream 1.1.1.1:53 \
  --diagnostics-jsonl .gatherlink/dns.jsonl
```

Look for:

- `dns.policy_denied`
- `dns.upstream_failed`
- `dns.dnssec_bogus`

If `--dnssec-mode require_ad` fails, confirm the upstream returns authenticated
data for the queried zone.

## SOCKS5 Connect Fails

1. Confirm SOCKS5 is TCP CONNECT only for now.
2. Check the exit allow list.
3. Check the client helper `--gatherlink-service` points at the local Gatherlink
   UDP service.
4. Check the companion stream exit is reachable through Gatherlink.

For local smoke only, `--lab-direct` can isolate SOCKS5 library behavior from
Gatherlink transport.

## WireGuard Does Not Handshake

1. Check Gatherlink core counters first.
2. Run the helper plan.

```bash
gatherlink helpers wireguard-plan configs/examples/wireguard-client.json
```

3. Confirm WireGuard `Endpoint` points at the local Gatherlink service shown by
   the plan.
4. Check WireGuard normally.

```bash
sudo wg show
```

Gatherlink does not own WireGuard keys, interfaces, routes, or firewall rules.

## Path Is Degraded

Use monitor and lab shaping facts before changing policy.

```bash
gatherlink services monitor core.node-a core.node-b
```

Look for rising send failures, missed packets, reorder-needed counters, or path
capacity/latency metadata. Expected duplicates can rise during fanout without
being a problem.

## When Reporting A Bug, Docs Issue, Or Performance Result

Open a GitHub issue through the
[issue chooser](https://github.com/markus-li/gatherlink/issues/new/choose). The
consolidated report form covers bugs, docs problems, performance results,
regressions, scheduler comparisons, and lab or VM reports. Include:

- Debian version or Linux distribution
- Gatherlink commit
- exact command
- redacted config
- `gatherlink doctor --json` output
- `gatherlink services monitor ... --once` output
- relevant service logs and diagnostics JSONL snippets
- for performance reports: topology, path speeds, MTU, scheduler, traffic
  shape, test duration, drops/retransmits, and the WireGuard or raw Gatherlink
  baseline you compared against
