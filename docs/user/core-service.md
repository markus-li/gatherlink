# Core Service

Use this when you want Gatherlink to carry UDP packets between two nodes.

## Start

1. Put a config on each node.
2. Validate each config:

```bash
gatherlink config validate node-a.json
gatherlink config validate node-b.json
```

3. Review the startup plan:

```bash
gatherlink run plan node-a.json
```

4. Start the receiver side first:

```bash
gatherlink run start node-b.json --name core.node-b --scheduler-reapply-interval 5
```

5. Start the sender side:

```bash
gatherlink run start node-a.json --name core.node-a --scheduler-reapply-interval 5
```

## Check It

```bash
gatherlink services list
gatherlink services status core.node-a
gatherlink services monitor core.node-a core.node-b --once
```

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
