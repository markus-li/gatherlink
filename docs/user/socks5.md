# SOCKS5 Helper

Use this when an application can use a SOCKS5 proxy and you want its TCP
connections carried through Gatherlink.

SOCKS5 support currently means TCP CONNECT. SOCKS5 UDP ASSOCIATE is not part of
the current user path.

## On The Exit Node

1. Start a Gatherlink core service that can receive helper stream packets.
2. Start the stream exit helper:

```bash
gatherlink helpers stream-exit \
  --listen 127.0.0.1:7000 \
  --allow-host example.com \
  --allow-port 443 \
  --diagnostics-jsonl .gatherlink/socks-exit.jsonl
```

Use narrow allow lists. The exit should not become an open proxy.

## On The Client Node

1. Start a Gatherlink core service whose UDP service reaches the exit helper.
2. Start the local SOCKS5 helper:

```bash
gatherlink helpers socks5-serve \
  --listen 127.0.0.1:1080 \
  --allow-host example.com \
  --allow-port 443 \
  --gatherlink-service 127.0.0.1:55180
```

3. Point the application at:

```text
SOCKS5 host: 127.0.0.1
SOCKS5 port: 1080
```

## Quick Local Smoke

For local testing only, `--lab-direct` bypasses Gatherlink and connects directly:

```bash
gatherlink helpers socks5-serve \
  --listen 127.0.0.1:1080 \
  --allow-host example.com \
  --allow-port 443 \
  --lab-direct
```

Do not use `--lab-direct` as a real tunnel.
