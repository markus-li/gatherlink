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

## Local REST API Helper

The local REST API is a protected operator surface for local tools and future UI
work. It is still an optional helper/control-plane sidecar, not core runtime or
dataplane logic.

Rules:

- start with `gatherlink helpers status-http`
- bind to `127.0.0.1` by default
- require an explicit danger flag for any non-loopback bind
- require API-key authentication for status and write endpoints
- store and compare API keys as hashes, not plaintext runtime state
- never return secret key material
- derive responses from the same structured facts used by CLI/status output
- keep CLI as the primary supported control surface

The current helper exposes authenticated status on `/json` and `/v1/status`,
plus one guarded local write endpoint: `POST /v1/services/{name}/close`.
Writable REST APIs must keep an expiry guard:

- all requests must include `Authorization: Bearer <key>` or
  `X-Gatherlink-Api-Key: <key>`
- writes are enabled only when started explicitly from the CLI
- write operations stop working after one hour by default
- restarting the REST helper from the CLI resets the write window
- read-only operations may continue after the write window expires
- the API must report whether writes are enabled and when they expire
- the close endpoint maps only to the existing service registry close behavior
- unknown or expired mutation requests fail closed and emit structured
  diagnostics

The goal is to make local automation and future UI work possible without
turning protected local HTTP into a WAN-facing remote management surface.

## Stability

Mark APIs explicitly as internal, experimental, or stable. Do not accidentally
freeze internal helper APIs too early.
