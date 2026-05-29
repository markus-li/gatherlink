# Troubleshooting

Start with these checks before reading design docs.

For deeper scenario diagnosis, use [`docs/operations/troubleshooting-guide.md`](../operations/troubleshooting-guide.md).
For event and counter meanings, use [`docs/operations/diagnostics-dictionary.md`](../operations/diagnostics-dictionary.md).

## Quick Doctor Check

Run the local readiness checker first when the failure is unclear:

```bash
gatherlink doctor --config node-a.json
```

For machine-readable output or diagnostics validation, use the schema-versioned
JSON envelope:

```bash
gatherlink doctor \
  --config node-a.json \
  --diagnostics-jsonl .gatherlink/services/core.node-a/diagnostics.jsonl \
  --json
```

Doctor output is redacted. It checks local config expansion, the service
registry, diagnostics JSONL shape, state paths, package-version agreement,
tracked release hygiene, optional operator tools, and whether the Rust dataplane
binding is importable. For QUIC DATAGRAM and HTTP/3 DATAGRAM paths, doctor also
reports that Python carrier supervision is required before Rust sees a local UDP
endpoint, plus the effective datagram MTU it can infer from config.

## Check Persisted State

If identity, trust-root, signed-bundle, or sealed-secret files may be wrong,
run the redacted state audit:

```bash
gatherlink secrets state-audit --state-dir .gatherlink/state
```

Private identity and sealed-secret files must be owner-only. Corrupt runtime
hints are warnings unless `--strict-hints` is used.

## Service Is Not Running

1. List known services:

```bash
gatherlink services list
```

2. Check one service:

```bash
gatherlink services status core.node-a
```

3. Read logs:

```bash
gatherlink services logs core.node-a --tail 100
```

## Packets Are Not Moving

1. Validate both configs:

```bash
gatherlink config validate node-a.json
gatherlink config validate node-b.json
```

2. Check the runtime plan:

```bash
gatherlink run plan node-a.json
```

3. Watch counters once:

```bash
gatherlink services monitor core.node-a core.node-b --once
```

Look for transmitted packets on one side and received packets on the other.
Expected duplicate counters may increase when fanout sends more than one
encrypted copy. That is normal when one copy is delivered and later copies are
discarded.

Remote service rows come from two steps. Discovery metadata is sparse and may
only prove that a remote service exists. Live remote counters require an
explicit temporary monitor/status request. If that request has expired, the
remote row should show stale or unknown until the monitor refreshes it.

## DNS Is Not Resolving

Direct DNS upstream mode and Gatherlink-tunnel upstream mode are available now.
For tunnel mode, point `--tunnel-upstream` at the local Gatherlink UDP service
listen endpoint that carries DNS to the peer.

```bash
gatherlink helpers dns-serve \
  --listen 127.0.0.1:5353 \
  --upstream 1.1.1.1:53 \
  --diagnostics-jsonl .gatherlink/dns.jsonl
```

Tunnel example:

```bash
gatherlink helpers dns-serve \
  --listen 127.0.0.1:5353 \
  --tunnel-upstream peer-dns=127.0.0.1:55153,timeout=1 \
  --diagnostics-jsonl .gatherlink/dns.jsonl
```

Check for `dns.policy_denied`, `dns.upstream_failed`, and `dns.dnssec_bogus`.

## Helper Is Denying Traffic

SOCKS5 and stream exits require allow lists. Check that the target host and port
match exactly:

```bash
--allow-host example.com --allow-port 443
```

For helper exit diagnostics, write JSONL:

```bash
gatherlink helpers stream-exit \
  --listen 127.0.0.1:7000 \
  --allow-host example.com \
  --allow-port 443 \
  --diagnostics-jsonl .gatherlink/helper.jsonl
```

## Stop And Restart Cleanly

```bash
gatherlink services close core.node-a
gatherlink services close core.node-b
gatherlink run start node-a.json --name core.node-a
gatherlink run start node-b.json --name core.node-b
```

## Reporting Bugs, Docs Issues, Or Performance Results

Gatherlink is currently tested on Debian. It should work in most Linux
environments, but there will be system-specific bugs. Use the
[GitHub issue chooser](https://github.com/markus-li/gatherlink/issues/new/choose)
for bugs, docs problems, performance results, regressions, scheduler
comparisons, and lab or VM reports. Include:

- operating system and version
- Gatherlink commit
- command you ran
- config with secrets removed
- relevant `gatherlink services logs` output
- relevant `gatherlink services monitor --once` output
- for performance reports: topology, path speeds, MTU, scheduler, traffic
  shape, test duration, drops/retransmits, and the WireGuard or raw Gatherlink
  baseline you compared against
