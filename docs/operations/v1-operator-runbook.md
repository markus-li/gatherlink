# V1 Operator Runbook

Use this when running Gatherlink on Debian for a lab or small site. Keep this
page practical: start, inspect, reload by restart when needed, and stop cleanly.

## Before Starting

1. Install Gatherlink in the active environment.

```bash
pip install -e .
```

2. Validate every node config.

```bash
gatherlink config validate node-a.json
gatherlink config validate node-b.json
```

3. Run local readiness checks.

```bash
gatherlink doctor --config node-a.json
gatherlink doctor --config node-b.json
```

4. Review the runtime plan before background launch.

```bash
gatherlink run plan node-a.json
```

## Start Core Services

Start the receiving side first, then the sending side.

```bash
gatherlink run start node-b.json --name core.node-b --scheduler-reapply-interval 5
gatherlink run start node-a.json --name core.node-a --scheduler-reapply-interval 5
```

If the config declares managed helpers, start them after the core service:

```bash
gatherlink run helpers-start node-a.json
```

## Check Health

List registered services:

```bash
gatherlink services list
```

Inspect one service:

```bash
gatherlink services status core.node-a
```

Watch both sides once:

```bash
gatherlink services monitor core.node-a core.node-b --once
```

Follow logs:

```bash
gatherlink services logs core.node-a --follow
```

## What Healthy Looks Like

- both services show `running`
- source-side transmit counters increase when traffic is sent
- sink-side receive/deliver counters increase
- expected duplicate counters may increase when fanout sends copies
- replay/auth/unknown receiver counters stay at zero during normal operation
- diagnostics JSONL contains lifecycle and counter facts, not repeated startup
  failures

Expected duplicate drops are not a failure. They mean multiple encrypted copies
arrived and only one was delivered.

## Config Changes

Runtime changes should be live-reloadable where the runner supports reapply.
During v1 operations, the safe fallback is:

1. Validate the changed config.
2. Stop the affected service.
3. Start it again with the same service name.
4. Check monitor counters.

```bash
gatherlink config validate node-a.json
gatherlink services close core.node-a
gatherlink run start node-a.json --name core.node-a --scheduler-reapply-interval 5
gatherlink services monitor core.node-a core.node-b --once
```

Endpoint IP and port changes must not be accepted through control context. Use
config and provisioning changes for endpoint movement.

## Stop Cleanly

```bash
gatherlink services close core.node-a
gatherlink services close core.node-b
```

Close helper services by their registered names if they were started separately.

## Daily Checks

Run these before calling a site healthy:

```bash
gatherlink doctor --config node-a.json
gatherlink services list
gatherlink services monitor core.node-a core.node-b --once
```

For state and secret layout:

```bash
gatherlink secrets state-audit --state-dir .gatherlink/state
```

## Rules Operators Should Remember

- Debian is the tested v1 platform.
- No plaintext routing is supported.
- There is no `route_id`.
- Static crypto is lab/manual fallback; authenticated sessions are the normal
  secure path.
- REST/status helper behavior is experimental and local by default.
- Report bugs as GitHub issues with redacted config, logs, and monitor output.
