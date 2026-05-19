# Performance Philosophy

## Purpose

Gatherlink targets serious userspace performance without prematurely entering
kernel/XDP/DPDK complexity.

## Target

Initial serious target: 1 Gbit/s easy, 2.5 Gbit/s realistic, 5 Gbit/s realistic
on good hardware, and 10 Gbit/s possible with tuning and suitable hardware.

## Userspace first

Userspace Rust is preferred because it is safer, debuggable, portable, non-root
friendly, open-source friendly, and good enough for target use cases.

## Avoid Python in dataplane

Python must not process packets in the hot path. Python computes active paths,
weights, policy, and state. Rust executes receive, wrap, choose from compiled
state, send, unwrap, dedupe, reorder, and count.

## Main bottlenecks

Expected bottlenecks are small-packet packet rate, copying/allocation,
TLS/WSS/QUIC crypto, queue contention, metrics overhead, NIC interrupts, weak
CPUs, bad NICs, and thermal throttling.

## Rust expectations

Prefer preallocated buffers, bounded queues, minimal locking, cheap counters,
batching later, sendmmsg/recvmmsg later where useful, and io_uring only if
justified.

## Metrics cost

Metrics are mandatory, but hot-path metrics must be cheap. Avoid per-packet
allocation, per-packet string formatting, and per-packet Python callbacks.

## Kernel future

Kernel/XDP is not v0.9. If ever needed, it should be an optional acceleration
backend behind the same dataplane boundary.
