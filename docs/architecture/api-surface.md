# API Surface

## Purpose

This document defines intended public API boundaries.

## Public Python API

The Python package may expose config load/validate/build, runtime supervisor,
diagnostics event models, helper interfaces, lab tooling, and CLI entrypoints.
The public API should stay smaller than the internal module tree.

## Rust/Python boundary

Python should talk to Rust through a narrow backend API: start, stop,
add/update/remove service, add/update/remove path, update compiled scheduler
policy, snapshot stats, and subscribe/poll events if needed.

Rust should not expose low-level packet operations to Python.

## CLI

The CLI should expose validate config, build config pair, run, stats, path
test/retry/scan, lab create/destroy, secrets seal/open, and time status.

## Local diagnostics API

Diagnostics may be exposed through local WebSocket, JSONL, stdout, and future
HTTP/Unix socket API. Default must be local-only.

## Experimental REST API Helper

V1 should prepare for a REST API that can cover the same control surface as the
CLI, but it is an optional helper/control-plane sidecar, not core runtime or
dataplane logic.

Rules:

- start with `gatherlink helpers status-http`
- bind to `127.0.0.1` by default
- require an explicit danger flag for any non-loopback bind
- mark the server and docs as `EXPERIMENTAL`
- never return secret key material
- derive responses from the same structured facts used by CLI/status output
- keep CLI as the primary supported control surface

The current helper exposes read-only status on `/json` and `/v1/status`.
Writable REST APIs are allowed later in v1, but only with an expiry guard:

- writes are enabled only when started explicitly from the CLI
- write operations stop working after one hour by default
- restarting the REST helper from the CLI resets the write window
- read-only operations may continue after the write window expires
- the API must report whether writes are enabled and when they expire
- until concrete write endpoints are added, mutation requests must fail closed

The goal is to make local automation and future UI work possible without
turning unauthenticated HTTP into a long-lived remote management surface.

## Stability

Mark APIs explicitly as internal, experimental, or stable. Do not accidentally
freeze internal helper APIs too early.
