# Gatherlink User Guide

Gatherlink is currently tested on Debian. It should work on most Linux systems
with Python 3.12+, Rust-built bindings, and normal UDP sockets. If you find a
bug, please report it as a GitHub issue.

Use this guide for day-to-day setup. Use the design docs only when developing
Gatherlink itself.

## Common Uses

- Run a UDP service over one or more paths: `docs/user/core-service.md`
- Start from common config shapes: `docs/user/config-cookbook.md`
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
- Authenticated Noise-generated security material is the normal secure path.
  Static crypto remains an explicit lab/manual fallback.
- SOCKS5 and WireGuard are the most useful helper paths at this stage.
- Day-to-day operation is covered in `docs/operations/v0.9-operator-runbook.md` until the next general runbook replaces it; treat it as the current Debian operations guide, not as a new feature roadmap.
- Scenario troubleshooting is covered in `docs/operations/v0.9-troubleshooting-guide.md`.
