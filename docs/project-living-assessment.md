# Project Living Assessment

Last updated: 2026-05-18

This is the living project assessment for Gatherlink. Keep it current as the
implementation changes. It should answer four questions quickly:

- What is real and working now?
- What remains for MVP?
- What remains for v1?
- What should be changed, protected, or watched?

## How To Maintain This File

Update this file after any substantial architecture, protocol, helper, runtime,
or lab milestone.

When updating:

- keep the "Current Verification" section honest
- move items from "Remaining" to "Done" only when tests or runnable behavior
  prove them
- distinguish scaffold, tested implementation, and production-ready behavior
- do not let this file become a second protocol spec; link to canonical docs
  for packet layouts, crypto, config, helper scope, and relay behavior
- every development slice should add or update focused unit tests and should
  run sensible lab/config sets for the area being changed

Canonical docs to consult before changing this file:

- `docs/README.md`
- `docs/architecture/architecture-contract.md`
- `docs/protocol/protocol.md`
- `docs/protocol/security.md`
- `docs/protocol/runtime-session-model.md`
- `docs/runtime/config-runtime-state.md`
- `docs/protocol/control-context.md`
- `docs/protocol/relay-session-lifecycle.md`
- `docs/helpers/helper-priorities.md`
- `docs/operations/testing-strategy.md`
- `docs/reports/v1-roadmap.md`

## Executive Summary

Gatherlink is no longer just an idea or pure documentation. The repository has a
working Rust packet execution core, Python configuration/runtime orchestration,
PyO3 bindings, local lab scaffolding, static AEAD transport mode, multipath
scheduling primitives, fragmentation, batching, dedupe, service monitoring, and
initial helper implementations.

Since the previous assessment, several important implementation steps landed:

- `route_id` was removed from the active Python/Rust hot path, DTOs, scheduler
  reapply, and tests.
- Diagnostics gained normalized event DTOs, a bounded bus, a JSONL sink, and
  foreground runner lifecycle event wiring.
- SOCKS5/TCP helper transport boundaries were tightened and now include a
  Gatherlink UDP service stream adapter plus companion exit; direct TCP is
  explicit lab-only smoke behavior.
- The Rust-backed core now has a managed `gatherlink run start` path with
  service registry, IPC status/monitor/close, JSONL diagnostics, and visible
  static/plaintext warnings.
- A two-instance Windows/WSL preparation path exists and has carried an
  encrypted UDP payload between `gatherlink-dev` and `gatherlink-peer`.
- A local status HTTP helper exists for read-only machine/service discovery,
  including `.hidden` service entries.

The strongest part of the project is the boundary discipline:

- Python owns policy, config, orchestration, diagnostics, helpers, and operator
  meaning.
- Rust owns compact packet execution, sockets, counters, AEAD, replay, frame
  parsing, dedupe, batching, fragmentation, queues, and cheap scheduling.

That boundary is the project's best architectural asset. It makes the system
easier to test and less likely to turn into an accidental firewall, VPN stack,
or opaque SD-WAN mesh.

The main remaining weakness is no longer the narrow core MVP path; it is the
boundary between MVP and the larger v1 product. V1 targets personal/lab users
and small sites, with Debian as the only supported platform. Debian-specific
behavior must still sit behind compatibility interfaces so future platforms can
be added without rewriting runtime or helper logic.

Several important v1 parts exist as docs, narrow interfaces, or
test-only/manual paths:

- authenticated session setup is not implemented; static session material is
  the current secure transport path
- relay sessions are documented but not implemented
- the diagnostics event bus has DTO/bus/JSONL primitives and core lifecycle,
  counter, reapply, shutdown, and helper-stream producers; broader helper and
  security drop producer coverage remains v1 hardening work
- helper services exist, including first SOCKS5/TCP stream-over-Gatherlink
  adapters and a shared companion UDP stream exit
- carrier modules beyond local UDP path sockets are placeholders
- live scheduler reapply is wired into the managed runner with cadence,
  diagnostics, and validation, but still needs longer production soak tests
- Python tests are green after the managed runner, status HTTP helper,
  two-node config, runtime JSON redaction, and process restart fixes.

The project is at MVP-complete shape for local/lab validation if the scope stays
narrow: a two-node, unprivileged, Rust-backed UDP service over configured UDP
path sockets, with explicit config, static AEAD transport, clear warnings,
service monitoring, diagnostics, deterministic tests, and repeatable local lab
proof. A true two-VM Windows acceptance run is prepared but still needs two
distinct Debian VM network identities rather than WSL instances sharing the
same WSL virtual network.

The v1 release should be a real Git tag, expected to be `v1.0.0`, after the v1
acceptance gates pass.

## Current Verification

Verified on 2026-05-18:

```text
cargo test
```

Result: passed.

Note: the first full Rust run in this pass hit a transient failure in two
`gatherlink-dataplane` duplicate/dedupe tests. The focused
`gatherlink-dataplane --test path_transport` rerun passed, and the following
full `cargo test` passed. Track this as a possible flaky timing test if it
recurs.

Coverage observed:

- crypto envelope tests
- X25519/Ed25519 key tests
- replay window tests
- dataplane engine tests
- path transport tests
- bounded queue tests
- runtime config tests
- scheduler tests
- UDP service tests
- protocol control/frame tests
- PyO3 engine API tests
- time-helper tests

Python tests:

```text
.venv/bin/pytest -q
```

Result: passed, `208 passed in 3.47s`.

The previous config CLI JSON fix is now committed. `config show --runtime
--json` is accepted, the runtime marker is asserted under
`metadata.runtime_model`, and static runtime key bytes are redacted in operator
JSON output.

Plain `pytest` was not on PATH in the WSL shell; the repo virtualenv command
works.

Additional verification in this pass:

```text
gatherlink lab helpers-smoke
gatherlink lab rust-smoke configs/lab/local-dual-path.json --count 4
gatherlink lab rust-smoke configs/lab/local-dual-path-encrypted.json --count 4
gatherlink lab rust-smoke configs/lab/local-three-path.json --count 4
gatherlink lab cleanup/up/smoke/apply-network-mode/send/cleanup configs/lab/local-dual-path.json
gatherlink lab cleanup/up/smoke/apply-network-mode/send/cleanup configs/lab/local-dual-path-encrypted.json
gatherlink lab cleanup/up/smoke/send/cleanup configs/lab/local-three-path.json
```

Result: passed. The live lab pass included bidirectional traffic, normal
saturated shaping, forced-drop shaping, latency/jitter skew, encrypted
dual-path traffic, three-path traffic, and cleanup.

WSL MVP acceptance gate:

```text
powershell.exe -ExecutionPolicy Bypass -File tools/run_wsl_mvp_acceptance.ps1
```

Result: passed on 2026-05-18. It configures the WSL private address shim, syncs
`gatherlink-peer`, validates both static-AEAD node configs, applies three shaped
carrier links, starts both managed services, sends counted UDP payloads, drops
one carrier path, verifies degraded delivery, restores shaping, verifies exact
recovery delivery, validates per-path status/monitor counters, validates JSONL
diagnostics, and closes both services. This gate passed again in this assessment
pass. Keep this command green before calling the Windows/WSL operational path
healthy.

Two-instance Windows/WSL preparation smoke:

```text
gatherlink run start configs/examples/windows-two-node-b.json --name core.windows-node-b
gatherlink run start configs/examples/windows-two-node-a.json --name core.windows-node-a
python3 tools/udp_probe.py receive 127.0.0.1:51820
python3 tools/udp_probe.py send 127.0.0.1:55180 hello-two-node
```

The WSL smoke now uses the private WSL address shim:

```text
powershell.exe -ExecutionPolicy Bypass -File tools/setup_wsl_private_lan.ps1
python3 tools/udp_probe.py receive 10.88.0.12:51820
python3 tools/udp_probe.py send 10.88.0.11:55180 mvp-lifecycle-ok
```

Result: passed. The peer target received `mvp-lifecycle-ok`, service monitor
showed `core.windows-node-a` transmit counters and `core.windows-node-b`
receive counters, and `gatherlink services close` left both process records
stopped without orphaning the UDP socket owner.

## What Has Been Done

### Project Shape

Done:

- Rust workspace with crates:
  - `gatherlink_protocol`
  - `gatherlink_crypto`
  - `gatherlink_dataplane`
  - `gatherlink_pybindings`
  - `gatherlink_time_helper`
- Python package with:
  - config loading/validation/expansion
  - runtime models
  - scheduling compiler/scoring/simulation
  - dataplane bridge
  - runtime runner/reapply scaffolding
  - lab planning/runtime scaffolding
  - service registry and monitoring CLI
  - bootstrap validation scaffolding
  - secrets/identity/static-session helpers
  - helper implementations and stubs
- Docs organized around canonical design boundaries and helper priorities.
- Tests exist in both Rust and Python, with a meaningful amount of behavior
  covered.

Assessment:

This is good. The repo is already shaped like a real system. The main risk is
that the breadth makes it easy to confuse scaffolded modules with finished
features. This file should keep that distinction sharp.

### Protocol And Frame Format

Done:

- Compact v1 plaintext logical frame implemented in Rust.
- Compact v2 decrypted secure logical frame implemented in Rust.
- V1 includes visible `version`.
- V2 omits version because secure session context selects it.
- Header lengths match docs:
  - v1 normal: 14 bytes
  - v2 normal: 13 bytes
  - fragment metadata: +10 bytes
- Frame kinds implemented:
  - data
  - control
  - batch
- Batch payload encoding exists for small datagram coalescing.
- Fragment metadata and fragmentation/reassembly paths exist.
- Reserved flag rejection and malformed batch/fragment tests exist.

Assessment:

This is one of the best-completed areas. The implementation and docs are close
to each other. The protocol is compact and similar in spirit to WireGuard where
that matters: small public envelope, no public negotiation oracle, session
lookup by receiver index, and authenticated encrypted payload.

Watch:

- The current security/encryption design explicitly removes `route_id`.
  Routing through untrusted peers is done by outer routing/relay-hop headers
  plus authenticated relay session state, not by endpoint packet fields or
  runtime route labels.

### Crypto And Replay

Done:

- ChaCha20-Poly1305 data envelope implemented.
- Public encrypted packet shape implemented:
  - `packet_type`
  - `receiver_index`
  - `counter`
  - ciphertext
  - tag
- AEAD associated data includes a domain separator and public header.
- Replay window implemented and tested.
- Static transport security mode exists.
- Static key derivation from identity material exists in Python.
- Silent-drop behavior is represented as `CryptoError::SilentDrop`.
- Rust dataplane can protect frames as v2 and unprotect encrypted packets back
  into compact frames.

Assessment:

The crypto packet primitive is a real implementation, not just a placeholder.
It is good enough to exercise the production-shaped secure packet path in labs
and tests.

Not production-complete:

- no Noise-style authenticated session handshake yet
- no session rekey lifecycle
- no receiver-index rotation lifecycle
- no trust-root/topology enforcement in packet setup
- no relay-hop crypto/session implementation yet
- static key config is suitable for lab/manual testing, not the final security
  story

### Rust Dataplane

Done:

- Rust binds user-facing UDP services.
- Rust binds path transport UDP sockets when path endpoints are configured.
- Rust forwards local UDP datagrams through Gatherlink framing.
- Rust receives path frames and emits original UDP payloads.
- Rust supports fixed target return and learned single-source return modes.
- Rust supports batching, fragmentation, reassembly, dedupe, and duplicate
  telemetry.
- Rust applies Python-compiled scheduler primitives.
- Rust supports service disable/enable from Python-owned policy.
- Rust queues reserved service payloads for Python instead of interpreting them.
- Rust exposes status snapshots for service/path/control counters.
- Rust supports full config reapply and narrower scheduler-only reapply.
- Rust validates DTO boundaries such as service id ranges and path ids.
- Rust path runtime config no longer carries `route_id`.

Assessment:

This is excellent for the current stage. The dataplane has real behavior and
real tests. The code generally respects the architecture contract.

Needs change or care:

- The carrier abstraction files for QUIC, WSS/TLS, TCP/TLS, stealth UDP, and
  obfuscation are mostly placeholders. The real transport today is local UDP
  path sockets.
- The runner still loops over services and catches receive timeouts; it is
  practical for smoke/lab use, but not yet a polished async/evented supervisor.
- Relay-hop runtime state still needs implementation before untrusted relay
  forwarding can be treated as a production path.

### Python Config And Runtime Models

Done:

- Pydantic config models exist.
- Supported config formats:
  - `minimal-client`
  - `minimal-server`
  - `wireguard-client`
  - `wireguard-server`
  - `dns-helper`
- Schema version is explicit and currently version `1`.
- Config validation catches duplicate service names, service ids, listen
  addresses, path names, and invalid helper references.
- Runtime models make service ids, priorities, scheduler hints, paths, security,
  and helpers explicit.
- Runtime expansion assigns user service ids in the `256..65535` range.
- Static security material is validated as base64 32-byte keys.
- Rust bridge receives DTOs built from expanded runtime state.
- Python runtime scheduler/path models and Rust DTO conversion no longer carry
  `route_id`.

Assessment:

This is good and consistent with the architecture. The user-facing config is
small; the runtime contract is explicit.

Needs change or care:

- Helper config models are still very narrow: WireGuard and DNS are modeled;
  SOCKS5/TCP/relay helper config needs to be represented when those helpers are
  integrated into normal service startup.
- Schema migration scaffolding exists by documentation and version modules, but
  there is no real multi-version config migration pressure yet.
- Numeric ids are allowed and validated, but operator warnings and runtime
  introspection should make explicit ids visible everywhere.
- Config CLI JSON shape has just been tightened in the current worktree:
  `config show --runtime --json` is accepted and the runtime marker is expected
  under `metadata.runtime_model`.

### Scheduling And Multipath

Done:

- Scheduler modes exist in Python/Rust DTOs:
  - round robin
  - weighted round robin
  - lowest latency
  - loss aware
  - capacity aware
  - least queue
  - earliest completion first
  - blocking estimation
  - balanced
  - adaptive
- Rust tests cover weighted round robin and several compiled scheduler modes.
- Python compiler/scoring modules exist.
- Service-level fanout exists.
- Small-payload duplication exists.
- Expected and unexpected duplicate telemetry are separated.
- Hot scheduler reapply functions exist.
- Scheduler reapply preservation now keeps path ids and MTU only; stale route id
  preservation was removed.

Assessment:

The scheduler foundation is strong. The code already reflects the major design
decision: Python decides rich policy and compiles small primitives for Rust.

Not production-complete:

- The continuous telemetry-to-reapply loop is wired into the managed runner, but
  it still needs longer soak runs, richer remote receiver telemetry, and clearer
  operator explanations before it should be treated as mature v1 behavior.
- Receiver metrics are not yet a robust authenticated control-plane feature.
- Adaptive modes exist as primitives/scaffolding; they should not be marketed as
  mature until real receiver metrics, smoothing, and diagnostics exist.

### Runtime, Services, And CLI

Done:

- Typer CLI exists.
- CLI areas include:
  - bootstrap
  - config
  - helpers
  - lab
  - run
  - secrets
  - services
  - stats
  - time
- Runtime runner can run a Rust-backed core loop.
- Runtime reload functions can recompile and reapply scheduler state.
- Service registry exists for process/systemd managed service metadata.
- Service attach/status/logs/monitor/close commands exist.
- Monitor can request temporary higher-rate control metadata.
- `gatherlink run service --diagnostics-jsonl PATH` can publish foreground
  runner diagnostics and startup failures to JSONL.
- `gatherlink helpers stream-exit --diagnostics-jsonl PATH` can publish helper
  stream lifecycle and policy diagnostics to JSONL.

Assessment:

This is good scaffolding for operations. It is not just a library; there is an
operator-facing shape.

Needs change or care:

- The diagnostics event bus has first primitives, foreground runner lifecycle
  events, startup failure events, and helper stream lifecycle/policy events.
  Most remaining producers still need to be routed through it.
- Process supervision is still limited. The product needs a clean story for
  starting, stopping, IPC, logs, and live reload outside lab scenarios.
- Human CLI output exists, but `--json` consistency should be checked command by
  command before MVP.

### Local Labs

Done:

- Local lab scenario models exist.
- Local dual-path and local multi-path are supported scenario categories.
- Lab plans include namespace/veth setup, tc shaping, service launch, traffic
  checks, and reorder policy compilation.
- Shaping profiles exist.
- Lab docs are extensive.
- Local Rust-backed transport tests prove encoded frames move over path sockets.
- Encrypted local path transport tests exist.
- `gatherlink lab helpers-smoke` runs userland smoke scenarios for the active
  helpers and the helper transport boundary.
- `tools/run_wsl_mvp_acceptance.ps1` is the repeatable Windows/WSL MVP gate for
  encrypted three-path managed service startup, traffic, carrier-path
  degradation/recovery, monitor visibility, diagnostics, and clean teardown.

Assessment:

The lab story is one of the project's best assets. It gives the project a way
to avoid fantasy architecture and keep proving real packet behavior.

Not complete:

- Runnable IPv6 lab parity is still called out as future work.
- Some scenario kinds are accepted by model but marked `not_implemented`.
- Lab setup may require root for namespaces/shaping, which is appropriate, but
  must remain separate from the normal Gatherlink service privilege model.
- The local lab and WSL acceptance gates are explicit. They still need to be
  promoted into CI-friendly wrappers once the target CI environment supports
  the required namespace/shaping capabilities.

### Diagnostics

Done:

- Service monitor/status/log surfaces exist.
- Rust exposes counter snapshots.
- Docs define stable event codes and JSONL-first direction.
- Several modules log warnings or diagnostics.
- Normalized event DTOs exist.
- A bounded Python diagnostics bus exists.
- JSONL is implemented as the first durable sink.
- Foreground core runner warnings, service-bound facts, and shutdown events can
  publish through the bus.
- `gatherlink run service --diagnostics-jsonl PATH` can append foreground
  runner diagnostics while the service loop is running.
- Foreground core startup failures can publish `runtime.start_failed` events
  with config path, error type, and validation details when a JSONL sink is
  configured.
- Helper UDP stream exits can publish open, close, denied target, unreachable
  target, and invalid frame events through the same bus.
- `gatherlink helpers stream-exit --diagnostics-jsonl PATH` can append helper
  stream diagnostics while the companion exit is running.

Not done:

- Rust status/counters, crypto drops, config reload results, and service
  monitor requests still need broader producer integration.
- DNS/time/SOCKS5/TCP helper-specific warnings still need broader structured
  diagnostics coverage beyond the shared UDP stream companion exit.
- Operator "why" explanations are not yet generated from structured facts.

Assessment:

The event bus foundation is now real and tested. The remaining MVP risk is
producer integration: new runtime/helper/security facts must use this pipeline
instead of creating more ad hoc status shapes.

### Helpers

Done:

- Helper priority docs are clear.
- Time helper has a Rust privileged execution primitive and tests.
- DNS helper exists with dnspython, cache, direct UDP/TCP fallback, IDNA-aware
  names, and DNSSEC policy based on upstream AD bit.
- SOCKS5 helper exists using `asyncio-socks-server`, conservative allow lists,
  optional auth, stats, and connector abstraction.
- TCP forwarding helper exists with asyncio streams, counters, timeouts, and
  narrow one-to-one semantics.
- Shared helper stream transport abstractions exist.
- SOCKS5/TCP production defaults refuse direct TCP unless a Gatherlink stream
  transport adapter is supplied.
- A first Gatherlink UDP service stream adapter and companion stream exit are
  implemented for SOCKS5 and TCP forwarding helpers.
- Lab-only direct TCP stream transport exists for local smoke tests.
- WireGuard helper files and tests exist.
- Relay fabric models/discovery/health files exist.
- Helper smoke scenarios cover time, DNS, DNS negative/DNSSEC policy, TCP
  forward, SOCKS5, WireGuard planning, relay fabric, relay negative, and
  transport-boundary behavior.
- Deferred helper areas exist mostly as stubs or design notes.

Assessment:

The helper direction is good: Python-owned, narrow scope, no Rust proxy parsing.
The current helper code is useful scaffold and in some cases runnable.

Important caveat:

- SOCKS5 and TCP forwarding no longer silently default to direct TCP, which is
  good. The first Gatherlink UDP stream adapter and companion exit exist, but
  they still need supervisor/config integration, structured diagnostics,
  explicit operator configuration, and broader cross-process lab acceptance
  coverage.
- The current helper UDP stream frames are JSON/base64 control/data frames over
  a configured Gatherlink UDP service. That is appropriate for the helper
  adapter slice, but it should be treated as a helper transport framing layer,
  not as a new core packet format.
- DNSSEC currently trusts upstream AD-bit validation. Full local chain
  validation is not implemented yet.
- DNS helper direct upstream works; tunnel and DoH upstream kinds are TODO.
- Relay fabric discovery/health is separate from the secure relay session
  lifecycle, which is documented but not implemented.

### Bootstrap, Identity, And Secrets

Done:

- Node identity records exist.
- Public identity records exist.
- Signed documents and age-related secret handling exist.
- Static session key derivation exists.
- Bootstrap endpoint cache/probe/challenge scaffolding exists.
- Authenticated bootstrap proof can verify signed challenge material against an
  expected peer identity and endpoint.
- Insecure bootstrap is explicitly warned as lab-only.

Assessment:

This is a good bridge between plaintext lab work and future authenticated
deployment. It is not yet a complete enrollment/control-plane system.

Not production-complete:

- no signed topology/provisioning bundle lifecycle implementation
- no trust-root management flow
- no revocation generation enforcement wired through session setup
- no bootstrap-token redemption flow
- no Noise handshake or dynamic peer enrollment

### Time

Done:

- Time models and sources exist in Python.
- Time sink tests exist.
- Rust privileged time-helper can preview/refuse/apply correction requests.
- The helper refuses corrections outside bounds.
- Docs correctly state that core must not set system time itself.

Assessment:

This is good. The boundary is narrow and security-conscious.

Needs change or care:

- Production integration should detect/warn when another NTP agent is active.
- The helper should remain explicit opt-in and never become automatic by
  accident.

## What Is Great

The Python/Rust boundary is unusually clear.

The project has resisted the common failure mode of letting the fast dataplane
become a policy engine. Rust is allowed to execute compact facts. Python owns
meaning. That is exactly right for this kind of system.

The protocol has become compact and coherent.

V1/v2 are now simple. Secure packets have a WireGuard-like minimal public
surface. The relay story avoids plaintext routing labels. The overhead is
explicit and acceptable.

The test suite is real.

`cargo test` passes, and the Python suite now runs 204 passing tests on the
current worktree. Tests cover meaningful behavior: fragmentation, batching,
encrypted path packets, replay rejection, scheduler choices, runtime DTOs,
helper policy, diagnostics DTO/bus/JSONL, helper smoke scenarios, and lab
planning.

The docs now have a navigable shape.

The docs are not just notes anymore. There is a map, canonical sources, helper
priorities, protocol/security decisions, and a clear stale-information rule.

The Debian compatibility backend has started.

The first v1 platform slice routes lab `ip`/`tc` calls, systemd journal command
construction, systemd active-state checks, and interface MTU sysfs reads through
a Debian-only compatibility backend. This is not yet the full platform layer;
standard Debian config/state/log paths, capability checks, and broader helper
integration still need to move behind the same boundary.

The lab-first posture is healthy.

Network software lies easily if it is only reasoned about. The local lab and
path socket tests keep the project tied to real packet movement.

## What Is Good But Not Done

Static crypto is a good stepping stone, not the final answer.

It lets the production AEAD packet path be tested now. It should not become the
permanent security model.

Scheduler primitives are good.

They are already richer than MVP needs, and the managed runner can turn status
snapshots into Python-owned scheduler reapply decisions. The remaining work is
production hardening: longer soak tests, authenticated receiver metrics, better
hysteresis tuning, and structured operator explanations for each decision.

Helpers are useful first implementations.

They express shape, policy, counters, library choices, helper stream transport
interfaces, and first Gatherlink UDP stream exits. Several still need normal
supervisor/config integration and stronger acceptance labs.

Service registry, monitor, and diagnostics bus are useful.

They now have the first production-shaped diagnostics event bus underneath
them, but still need broader producer integration.

## What Is Bad Or Risky

There is a lot of scaffold.

The module tree is broad. Some files are intentionally placeholders. This is not
bad by itself, but it makes it easy to overestimate what is implemented. This
assessment should keep marking features as scaffold, tested primitive, or
production behavior.

Diagnostics are still behind the rest of the system.

The first bus/DTO/JSONL primitives exist, so the next risk is producer drift:
helpers, runtime lifecycle, config reloads, crypto drops, service counters, and
relay/security drops should route through structured events instead of adding
new ad hoc status shapes.

`route_id` has been removed from code.

The security/encryption design explicitly removes it. Secure routing through
untrusted peers uses outer routing/relay-hop headers and authenticated relay
session state. The implementation now avoids preserving route id as a
compatibility field in runtime DTOs, path config, scheduler reapply, transmit
plans, tests, fixtures, and examples.

Carrier breadth is mostly aspirational.

Raw UDP path sockets are real. QUIC, WSS/TLS, TCP/TLS, stealth UDP, and
obfuscation modules are mostly placeholders. Do not imply they are working
until they have tests and runnable paths.

Helper code can outpace helper operations.

SOCKS5 and TCP direct connectors are useful for lab tests, and the first
Gatherlink UDP service stream adapter now exists. The remaining risk is not
basic adapter shape; it is operational integration: explicit config, companion
exit supervision, allow-list diagnostics, backpressure/loss behavior, and
cross-process lab coverage.

Security docs are ahead of implementation.

This is fine right now, but production claims must wait for handshake,
receiver-index lifecycle, rekey, signed topology, revocation, and relay-session
implementation.

## MVP Definition

MVP should be deliberately narrow:

One unprivileged Gatherlink service can carry UDP payloads between two nodes
over multiple configured UDP path sockets, using compact frames, static AEAD
transport, deterministic scheduling, batching/fragmentation/dedupe, clear
service monitoring, explicit config/runtime introspection, and local lab proof.

MVP should not include:

- dynamic mesh routing
- production relay fabric
- peer failover
- adaptive scheduler marketing
- full helper ecosystem
- WireGuard replacement behavior
- generic TCP proxy product
- firewall/NAT/LAN routing features
- automatic privileged time changes

## Remaining Work For MVP

### MVP P0: Productize The Core Happy Path

Goal:

Two configured processes can run the Rust-backed core over real UDP path
sockets and be operated with normal CLI/service commands.

Done:

- make the foreground runner/service start path the normal documented path
- ensure config examples match the actual runner
- make all startup warnings visible, especially plaintext/static security and
  explicit numeric ids
- ensure failures are readable without a debugger
- ensure service lifecycle commands can start, status, monitor, and stop the
  core cleanly outside lab-only flows
- document the golden MVP command path for local Windows/WSL preparation and
  future two-VM testing
- keep config CLI JSON output stable and covered by tests

Status:

- managed `gatherlink run start` starts the Rust-backed core as a process
  service with registry, IPC status/monitor/close, JSONL diagnostics, and
  scheduler reapply cadence
- two WSL instances have been prepared and the one-command acceptance gate
  carries counted encrypted UDP payloads from node A to node B over three shaped
  Gatherlink carrier paths
- true two-VM acceptance remains an environment step, not a missing core path

### MVP P0: Diagnostics Event Bus

Goal:

One bounded Python-owned diagnostics event bus that can feed stdout and JSONL
without blocking dataplane/control loops.

Done:

- implement event DTOs in `diagnostics/events.py`
- implement bounded async/sync-safe bus in `diagnostics/bus.py`
- implement JSONL sink first
- wire foreground runner warnings, service-bound events, and shutdown events to
  the diagnostics bus

Done in the MVP path:

- publish core counter snapshots through the diagnostics bus
- publish scheduler reapply success/skip diagnostics
- publish helper stream lifecycle and policy diagnostics

Remaining v1 hardening:

- broaden DNS/time/SOCKS5/TCP helper-specific diagnostics beyond the shared
  helper stream exit
- expose crypto/security drops as structured events wherever they cross the
  Python diagnostics boundary
- add richer operator "why" explanations from structured facts

Status:

- MVP diagnostics are implemented and tested; v1 producer coverage remains a
  clear improvement area

### MVP P1: Static Secure Mode As A Supported Lab/Manual Mode

Goal:

Static AEAD transport is easy and safe enough to use for local/manual MVP
testing without hand-copying raw symmetric keys.

Done:

- document and test end-to-end static identity/session provisioning commands
- ensure receiver indexes and key direction are unambiguous
- ensure encrypted local lab config works as a first-class path
- ensure invalid secure packets are silent on network and counted locally
- make plaintext mode loudly explicit

Status:

- Rust AEAD path works
- Python static key derivation exists
- encrypted path tests pass
- static mode is suitable for MVP lab/manual use, but not the final v1 security
  story

### MVP P0: Route Id Removed

Goal:

The implementation should match the security/encryption docs: no `route_id`
field remains in packet format, runtime DTOs, scheduler hot path, transmit
plans, tests, fixtures, or active docs.

Done:

- remove `route_id` from Python runtime models and DTO conversion
- remove `route_id` from Rust path config and transmit plan structures
- remove scheduler reapply preservation of route ids
- remove route id from tests, fixtures, examples, and stale comments

Status:

- implementation cleanup complete
- relay-hop runtime state remains a separate MVP/v1 security task

### MVP P1: Live Scheduler Reapply Loop

Goal:

Runtime can periodically convert status/telemetry into scheduler primitives and
hot-apply them to Rust.

Done:

- define loop cadence and hysteresis
- wire status snapshots into `hot_reapply_scheduler_from_status`
- ensure invalid telemetry cannot destabilize runtime
- produce diagnostics explaining reapply decisions
- test live reapply in a long-running service scenario

Status:

- managed services can run the Python-owned scheduler reapply loop with
  `--scheduler-reapply-interval`
- v1 still needs longer soak testing and richer real-world telemetry inputs

### MVP P1: Local Lab Becomes The Acceptance Gate

Goal:

The local dual-path lab proves MVP behavior repeatedly.

Done:

- one command path for setup, run, monitor, send, fail a path, recover, and
  teardown
- encrypted lab path included in acceptance checks
- packet split over three WSL carrier paths, carrier degradation/recovery,
  monitor visibility, JSONL diagnostics, and teardown are demonstrated by the
  WSL acceptance gate
- no Gatherlink process runs privileged; only lab setup uses root

Status:

- lab configs/docs exist
- local path tests exist
- WSL acceptance gate exists as `tools/run_wsl_mvp_acceptance.ps1`
- future work is to add a true two-VM run once Windows networking exposes two
  distinct Debian VM addresses

### MVP P1: Helper Scope Guardrails

Goal:

Active helpers do not sprawl beyond MVP.

Needed:

- keep time helper opt-in and warning-heavy
- keep DNS helper local/direct first, with DNSSEC AD policy clear
- keep SOCKS5/TCP helpers from becoming general proxy products now that a
  Gatherlink UDP stream adapter and companion exit exist
- keep WireGuard helper as orchestration/guidance, not protocol ownership
- keep relay fabric to discovery/health until relay sessions exist

Status:

- docs are clear
- first helper stream adapter/exit code exists and emits structured diagnostics
- normal helper supervisor/config integration still incomplete

## Remaining Work For v1

### V1: Authenticated Session Handshake

Needed:

- Noise-style or equivalent authenticated session setup
- receiver-index allocation/rotation
- rekey
- key phase handling
- replay window reset semantics
- signed transcript/context binding
- downgrade/capability handling
- no public oracle behavior

This is the biggest security milestone after MVP.

### V1: Signed Topology And Trust Root Lifecycle

Needed:

- trust root creation/import/export
- signed topology/provisioning bundles
- topology generations
- revocation generation enforcement
- identity rotation with signed transition
- bootstrap token redemption
- audit-friendly state

### V1: Secure Relay Sessions

Needed:

- relay-hop session provisioning from signed topology/control context
- relay receiver indexes and relay replay windows
- hop AEAD wrapping/unwrapping
- authorization checks: direction, next hop, expiry, generation, limits
- local diagnostics for invalid relay packets
- tests proving invalid packets are not forwarded and get no network response

### V1: Control Context

Needed:

- authenticated control message format
- generation ids
- service mapping updates
- helper-control wrapper for small helper commands
- reserved-service decoder registry in Python
- stale generation rejection
- no endpoint IP/port changes through control context

### V1: Diagnostics And Operator UX

Needed:

- stable event codes everywhere
- JSONL first
- consistent `--json` outputs
- `gatherlink doctor`
- structured "why" explanations
- event-driven helper warnings
- Prometheus/WebSocket later if useful

### V1: Debian Compatibility Backend

Needed:

- Debian-only support statement for v1
- platform compatibility package with a Debian backend
- service manager adapter for systemd/process lifecycle
- log adapter for files and journalctl-compatible views
- network inspection adapter for `ip`, `ss`, `tc`, `/sys`, and interface facts
- privilege/capability checks behind platform calls
- lab/VM setup commands kept outside runtime and behind tools/platform helpers

OS-specific behavior must not be scattered across runtime, helpers, protocol, or
diagnostics code.

### V1: Experimental Local REST Helper

Needed:

- optional REST helper started explicitly from CLI
- bind to `127.0.0.1` by default
- non-loopback bind only with an explicit danger flag
- mark all REST docs and output as `EXPERIMENTAL`
- read APIs can remain available while the helper is running
- write APIs expire after one hour by default unless the helper is restarted
  from CLI
- no secret key material in responses
- same structured facts as CLI/status output

The REST service is a helper/control-plane sidecar, not core runtime or
dataplane logic.

### V1: Peer Failover

Needed:

- peer priority
- standby peer probing
- aggressive failover
- conservative failback
- session-aware migration
- minimum dwell windows
- diagnostics explaining peer choice

### V1: Carrier Expansion

Needed:

- real WSS/TLS fallback
- real QUIC/datagram carrier if chosen
- TCP/TLS behavior if needed
- obfuscation profiles with tests
- carrier discovery and ranking
- path validation for real multi-WAN environments

### V1: Helper Completion

Needed:

- DNS tunnel/DoH upstream support if still wanted
- full local DNSSEC validation if upstream-AD is not enough
- production helper config/supervisor integration for the SOCKS5 Gatherlink UDP
  stream adapter and companion exit
- production helper config/supervisor integration for the TCP forward
  Gatherlink UDP stream adapter and companion exit
- helper stream backpressure, timeout, and cross-process lab hardening
- WireGuard helper config generation and lifecycle integration
- relay fabric discovery feeding authenticated topology/control state

### V1: Real VM Acceptance

Needed:

- two Debian VMs with distinct network identities
- simple Bash/SSH deploy scripts under `tools/vm_acceptance/`
- no Ansible requirement for v1
- build VM scripts, configs, report format, and docs now, but do not ask for
  VM access or attempt real VM operations until the project owner provides
  access
- AI-assisted deploy allowed only when it produces auditable scripts/configs
- VM acceptance report with commands, configs with secrets removed, logs,
  diagnostics, monitor output, and pass/fail state
- traffic, monitor, diagnostics, service close, and path degradation/recovery
  checks equivalent to the WSL gate where the VM network can model them

### V1: Persistence

Needed:

- persisted node identities
- trust roots
- signed topology/provisioning bundles
- last-known endpoints
- relay health hints
- non-authoritative caches
- sealed secrets
- canonical signed artifacts where needed

## What Needs To Change Soon

1. Broaden diagnostics producers before adding many more features.

The bus and MVP producers exist now. The next job is broadening helper-specific
and security/drop producers so v1 features do not invent local status shapes.

2. Keep the MVP runner path boring and green.

The golden WSL gate starts two nodes over three paths, sends traffic, shows
status, drops and recovers a path, validates diagnostics, and stops cleanly.
Keep that gate green before expanding outward.

3. Keep helper implementation behind helper scope.

SOCKS5 and TCP forwarding now fail closed without a stream transport, which is
good. The first Gatherlink UDP stream adapter and companion exit path now
exist. The next step is making them operable through explicit helper config,
supervision, diagnostics, and acceptance labs without turning them into generic
local proxies.

4. Do not market adaptive scheduling yet.

The primitives are promising, but serious adaptive scheduling requires receiver
metrics, smoothing, bad-metric handling, and operator explanations.

5. Keep static crypto clearly labeled.

Static AEAD is useful and tested. It is not the final identity/session protocol.

## Suggested Near-Term Priority Order

1. Run the WSL MVP acceptance gate on every operational change.
2. Move from WSL shared-namespace proof to true two-VM proof with distinct
   network identities.
3. Add the Debian compatibility backend boundary before platform-specific work
   spreads.
4. Add the real-VM acceptance deploy scripts and report format.
5. Add the experimental local REST helper with one-hour write expiry.
6. Broaden diagnostics producer coverage for helper-specific and security/drop
   events.
7. Soak the live scheduler reapply loop under longer traffic and path-flap runs.
8. Helper supervisor/config integration for the active helpers, especially the
   Gatherlink UDP stream adapter and companion exit for SOCKS5/TCP helpers.
9. Begin authenticated control/session design implementation.

## Feature Status Table

| Area | Status | Notes |
| --- | --- | --- |
| Compact v1/v2 frames | Implemented and tested | Strong |
| AEAD envelope | Implemented and tested | Static mode only |
| Replay protection | Implemented and tested | Needs lifecycle/rekey later |
| Rust UDP service dataplane | Implemented and tested | Real path sockets exist |
| Multipath scheduling | Implemented primitives and live reapply loop | Needs longer soak and richer telemetry |
| Batching | Implemented and tested | Good |
| Fragmentation/reassembly | Implemented and tested | Good |
| Dedupe | Implemented and tested | Good |
| PyO3 bridge | Implemented and tested | Good |
| Config validation/expansion | Implemented and tested | Needs helper expansion |
| Runtime reload | Implemented for scheduler reapply | Broader config reload remains v1 |
| Service registry/monitor | Implemented and tested | WSL gate validates status/monitor/close |
| Diagnostics event bus | MVP slice implemented and tested | Broader producer integration remains v1 |
| Local lab | Implemented for MVP | Helper smoke, Rust smokes, and WSL MVP gate pass |
| Static identity/session material | Implemented | Lab/manual stepping stone |
| Authenticated handshake | Not implemented | v1 critical |
| Signed topology/trust roots | Not implemented | v1 critical |
| Relay sessions | Not implemented | Docs only |
| Relay fabric discovery/health | Scaffolded | Helper first scope |
| DNS helper | Implemented first slice | Direct upstream, cache, AD-bit DNSSEC |
| SOCKS5 helper | Implemented first slice | Gatherlink UDP stream adapter and companion exit exist; needs supervisor/config integration |
| TCP forwarding helper | Implemented first slice | Gatherlink UDP stream adapter and companion exit exist; needs supervisor/config integration |
| WireGuard helper | Implemented first slice | Planning/config helpers and smoke coverage exist |
| Time helper | Implemented first slice | Narrow privileged helper |
| QUIC/WSS/TCP carriers | Placeholder | Future |
| Obfuscation profiles | Placeholder | Future |
| Peer failover | Docs/scaffold | v1 |
| Persistence | Docs mostly | Implementation needed |
| Test status | Green on current worktree | Rust passes; Python has 208 passing tests |

## Final Assessment

Gatherlink is in a promising and unusually disciplined early implementation
state. The core packet engine is real. The tests are meaningful. The docs have
become a serious design system. The architecture boundary is strong enough to
scale if it is protected.

The project should now resist adding broad new feature areas. The next win is
v1 integration depth: true two-VM proof, broader diagnostics producer coverage,
helper supervision, longer scheduler soak, and eventually authenticated
sessions.

If those land cleanly, v1 can build on a solid base: authenticated sessions,
signed topology, secure relays, real control context, peer failover, and carrier
expansion.
