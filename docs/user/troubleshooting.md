# Troubleshooting

Start with these checks before reading design docs.

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

## Reporting Bugs

Gatherlink is currently tested on Debian. It should work in most Linux
environments, but there will be system-specific bugs. Please report bugs as
GitHub issues and include:

- operating system and version
- Gatherlink commit
- command you ran
- config with secrets removed
- relevant `gatherlink services logs` output
- relevant `gatherlink services monitor --once` output
