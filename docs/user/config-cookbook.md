# Config Cookbook

These are small starting points for common v0.9 setups. Use the example configs as
the source files, then change addresses, names, service targets, and keys for
your site.

## Two-Node Encrypted UDP Service

Start from:

```text
configs/examples/minimal-client.json
configs/examples/minimal-server.json
```

Use when one local UDP service should cross Gatherlink to one remote UDP target.

Steps:

1. Put one config on each node.
2. Set each node's local carrier bind address.
3. Set each peer's remote carrier endpoint.
4. Set the service listen address on the source.
5. Set the service target address on the sink.
6. Use authenticated provisioning for normal secure use.
7. Validate both configs.

```bash
gatherlink config validate minimal-client.json
gatherlink config validate minimal-server.json
```

## SOCKS5 Through Gatherlink

Start from:

```text
configs/examples/socks5-helper.json
```

Use when local applications can point at a SOCKS5 proxy.

Important fields:

- client helper listen address, usually `127.0.0.1:1080`
- Gatherlink UDP service used by the helper
- exit helper listen address on the remote side
- narrow `allow_hosts` and `allow_ports`

Run shape:

```bash
gatherlink run start node-a.json --name core.node-a
gatherlink run helpers-start node-a.json
```

SOCKS5 supports TCP CONNECT for v0.9. UDP ASSOCIATE is deferred.

Local acceptance proof:

```bash
tools/socks5_gatherlink_acceptance.py --out .gatherlink/socks5-acceptance/local
```

The proof starts the SOCKS5 helper, a Gatherlink node pair, the companion stream
exit, and the status HTTP helper. It then fetches the status page through the
SOCKS5 proxy to prove that the TCP CONNECT bytes crossed Gatherlink and exited
on the peer side.

## TCP Forward Through Gatherlink

Start from:

```text
configs/examples/tcp-forward-helper.json
```

Use for one local TCP listener forwarding to one target through Gatherlink, for
example a local port that reaches a web server on the other side.

Keep it one-to-one:

- one local listen endpoint
- one remote target endpoint
- explicit timeout values
- diagnostics enabled when process-managed

## DNS Direct Resolver

Start from:

```text
configs/examples/dns-helper.json
```

Use when Gatherlink should expose a local DNS listener backed by normal direct
upstreams.

Current direct CLI smoke:

```bash
gatherlink helpers dns-serve \
  --listen 127.0.0.1:5353 \
  --upstream 1.1.1.1:53 \
  --dnssec-mode allow_unsigned
```

DNS uses `dnspython`, IDNA-aware names, cache behavior, serve-stale behavior, and
explicit DNSSEC policy.

## DNS Tunnel Resolver

Use this when the DNS helper should send DNS wire queries through a configured
Gatherlink UDP service instead of directly to the upstream from the local node.

- local DNS listener receives client queries
- DNS helper selects a tunnel upstream by policy
- helper sends the DNS packet to a local Gatherlink service listen endpoint
- Gatherlink carries it to the peer's explicit DNS target and returns the DNS
  response over the same service mapping
- no plaintext routing labels are added

The tunnel upstream endpoint is usually `127.0.0.1:<service-listen-port>` on the
node running the DNS helper.

Current tunnel CLI smoke shape:

```bash
gatherlink helpers dns-serve \
  --listen 127.0.0.1:5353 \
  --tunnel-upstream peer-dns=127.0.0.1:55153,timeout=1 \
  --dnssec-mode allow_unsigned
```

The v0.9 release report should still prove this in the VM environment before
tagging.

## WireGuard Over Gatherlink

Start from:

```text
configs/examples/wireguard-client.json
configs/examples/wireguard-server.json
```

Use when WireGuard should own the VPN interface while Gatherlink carries the UDP
transport underneath it.

Plan first:

```bash
gatherlink helpers wireguard-plan configs/examples/wireguard-client.json
```

Then point the WireGuard peer `Endpoint` at the local Gatherlink service address
shown by the plan. WireGuard still owns keys, interface lifecycle, routes, and
firewall rules.

On a server-like WireGuard node, use `return_mode: "peer-scoped-source"` on the
Gatherlink WireGuard service when several authenticated Gatherlink peers share
the same sink carrier port. This preserves WireGuard's endpoint behavior without
making Gatherlink inspect or implement WireGuard packets.

## Shared Sink Service

Start from:

```text
configs/examples/shared-sink-server.json
```

Use this shape when several authenticated source nodes should use the same sink
UDP carrier port per path. Each session needs a unique `local_receiver_index`,
and the session `services` list maps that authenticated peer to the user service
it may reach. For server-like UDP helpers, set the service to
`return_mode: "peer-scoped-source"` so replies from the local target return to
the authenticated peer that produced the app-facing UDP source socket.

`gatherlink doctor --config CONFIG` reports a warning if one service is mapped
to several sessions without `peer-scoped-source`.

## Untrusted Relay Scenario

Use relay docs before implementing or deploying this shape:

```text
docs/protocol/relay-session-lifecycle.md
docs/protocol/relay-trust-model.md
```

Rules:

- relay peers are untrusted
- relay forwarding uses receiver/session context
- invalid relay packets silently drop
- relays do not decrypt endpoint payloads
- relays do not route by plaintext service labels
- routing uses authenticated relay-hop/session state

## Config Safety Checklist

Before using any cookbook shape:

```bash
gatherlink config validate CONFIG.json
gatherlink config show --runtime CONFIG.json
gatherlink doctor --config CONFIG.json
```

Check that secret-looking fields are redacted in operator output.
