# Gatherlink

Gatherlink is a carrier-aware multipath UDP transport for personal labs and
small sites.

It presents local UDP services over one or more logical carrier paths, with a
Python control plane and a Rust packet executor.

```text
local UDP listen -> Gatherlink carrier fabric -> remote UDP emit
```

Gatherlink is intentionally not a firewall, DPI engine, classic SD-WAN
appliance, QoS system, or general proxy framework. It can carry tools such as
WireGuard and helper traffic, but those tools keep owning their own domains.

## Status

The project is at v0.9.2 release-candidate state for Debian personal/lab users
and small sites. The v0.9 and v0.9.1 baselines are closed; current release
evidence, remaining limits, and tag posture are tracked in the living
assessment.

Real-world testing is still limited. So far, Gatherlink has mainly been tested
by the developer as a practical tool for aggregating a fiber connection and a
5G connection, plus extensive lab tests, VM checks, and simulated network
conditions. Feedback from real users in live networks is very welcome and
needed to improve the project; please report bugs, rough edges, and successful
or failed deployment stories as GitHub issues.

Current shape:

- Debian is the tested platform.
- CLI operation is the supported interface.
- Rust owns compact packet execution.
- Python owns config, lifecycle, helpers, diagnostics, provisioning, and
  operator meaning.
- Invalid encrypted packets and invalid relay packets are silently dropped on
  the network side.
- No plaintext routing is supported.
- Routing uses authenticated session/control context and relay-hop state.

Before using this as anything more serious than a lab/small-site tool, read the
release checklist and current living assessment.

## What Works

- compact UDP frame transport
- multi-path local and VM lab flows
- encrypted path transport
- authenticated config-facing security material
- replay and duplicate handling
- managed local services
- diagnostics JSONL and service monitor output
- SOCKS5 TCP CONNECT helper over Gatherlink transport
- TCP forwarding helper over Gatherlink transport
- DNS helper direct and tunnel upstream paths
- WireGuard planning/helper workflow

The exact current proof state is tracked in
`docs/project-living-assessment.md`.

## Quick Start

Install in a development checkout:

```bash
pip install -e .
```

Validate a config:

```bash
gatherlink config validate configs/examples/minimal-client.json
```

Inspect the runtime plan:

```bash
gatherlink run plan configs/examples/minimal-client.json
```

Start a managed service:

```bash
gatherlink run start configs/examples/minimal-client.json --name core.node-a
```

Inspect services:

```bash
gatherlink services list
gatherlink services monitor core.node-a --once
```

For real use, start with the user docs instead of copying these commands
blindly.

## Documentation

Start here:

- `docs/README.md`: documentation map
- `docs/project-story.md`: how the project was shaped and why the architecture
  looks the way it does
- `docs/user/README.md`: short user guide
- `docs/user/config-cookbook.md`: common config shapes
- `docs/operations/v0.9-operator-runbook.md`: day-to-day v0.9 operation
- `docs/operations/v0.9-troubleshooting-guide.md`: scenario troubleshooting
- `docs/operations/v0.9-release-checklist.md`: release gates
- `docs/project-living-assessment.md`: current state and remaining work

For design and implementation:

- `docs/architecture/architecture-contract.md`
- `docs/architecture/source-map.md`
- `docs/protocol/protocol.md`
- `docs/protocol/security.md`
- `docs/protocol/runtime-session-model.md`
- `docs/helpers/helper-priorities.md`

## Development

Run the default verification set:

```bash
cargo fmt -- --check
cargo test --workspace
.venv/bin/ruff check .
.venv/bin/black --check .
python3 -m compileall -q python tests tools
.venv/bin/pytest -q
```

When changing runtime behavior, also run the relevant lab or VM acceptance
checks. Unit tests alone are not enough for packet movement, helper behavior,
crypto, relay, scheduler, or diagnostics changes.

## Security

Gatherlink is security-sensitive networking software under active development.
Do not assume it has had an external security audit.

Report security issues using `SECURITY.md`. Do not publish exploit details in a
public issue before there is a fix or mitigation.

## License

Gatherlink is licensed under the GNU Affero General Public License v3.0 or
later. See `LICENSE`.
