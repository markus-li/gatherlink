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
batching, `sendmmsg`/`recvmmsg` where useful, and io_uring only if justified.
Batching applies on both sides of the carrier boundary: Gatherlink should batch
application UDP emits after decode and batch encoded carrier-frame sends when
several already-planned frames share one path endpoint.

The production runner should use large nonblocking drain budgets. A larger
budget does not wait for packets; it only lets Rust drain a hot socket before
returning to Python-owned control work. Small smoke-test budgets are valid for
tests, but they should not be the service default.

## Metrics cost

Metrics are mandatory, but hot-path metrics must be cheap. Avoid per-packet
allocation, per-packet string formatting, and per-packet Python callbacks.
Summary-mode counters should aggregate packet/byte counts in Rust before
returning to Python; terminal views can expand those structured facts later.

## Kernel Future

Kernel/XDP remains future-only unless a release roadmap promotes it. If ever
needed, it should be an optional acceleration backend behind the same dataplane
boundary defined in [`docs/architecture/architecture-contract.md`](architecture-contract.md).

## High-throughput host tuning

High-throughput UDP tests are packet-rate sensitive. The Rust dataplane can
batch Gatherlink carrier frames when the compiled path MTU has room, but the
host kernel must still have enough UDP queue memory to absorb short bursts.
The path receiver must also drain carrier sockets fairly; a stable poll order
can make one otherwise healthy path become the overflow point under bursty
load.
Small carrier packets and jumbo/coalesced carrier packets put different kinds
of pressure on the receiver. It is valid for Rust to tune socket-drain quanta
from compiled path facts, as long as this remains execution fairness and not
Python-owned routing policy.

For Debian/Linux labs that intentionally push Gbit/s-class UDP, verify:

- path interfaces use normal MTU for ordinary acceptance and jumbo MTU only
  when the physical/virtual network supports it end to end
- `net.core.rmem_max` and `net.core.wmem_max` are high enough for the socket
  buffer sizes requested by Rust
- `net.ipv4.udp_mem` is high enough that global UDP memory pressure does not
  drop packets before Gatherlink can drain them
- `/proc/net/snmp` `Udp` counters are captured before/after tests so
  `RcvbufErrors` and `MemErrors` are not mistaken for protocol loss

Jumbo MTU is not a correctness requirement. It is a performance lever: with
VPN-sized UDP payloads, normal 1500-byte links usually carry one payload per
Gatherlink carrier datagram, while jumbo links can coalesce several payloads
into one authenticated carrier packet. If jumbo improves throughput
dramatically, the bottleneck is packet rate rather than the byte path.

Measured performance evidence lives in [`docs/benchmarks/README.md`](../benchmarks/README.md) and
[`docs/benchmarks/hyperv-performance-log.md`](../benchmarks/hyperv-performance-log.md). Keep exact result tables there so
architecture docs do not become a second benchmark ledger.

Current high-level reading:

- conservative carrier MTUs are packet-rate expensive at multi-Gbit/s rates;
  use a path MTU close to the real link MTU when the network supports it
- raw Gatherlink UDP is healthy enough that remaining high-rate work should be
  driven by measured bottlenecks, not broad redesign
- WireGuard-over-Gatherlink is the hard case because ordered nested traffic can
  lose useful throughput when blindly striped over multiple paths
- scheduler work belongs in Python policy compiled into cheap Rust primitives;
  Rust must not infer service meaning or add hidden reliability
- direct WireGuard parity, kernel/XDP acceleration, and ordered single-flow
  aggregation are not claims unless benchmark evidence proves them

Do not reopen performance work by guessing at constants. Revisit it only with a
profile, comparable baselines, and a specific bottleneck. The usual next
questions are syscall count, datagrams per second, AEAD cost, app-facing socket
handoff, CPU/socket locality, and whether an ordered service should use fewer
paths rather than more.

## Research Backlog And Guardrails

Detailed active scheduler behavior lives in [`docs/runtime/scheduler.md`](../runtime/scheduler.md).
Benchmark method and measured results live in `docs/benchmarks/`. Future
optimization ideas that are not assigned to the active release belong in
[`docs/reports/future-roadmap-pipeline.md`](../reports/future-roadmap-pipeline.md).

Performance work may study MPTCP ECF/BLEST, Linux MPTCP path management,
Multipath QUIC feedback, WireGuard batching/locality, UDP GSO/GRO-like effects,
and SCTP-CMT receive-buffer blocking. Translate useful ideas into Python policy
and Rust execution primitives; do not import TCP reliability or move product
policy into Rust.

Do not optimize by crossing these lines:

- no retransmission, reinjection, or hidden reliability for ordinary UDP
  payloads
- no payload inspection, including TCP ACK inference inside WireGuard or other
  encrypted helpers
- no Rust-owned business policy, helper behavior, service meaning, environment
  policy, or semantic control branches
- no kernel/XDP dataplane unless a future release roadmap explicitly promotes
  it with a separate design
- no claims of WireGuard compatibility or direct-WireGuard parity without
  matching one-path, three-path, relay, raw Gatherlink, and
  WireGuard-over-Gatherlink benchmark evidence
- no new packet-header fields for scheduler policy unless every compact-header
  alternative has been exhausted and documented
- no broad QUIC/WSS/TCP product pivot; these are carriers and helpers around
  the Gatherlink UDP transport, not replacements for it

## VPN Payload Classification Decision

We discussed making Gatherlink more aware of WireGuard-carried traffic by
owning or inspecting VPN-side packets so the scheduler could identify inner TCP
streams. That path is rejected for now.

The reason is architectural, not language performance. Rust userspace
WireGuard implementations such as GotaTun and BoringTun are useful benchmark
comparisons. In the 2026-05-25 Hyper-V VM baselines, both tested Rust
userspace WireGuard clients were materially slower than `wireguard-go` for
simultaneous TCP, roughly around half of the `wireguard-go` per-path result in
that clean test shape. That evidence is a reason to keep `wireguard-go` as the
primary userspace WireGuard comparison baseline for now, not a reason to
rewrite Gatherlink's Rust dataplane in Go. Gatherlink's raw Rust dataplane has
separate high-throughput evidence; the WireGuard-client result is about those
specific VPN packet paths and implementation maturity.

Adopting or embedding one of those clients would make Gatherlink responsible
for VPN protocol behavior, key lifecycle, replay semantics, packet parsing,
helper policy, and edge-case compatibility. That would blur the current product
boundary: Gatherlink is a UDP transport and helper orchestration layer, not a
WireGuard implementation.

The useful part of the idea remains valid: the scheduler benefits from knowing
whether a service carries TCP-like ordered traffic, UDP bulk traffic,
latency-sensitive traffic, or control traffic. Gatherlink should get that
classification from configuration, helpers, or OS policy before encryption
where possible, not by decrypting or inspecting encrypted helper payloads.

Gatherlink now has a Python-owned service traffic-class model:

- `tcp_ordered`: ordered, loss-sensitive traffic such as a stable WireGuard
  service carrying mostly TCP
- `udp_bulk`: high-throughput datagram traffic that can tolerate path changes
  better than a TCP flow
- `latency_sensitive`: low-latency traffic that should prefer low-delay paths
  and avoid deep queues
- `control`: Gatherlink control or helper metadata with explicit reliability
  and fanout rules
- `unknown`: default class when a helper or operator has not provided better
  information

Helpers may map classes from explicit service config, separate WireGuard
interfaces, local firewall marks, nftables/conntrack policy, DSCP, or policy
routing. Python owns the meaning and compiles per-service policy. Rust receives
only cheap execution primitives such as path eligibility, weights, priority,
queue budget, pacing, and duplication/fanout behavior.

Example lab-only tuning values used during Hyper-V performance work:

```bash
sudo sysctl -w net.core.rmem_max=268435456
sudo sysctl -w net.core.wmem_max=268435456
sudo sysctl -w net.core.rmem_default=8388608
sudo sysctl -w net.core.wmem_default=8388608
sudo sysctl -w net.ipv4.udp_mem="262144 524288 786432"
```

For jumbo or multi-Gbit/s experiments, the Hyper-V lab has also used:

```bash
sudo sysctl -w net.core.rmem_max=2147483647
sudo sysctl -w net.core.wmem_max=2147483647
sudo sysctl -w net.core.rmem_default=16777216
sudo sysctl -w net.core.wmem_default=16777216
sudo sysctl -w net.ipv4.udp_mem="1048576 2097152 4194304"
```

These are host/operator tuning values, not protocol constants. Production
packages should surface warnings or doctor checks when the host caps are too
small for requested throughput, rather than silently changing system policy.
The Rust carrier and app-facing service sockets request large buffers so a
tuned host can absorb short VM/NIC bursts, but Linux still caps the effective
value with the sysctl limits above.

For local source builds where the binary will run on the same CPU family it was
built on, use:

```bash
RUSTFLAGS="-C target-cpu=native" maturin develop --release
```

This is not suitable for portable wheels, but it is appropriate for VM/lab
baselines where Gatherlink is compared to locally built or kernel-native
transports on the same host.
