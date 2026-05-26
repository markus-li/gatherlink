# Gatherlink User Guide

Gatherlink is currently tested on Debian. It should work on most Linux systems
with Python 3.12+, Rust-built bindings, and normal UDP sockets. If you find a
bug, please report it as a GitHub issue.

Use this guide for day-to-day setup. Use the design docs only when developing
Gatherlink itself.

## Common Uses

- Install and smoke-test Gatherlink: [`docs/user/quickstart.md`](quickstart.md)
- Run a UDP service over one or more paths: [`docs/user/core-service.md`](core-service.md)
- Start from common config shapes: [`docs/user/config-cookbook.md`](config-cookbook.md)
- Use a local SOCKS5 proxy through Gatherlink: [`docs/user/socks5.md`](socks5.md)
- Run WireGuard over Gatherlink transport: [`docs/user/wireguard.md`](wireguard.md)
- Generate an easy multipath WireGuard setup:
  [`docs/user/wireguard-multipath.md`](wireguard-multipath.md)
- Check status and fix common problems: [`docs/user/troubleshooting.md`](troubleshooting.md)

## Basic Setup

1. Follow the minimum setup in [`docs/user/quickstart.md`](quickstart.md).
2. Check that the CLI works:

```bash
gatherlink --help
```

3. Validate a config before running it:

```bash
gatherlink config validate configs/examples/windows-two-node-a.json
```

4. Run a local readiness check:

```bash
gatherlink doctor --config configs/examples/windows-two-node-a.json
```

## Important Notes

- Gatherlink runs unprivileged for normal UDP service transport.
- Lab setup may need root when it creates test network namespaces or shaping.
- Authenticated Noise-generated security material is the normal secure path.
  Static crypto remains an explicit lab/manual fallback.
- SOCKS5 and WireGuard are the most useful helper paths at this stage.
- Day-to-day operation is covered in
  [`docs/operations/v0.9-operator-runbook.md`](../operations/v0.9-operator-runbook.md) until the next general runbook
  replaces it; treat it as the current Debian operations guide, not as a new
  feature roadmap.
- Scenario troubleshooting is covered in [`docs/operations/v0.9-troubleshooting-guide.md`](../operations/v0.9-troubleshooting-guide.md).
