# Source Map

Use this to find the right code without crossing ownership boundaries.

## Rule Of Thumb

Python owns meaning and lifecycle. Rust executes compact packet facts.

If a change needs operator language, config names, helper behavior, provisioning,
policy, diagnostics rendering, or platform commands, it belongs in Python unless
there is a narrow packet-speed primitive to expose from Rust.

If a change needs frame parsing, AEAD/replay checks, receiver-index lookup,
dedupe, fragmentation, queues, or cheap packet-time scheduling, it may belong in
Rust.

## Rust Crates

| Path | Responsibility |
| --- | --- |
| `crates/protocol` | compact v1/v2 frame layout, encode/decode, protocol invariants |
| `crates/dataplane` | packet executor, UDP services, path sockets, replay, dedupe, relay-hop execution |
| `crates/pybindings` | narrow PyO3 bridge from Python runtime DTOs to Rust execution |
| `crates/time-helper` | small time-helper primitives where Rust is useful |

Rust should not decide service meaning, helper policy, user config behavior, or
operator wording.

## Python Control Plane

| Path | Responsibility |
| --- | --- |
| `python/gatherlink/bootstrap` | bootstrap endpoint resolution, cache, and connection helpers |
| `python/gatherlink/carriers` | carrier adapter lifecycle for alternate packet transports; packet semantics stay unchanged |
| `python/gatherlink/config` | user config loading, schema normalization, validation, runtime DTO expansion |
| `python/gatherlink/runtime` | core/relay runners, service registry, process supervision, live reapply |
| `python/gatherlink/cli` | Typer CLI commands and operator entry points |
| `python/gatherlink/protocol.py` | Python-side compact frame helpers used by control/lab code; Rust remains the hot-path protocol authority |
| `python/gatherlink/dataplane` | Python adapter around Rust bindings and status mapping |
| `python/gatherlink/control` | authenticated control metadata parsing and policy compilation |
| `python/gatherlink/security` | identity/session/provisioning helpers and Python-side security models |
| `python/gatherlink/secrets` | persistent identities, signed bundles, redacted state |
| `python/gatherlink/persistence` | state layout, sealed artifacts, audit helpers |
| `python/gatherlink/platform` | Debian compatibility backend and future OS backends |
| `python/gatherlink/diagnostics` | structured events, buses, sinks |
| `python/gatherlink/release` | release artifact helpers and packaging checks |
| `python/gatherlink/scheduling` | Python-owned scheduling policy, scoring, simulations |
| `python/gatherlink/paths` | path telemetry and MTU/capacity helpers |
| `python/gatherlink/shared` | tiny shared utilities with no policy ownership |
| `python/gatherlink/time` | derived time helper logic and opt-in system time setting support |

## Helpers

| Path | Responsibility |
| --- | --- |
| `python/gatherlink/helpers/socks5` | SOCKS5 TCP CONNECT helper over Gatherlink transport |
| `python/gatherlink/helpers/tcp_forward` | one-to-one TCP forwarding helper |
| `python/gatherlink/helpers/dns` | DNS helper, cache, policy, dnspython integration |
| `python/gatherlink/helpers/wireguard` | WireGuard planning/tooling integration, not WireGuard replacement |
| `python/gatherlink/helpers/relay_fabric` | relay discovery and health helper behavior |
| `python/gatherlink/helpers/status_http.py` | experimental local status/REST helper |
| `python/gatherlink/helpers/traffic_split.py` | Debian policy-routing snippet generation for advanced WireGuard split-profile setups |
| `python/gatherlink/helpers/transport.py` | helper transport abstraction |
| `python/gatherlink/helpers/udp_stream.py` | helper stream framing over Gatherlink UDP service |

Helpers must not become core protocol features. If helper logic needs faster
execution later, expose the smallest Rust primitive and keep helper meaning in
Python.

## Labs And Tools

| Path | Responsibility |
| --- | --- |
| `python/gatherlink/lab` | local lab planning and runtime helpers |
| `configs/examples` | small operator-facing examples |
| `configs/lab` | local lab scenarios and shaping profiles |
| `tools` | acceptance scripts, VM helpers, packaging/release support |
| `tests` | Python tests |
| `crates/*/tests` | Rust integration-style crate tests |

Lab and VM tools may use platform-specific commands, but reusable OS behavior
should move through `python/gatherlink/platform`.

## Docs

| Path | Responsibility |
| --- | --- |
| `docs/user` | short user instructions and common scenarios |
| `docs/operations` | runbooks, testing, diagnostics, releases, dependency policy |
| `docs/architecture` | boundaries and system shape |
| `docs/protocol` | packet, crypto, relay, and session contracts |
| `docs/runtime` | config/runtime/scheduler/state behavior |
| `docs/helpers` | helper scope and implementation contracts |
| `docs/labs` | lab and VM acceptance guidance |
| `docs/reports` | living status, roadmaps, audit follow-ups |
| `docs/future` | shaped ideas not yet release-authorized |

When docs disagree, follow `docs/README.md` and promote current decisions into
the canonical doc rather than implementing from stale reports.

## Boundary Smells

Review carefully if you see:

- Rust parsing user-facing config names
- Rust deciding helper policy
- helper packages importing core runtime internals unnecessarily
- CLI commands hand-parsing diagnostics prose
- platform-specific calls scattered outside `python/gatherlink/platform`
- plaintext routing labels
- redundant routing fields instead of authenticated session/control context
- placeholder packages for deferred helpers
- carrier code changing Gatherlink packet semantics

These are not always bugs, but they deserve a deliberate boundary check before
they settle into the project.
