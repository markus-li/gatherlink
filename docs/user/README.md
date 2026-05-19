# Gatherlink User Guide

Gatherlink is currently tested on Debian. It should work on most Linux systems
with Python 3.12+, Rust-built bindings, and normal UDP sockets. If you find a
bug, please report it as a GitHub issue.

Use this guide for day-to-day setup. Use the design docs only when developing
Gatherlink itself.

## Common Uses

- Run a UDP service over one or more paths: `docs/user/core-service.md`
- Use a local SOCKS5 proxy through Gatherlink: `docs/user/socks5.md`
- Run WireGuard over Gatherlink transport: `docs/user/wireguard.md`
- Check status and fix common problems: `docs/user/troubleshooting.md`

## Basic Setup

1. Install system dependencies for Python, Rust, and normal build tools.
2. Create and activate a Python virtualenv.
3. Install Gatherlink in editable mode:

```bash
pip install -e .
```

4. Check that the CLI works:

```bash
gatherlink --help
```

5. Validate a config before running it:

```bash
gatherlink config validate configs/examples/windows-two-node-a.json
```

## Important Notes

- Gatherlink runs unprivileged for normal UDP service transport.
- Lab setup may need root when it creates test network namespaces or shaping.
- Static crypto is for MVP/lab use. Future releases will add the full session
  handshake.
- SOCKS5 and WireGuard are the most useful helper paths at this stage.
