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

## Stability

Mark APIs explicitly as internal, experimental, or stable. Do not accidentally
freeze internal helper APIs too early.
