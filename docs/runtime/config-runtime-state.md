# Config And Runtime State

## Purpose

Gatherlink has two config shapes:

- human-facing config that operators write and review
- compiled runtime state that Python gives to Rust and helpers

The human config may contain names, defaults, helper policy, warnings, and
operator-friendly structure. Runtime state is compact, explicit, and already
validated.

## Human-facing config

User config should stay human-friendly and parseable by Python.

It may use names such as peers, paths, services, helpers, and profiles. Python
normalizes schema versions, expands defaults, validates relationships, and
assigns compact runtime ids.

Explicit numeric ids are allowed for advanced/debug use, but discouraged and
not recommended. Generated ids reduce collisions, accidental policy reuse, and
operator confusion. If explicit ids are used, validation must reject collisions
and diagnostics should make the manual choice visible.

## Runtime contract

Rust receives only compiled runtime state.

Compiled runtime state should contain primitives such as:

- peer/session ids and receiver indexes
- service ids
- path ids
- scheduler state, weights, and MTU
- replay window and counter parameters
- helper/service runtime permissions
- relay-hop session authorization
- diagnostic event codes and labels

Runtime state should not contain human names as policy authority. Names may be
included only as diagnostic labels if they cannot affect packet-time decisions.

Rust must not receive unresolved policy names such as "prefer home", "guest
exit", or "allow helper X" and decide what they mean. Python resolves those into
explicit runtime values first.

## Live reload

Configuration changes should be live-reloadable.

Python owns loading, validation, diffing, and apply order. A reload should
compile a new runtime state generation, push it to Rust/helpers, and keep the
old generation alive only where needed to drain in-flight traffic safely.

Reload rules:

- invalid config keeps the last good generation active
- new generation is applied atomically at the runtime boundary where possible
- stale control messages for old generations are ignored
- removed services stop accepting new traffic
- removed sessions/paths drain or close according to policy
- diagnostics explain what changed and what was rejected

## Introspection

Generated runtime state should be inspectable.

The first format should be JSON so tests, scripts, and operators can compare
what Python compiled without reading internal Python objects. Future terminal
views may render the same state as tables or summaries.

Useful commands should include:

```bash
gatherlink config show --canonical path/to/config.json
gatherlink config show --runtime path/to/config.json
gatherlink config show --runtime --json path/to/config.json
```

The JSON view is the source for automation. Human terminal views are derived
from the same data.

## Secrets

Human-facing config may reference secrets or sealed bundles, but runtime
introspection must not dump private keys, session keys, bootstrap secrets, or
plaintext provisioning material.
