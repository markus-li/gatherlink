# Architecture

Gatherlink is a Python-controlled, Rust-executed UDP transport.

The core shape is:

```text
local UDP service -> Rust packet executor -> one or more UDP carrier paths -> Rust packet executor -> remote UDP target
```

Python owns the parts that need product meaning:

- config loading and validation
- runtime expansion
- service lifecycle
- helper behavior
- diagnostics and operator messages
- scheduler intelligence and policy decisions
- control metadata semantics

Rust owns the parts that need packet-speed execution:

- compact frame parsing and encoding
- UDP carrier sockets
- AEAD, replay, and receiver-index checks
- batching, fragmentation, dedupe, and bounded queues
- counters and cheap scheduling primitives

The boundary is intentionally strict. Rust can execute configured primitives,
but Python decides what those primitives mean and when they should change.
Helpers are also Python-owned unless a future performance profile proves that a
small helper-specific primitive belongs in Rust.

For the full contract, read [`docs/architecture/architecture-contract.md`](architecture-contract.md).
