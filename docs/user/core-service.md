# Core Service

Use this when you want Gatherlink to carry UDP packets between two nodes.

## Start

1. Put a config on each node.
2. Validate each config:

```bash
gatherlink config validate node-a.json
gatherlink config validate node-b.json
```

3. Run a local readiness check:

```bash
gatherlink doctor --config node-a.json
gatherlink doctor --config node-b.json
```

4. Review the startup plan:

```bash
gatherlink run plan node-a.json
```

5. Start the receiver side first:

```bash
gatherlink run start node-b.json --name core.node-b --scheduler-reapply-interval 5
```

6. Start the sender side:

```bash
gatherlink run start node-a.json --name core.node-a --scheduler-reapply-interval 5
```

## Authenticated Live Rekey

Authenticated configs produced by the Noise provisioning commands can run
without autonomous rekey inputs, but then they only execute the already
provisioned session. To let the foreground service originate and accept live
rekey, pass the local identity, expected peer identity, signed topology, and
trust root together:

```bash
gatherlink run start node-a.json \
  --name core.node-a \
  --scheduler-reapply-interval 5 \
  --rekey-local-identity state/node-a.identity.json \
  --rekey-peer-identity state/node-b.public.json \
  --rekey-topology state/topology.signed.json \
  --rekey-trust-root state/trust-root.public.json
```

Python uses those files to validate topology, identity, receiver-index
direction, expiry, and rekey cadence. Rust only carries the reserved
auth/crypto payload bytes and later executes the compiled replacement AEAD
facts after Python hot-reapplies them.

## Check It

```bash
gatherlink services list
gatherlink services status core.node-a
gatherlink services monitor core.node-a core.node-b --once
```

Remote services may appear as learned read-only entries after discovery metadata
arrives. Monitoring a remote service asks the peer for temporary read-only
status; if the request expires, the remote row should show stale or unknown.

## Stop

```bash
gatherlink services close core.node-a
gatherlink services close core.node-b
```

## Common Scenario

For a simple WireGuard-style UDP service, node A listens locally and forwards to
node B's UDP target. Use the example configs as a starting point:

```bash
configs/examples/windows-two-node-a.json
configs/examples/windows-two-node-b.json
```

Change the bind, remote, listen, and target addresses to match your hosts.
