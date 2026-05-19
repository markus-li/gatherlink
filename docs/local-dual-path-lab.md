# Local Dual-Path Lab

## Purpose

The first integration milestone is a simple, repeatable lab that runs two
Gatherlink instances on one Debian host and forwards normal UDP traffic through
two simulated WAN paths.

This lab exists before crypto, authentication, helper tunnels, adaptive
scheduling, or appliance packaging. It should make the core behavior visible
early: receive UDP, frame it, send it over selected paths, emit UDP on the far
side, and report what happened.

## Boundary

Gatherlink itself must not run as root.

Lab setup tools may run as root when they create Linux network namespaces, veth
pairs, addresses, routes, or `tc` shaping rules. Those privileged operations are
test-environment setup, not Gatherlink runtime behavior.

The lab must keep this separation obvious:

- root-owned lab setup creates and shapes the simulated network
- unprivileged Gatherlink processes run the client and server nodes
- normal UDP tools generate and measure traffic
- cleanup removes namespaces, veths, shaping, and temporary files

## Target Topology

```text
UDP generator
  -> client service listen
  -> gatherlink-client
       path-a: client namespace/link/subnet A -> server namespace/link/subnet A
       path-b: client namespace/link/subnet B -> server namespace/link/subnet B
  -> gatherlink-server
  -> UDP sink
```

The two paths must use different subnets so the lab can exercise path selection,
per-path counters, and Linux traffic shaping without pretending that two local
ports are two networks.

The exact IP plan can change, but the first implementation should reserve a
small documented range, for example:

- path A client side: `10.80.1.1/30`
- path A server side: `10.80.1.2/30`
- path B client side: `10.80.2.1/30`
- path B server side: `10.80.2.2/30`

IPv6 must be supported from day one. The lab may start with IPv4 paths for the
first shell script, but the topology and config model must not assume IPv4-only
addresses. A follow-up lab scenario should mirror the same shape with IPv6.

## Commands

The target user experience is one command to start the lab and one command to
stop the service:

```bash
just lab-up
just lab-down
```

Lab network cleanup is explicit so logs and status can be inspected after a
service is stopped. To stop the service and remove all lab-owned virtual
interfaces, run:

```bash
just lab-cleanup
gatherlink lab cleanup configs/lab/local-dual-path.json
```

`lab cleanup` deletes the lab network namespaces. The veth pairs, addresses,
qdisc shaping, MTU settings, and link state live inside those namespaces, so
namespace deletion removes the whole virtual testbed in one repeatable step.

The direct CLI command is:

```bash
gatherlink lab up configs/lab/local-dual-path.json
```

`lab up` prepares or reuses the simulated paths, prints their status, starts the
actual lab forwarder and sink services as unprivileged background processes,
writes PID/log files under the scenario runtime directory, and exits with both
services left running.

The root boundary is intentional: `lab up` may run as root for namespace/veth/tc
setup, but the service process it starts must not run as root. When launched
through `sudo`, the unprivileged service user is selected from `SUDO_USER`.

The lab interfaces are moved into Linux network namespaces, so they will not
appear in plain `ip a s` output from the default namespace. Use:

```bash
gatherlink lab interfaces configs/lab/local-dual-path.json
ip netns list
sudo ip -n glab-local-dual-path-client a s
sudo ip -n glab-local-dual-path-server a s
```

Additional commands should be small wrappers around normal Linux tools:

```bash
just lab-status
just lab-logs-tx
just lab-logs-rx
just lab-stats
just lab-shape PATH=path-a RATE=10mbit LOSS=2%
just lab-unshape PATH=path-a
```

Live shaping commands apply one-shot system settings to an existing lab. They do
not keep running; they configure the namespace/veth testbed while traffic and
Gatherlink continue to run:

```bash
gatherlink lab profiles configs/lab/local-dual-path.json
gatherlink lab network-modes configs/lab/local-dual-path.json
gatherlink lab apply-profile configs/lab/local-dual-path.json rate-10mbit
gatherlink lab apply-network-mode configs/lab/local-dual-path.json normal-saturated
gatherlink lab apply-network-mode configs/lab/local-dual-path.json forced-drop
gatherlink lab apply-network-mode configs/lab/local-dual-path.json clean-50mbit
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/rate-limit-pair.json
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/sink-view-asymmetric-2x3-1x2.json
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/remote-loss-local-clean.json
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/clear-all.json
gatherlink lab shape configs/lab/local-dual-path.json path-a --rate 5mbit --loss 2%
gatherlink lab shape-sink-view configs/lab/local-dual-path.json path-a --up 2mbit --down 3mbit
gatherlink lab shape configs/lab/local-dual-path.json path-b --mtu 1200
gatherlink lab shape configs/lab/local-dual-path.json path-a --state down
gatherlink lab clear-shape configs/lab/local-dual-path.json path-a
```

These commands may use root because they call `ip` and `tc` against the lab
network. They are not Gatherlink service behavior.
`shape-sink-view` is a convenience wrapper for asymmetric path tests: from the
sink perspective, download traffic is sent by the local/client egress and upload
reply traffic is sent by the remote/server egress. The wrapper applies those two
one-way rates on the correct sending sides so asymmetric tests are repeatable
without remembering namespace/interface direction.

The first self-contained traffic check uses normal UDP sockets and does not need
root once the lab service is already running:

```bash
gatherlink lab smoke configs/lab/local-dual-path.json
```

When simulated paths are configured, `lab up` runs the forwarder inside the
client namespace and the sink inside the server namespace. `lab send` injects
traffic inside the client namespace too, so packets traverse the veth pair and
any live `tc` shaping before they reach the sink. The process still runs as the
unprivileged service user; the lab wrapper uses `sudo ip netns exec` only to
enter the test namespace.

For live packet visibility, attach to both lab services in separate terminals:

```bash
gatherlink services attach lab.local-dual-path
gatherlink services attach lab.local-dual-path.sink
```

For a quieter continuously refreshed counter view, use aggregate mode:

```bash
gatherlink services attach lab.local-dual-path --mode aggregate
gatherlink services attach lab.local-dual-path.sink --mode aggregate
gatherlink services monitor lab.local-dual-path lab.local-dual-path.sink
```

Aggregate mode uses 1024-based human-readable units by default for `bytes` and
`speed`. Press `h` while it is running to toggle between human units and raw
byte counters. Press `b` to toggle speed between bit/s and byte/s. Press `q` to
quit, or use Ctrl-C. Press `m` to toggle human units between binary
(`KiB`, `MiB`, `Kibit/s`, `Mibit/s`) and decimal network units
(`KB`, `MB`, `Kbit/s`, `Mbit/s`).

Then inject traffic from another terminal or any UDP generator:

```bash
gatherlink lab send configs/lab/local-dual-path.json --count 10 --payload hello
```

After at least one packet has reached the sink, the sink has learned the
forwarder source addresses for each path. You can then generate reverse lab
traffic from the sink side over those learned paths:

```bash
gatherlink lab send configs/lab/local-dual-path.json \
  --direction from-sink \
  --count 10 \
  --payload reply
```

To exercise both directions from one command, send an initial to-sink burst and
then a sink-originated reply burst:

```bash
gatherlink lab send configs/lab/local-dual-path.json \
  --direction both \
  --count 10 \
  --payload duplex
```

For a clean rate test, use the same command with duration, bandwidth, and payload
size options:

```bash
gatherlink lab shape configs/lab/local-dual-path.json path-a --rate 3.2mbit --side both
gatherlink lab shape configs/lab/local-dual-path.json path-b --rate 1.7mbit --side both
gatherlink lab send configs/lab/local-dual-path.json \
  --payload rate7 \
  --duration 20 \
  --bandwidth 7mbit \
  --payload-size 1200 \
  --count 1 \
  --interval 0
```

With the current round-robin scheduler this intentionally overdrives the two
paths. The forwarder should report the attempted send rate, while the sink and
`tc -s qdisc show` reveal the shaped delivered rate and any drops on each path.
The lab exposes this as named network modes:

```bash
gatherlink lab apply-network-mode configs/lab/local-dual-path.json normal-saturated
gatherlink lab apply-network-mode configs/lab/local-dual-path.json forced-drop
gatherlink lab apply-network-mode configs/lab/local-dual-path.json clean-50mbit
```

`normal-saturated` behaves like a normal saturated link: excess traffic may
queue and arrive later as latency, out-of-order delivery, or reorder pressure
rather than immediately dropping. `forced-drop` uses the same rates with small
netem queue limits so stress tests show drop counters quickly. The equivalent
ad-hoc commands are:

```bash
gatherlink lab shape configs/lab/local-dual-path.json path-a --rate 3.2mbit --limit 32 --side both
gatherlink lab shape configs/lab/local-dual-path.json path-b --rate 1.7mbit --limit 16 --side both
```

That tighter qdisc limit is a lab mode, not the production scheduling queue.
Production Gatherlink should keep its own small bounded FIFO scheduler queue so
packets can be redistributed across eligible paths before being dropped. It
should not retransmit ordinary UDP; if the bounded queue overflows or a packet
ages out, Gatherlink reports the drop and lets the application protocol decide
whether loss matters.

The forwarder log shows the packet arriving at Gatherlink and being sent onward
over a selected path. The sink log shows the far-side UDP receive event and
payload. Aggregate mode shows local-view transmit and receive packet counts,
byte counts, current rates, missed packet counters, reorder counters, and other
status fields when the running service reports them. Rust-owned dataplane
services should expose their native speed, loss, and reorder counters through
the same IPC status shape.
External tools such as `nc -u`, `socat`, and `iperf3 -u` can be used against the
same addresses.

The lab traffic at the user edge remains normal UDP. The internal path traffic
between the simulated peers is encapsulated in the Gatherlink v1 data frame:
the frame header carries `sequence` and `path_id`, while the original UDP
payload stays untouched inside the frame. That is the same boundary the Rust
dataplane should use, so reorder and missing-packet counters are driven by the
Gatherlink protocol metadata rather than by any test-only payload format.
The forwarder also sends v1 control metaband `PathMetadata` frames so the sink
can name per-path rows from peer-provided control information instead of relying
only on local lab config. The lab refreshes this metadata periodically because
the current control metaband rides over UDP and a receiver may miss the first
startup frame.
Service status includes a `control_metadata` block for both sent and received
control activity: frame count, message count, byte count, last send/receive
time, last source, the current path-id/name table, and directional path capacity
estimates. Lab path capacity starts from each path's `default_max_speed`
setting, falls back to shaping `rate`, and is cached in the runtime directory
after detection sees a meaningful change. The first detector watches the local
TX side using real qdisc counters and observed payload flow. It raises capacity
gradually only after sustained higher average throughput, and lowers capacity
only after at least 60 seconds of sustained lower average throughput while the
path is also reporting drops. Without drops, lower throughput means the lab did
not prove it was sending above the path maximum. Changed `tx_bps`/`rx_bps`
metadata is sent sparsely over the control metaband. The monitor keeps the main
table focused on traffic counters, then renders service time/control and path
control sections below it. Service time/control rows show aggregate control
metadata plus `sys`, `gl`, and `ntp` once per service. The service control table
keeps each logical control signal in its own compact column: `ctx`/`crx` for
aggregate control TX/RX, `clock` for the internal Gatherlink clock offset/RTT,
`paths` for the known path table size, `cap` for capacity metadata, `lat` for
latency metadata, and `last` for the latest control activity time. Path control
rows use the same split at path scope: `ctx`/`crx` are per-transport-path
control TX/RX, `cap` is directional capacity, and `lat` is directional latency.
Control payloads are duplicated over every eligible path with the same control
sequence so path loss, asymmetry, and metadata reachability are visible without
relying on ordinary data scheduling.
Control metadata uses the shared production control cadence policy: startup and
active data traffic keep the fast refresh interval so path and clock facts
converge quickly, while an established lab with no new data traffic backs off
to sparse refreshes of about 60 seconds. `gatherlink services monitor` is not
lab-specific; it requests temporary monitor cadence from each watched service.
If that request is not refreshed for 120 seconds, the service falls back to the
baseline service/scheduler cadence. The monitor refreshes the request before it
expires, currently every 60 seconds, so service diagnostics stay live while the
monitor is attached without sending IPC requests every redraw.
When the sink receives a framed lab packet it sends a same-size framed reply on
the same path socket. Those reply packets are not forwarded back to the user
UDP source yet; they exist to exercise the reverse path with normal Gatherlink
data frames, let the peer advertise its own TX capacity, and let the receiving
side display that as local RX capacity.
The forwarder also records reply RTT by matching the returned Gatherlink
sequence number to the original send time. The lab reports half of that RTT as
an early one-way current and rolling mean latency for both directions on the
same path. This is intentionally traffic-derived rather than synthetic test
data, and it is sent over the same control metaband as capacity so Python can
later feed scheduler weights and reorder timing from one service-status shape.
NTP clock truth belongs to the sink host/process role, and external time-source
queries stay outside the Gatherlink connection. The sink process first queries
direct UDP NTP and advertises that NTP-derived Unix time over the control
metaband. If direct NTP is unavailable, it may use a lower-confidence HTTPS
`Date` header fallback labeled `https-date`. If no external source is available,
it falls back to local system wall time and reports the host's NTP
synchronization state. Gatherlink itself does not step the OS clock; standard
platform services such as `systemd-timesyncd`, `chrony`, or `ntpd` remain
responsible for system clock discipline.
For process-internal time, the lab treats the sink side as authoritative and
runs a lightweight NTP-style exchange over the same control metaband. The
forwarder sends an origin timestamp from its monotonic process clock; the sink
stamps receive/transmit time from its own internal monotonic clock and replies
on the same path. The forwarder computes peer-relative clock offset and RTT in
Python. Host clock discipline remains separate from this internal exchange;
Gatherlink's internal offset is the value later crypto/replay/sliding-window
policy should consume.
Until full directional latency confidence is available, the non-sink side
applies sink time with half of the rolling internal-clock RTT as the one-way
correction. That keeps the estimate close while preserving a clean upgrade path
to per-path current/mean latency confidence and scheduler decisions.
Reorder hold policy is configured per node pair, with a default maximum hold of
`150ms`. Python owns the policy model: it should compute a hold window from
measured path latency skew plus a margin, clamp it using Python's default `2ms`
minimum and the per-node-pair maximum, and compile the resulting execution
value down to Rust. Rust must not hard-code that minimum; if Python later
chooses `0ms` or `1ms` for a service, Rust should execute that compiled value.

Aggregate and monitor columns use full names when they fit without widening the
table, and short names when the full title would make the live view noisy. The
main table is intentionally limited to traffic and lifecycle data; control and
time details live in compact secondary tables so path rows do not waste columns
on service-level facts.

Services that report per-path counters render child rows named `path:<name>`
under the parent service. These rows use the same packet, byte, and speed
columns, letting the local lab show `path-a` and `path-b` throughput separately
while preserving the service-level total.

- `service`: registered service name from the local Gatherlink service registry
- `state`: lifecycle state reported by IPC, or `systemd` / `ipc_error`
- `txp` / `rxp`: packets transmitted and received from the local service or
  path point of view
- `txB` / `rxB`: payload data transmitted and received,
  rendered in 1024-based units by default; press `m` to toggle 1000-based units
- `tx/s` / `rx/s`: current transmit and receive rates, sampled from payload
  byte deltas unless Rust reports native current speeds; press `b` to toggle
  bit/s and byte/s, and `m` to toggle `Mibit/s` vs `Mbit/s`
- `miss`: packets known missing or lost; `-` until exposed by the
  running service/dataplane
- `ooo`: packets received out of order; `-` until exposed by the running
  service/dataplane
- `reord`: packets that required reorder buffering; `-`
  until exposed by the running service/dataplane
- `context`: service-specific context such as target address, listen address,
  last source address, latest payload size, or systemd unit

The `service time/control` section shows:

- `sys`: local system wall time from service status, with monitor time as a
  fallback
- `gl`: Gatherlink time derived from sink-authoritative control metadata,
  including the latest sink sent time and local receive time when available
- `ntp`: sink-side NTP synchronization status reported over the control
  metaband; direct NTP sources are shown as `state/source`
- `ctx` / `crx`: aggregate control frames and bytes transmitted/received by
  the service
- `clock`: internal clock sync state as
  `clk=off<current>/<mean> rtt=<latest> n=<samples>` or `clk=sink` when the
  service is the authoritative sink
- `paths`: number of path-id/name mappings learned through control metadata
- `cap`: compact capacity metadata summary
- `lat`: number of paths with latency metadata
- `last`: latest control send/receive activity time

The `path control` section shows per-path control metadata:

- `ctx` / `crx`: control frames and bytes transmitted/received over that path
- `cap`: latest local-view detected `tx` / `rx` capacity
- `lat`: local transmit and receive current/mean latency pairs after
  Python has converted peer-view metadata into local-view state

Standalone shaping configs live under `configs/lab/shaping/`. They can be
applied as named testbed states and may target `local`, `remote`, or `both` veth
ends for each path. `local` maps to the client-side namespace/interface in the
single-host lab, and `remote` maps to the server-side namespace/interface. This
keeps the config shape useful for future multi-host labs where local and remote
shaping are applied separately.
Asymmetric sink-view configs should name their intent explicitly and encode the
same egress mapping as `shape-sink-view`, for example
`configs/lab/shaping/sink-view-asymmetric-2x3-1x2.json`.

The first implementation may put the real work in a Python script, for example
`scripts/lab/local_dual_path.py`, with `just` recipes as the stable entrypoint.

`gatherlink lab plan ...` remains a dry-run command. It should not mutate the
host or start services.

Top-level service aliases are available for the default local lab config:

```bash
gatherlink up
gatherlink status
gatherlink down
```

Running labs are also registered in the shared service registry so logs and
lifecycle controls are available from one place:

```bash
gatherlink services list
gatherlink services status lab.local-dual-path
gatherlink services status lab.local-dual-path.sink
gatherlink services attach lab.local-dual-path
gatherlink services attach lab.local-dual-path.sink
gatherlink services attach lab.local-dual-path --mode aggregate
gatherlink services attach lab.local-dual-path.sink --mode aggregate
gatherlink services monitor lab.local-dual-path lab.local-dual-path.sink
gatherlink services logs lab.local-dual-path --follow
gatherlink services logs lab.local-dual-path.sink --follow
gatherlink services close lab.local-dual-path
gatherlink services close lab.local-dual-path.sink
```

`lab down` remains the lab-aware service stop command. `lab cleanup` is the
lab-aware virtual network cleanup command. `services close` is the shared
process stop command for any process-managed Gatherlink service; it does not
remove namespaces or other lab testbed resources.

The registry is folder-discovered under `.gatherlink/services/`: each service
gets its own directory with `service.json` for identity/config metadata and
`current.pid` for process-managed services. Process-managed services also expose
`control.sock` for live status and graceful stop requests.

Systemd-owned services should be marked instead of detached by Gatherlink:

```bash
gatherlink services register configs/lab/local-dual-path.json --systemd
gatherlink services list
gatherlink services attach lab.local-dual-path
```

In this mode the registry shows the service as `manager=systemd`, and log
attachment runs `journalctl` in the foreground. Direct close is intentionally
left to systemd lifecycle tools.

## Scenario Config

Lab behavior should be driven by an extensible scenario file, not hardcoded into
one script. The first scenario config should live at
`configs/lab/local-dual-path.json`.

The config should be able to describe future scenarios from
`docs/testing-strategy.md` even when they are not implemented yet. Unsupported
features must appear in the lab plan as `not_implemented` instead of being
silently ignored.

The shape should include:

- scenario name and kind
- security mode
- nodes to launch, including the user that should run Gatherlink
- simulated paths, addresses, and subnets
- traffic generator and sink settings
- requested shaping such as rate, delay, jitter, loss, reorder, blackhole, and recovery
- named live shaping profiles that can be applied to running labs
- requested future features such as WSS fallback, MTU mismatch, receiver metrics, peer failover, DNS racing, or bootstrap variants

The first implementation only needs to plan the local dual-path plaintext UDP
lab. Namespace setup, traffic shaping, process launch, and traffic generation
can initially report `not_implemented` until their commands are added.

## Traffic Tools

The lab should work with standard UDP tools:

- `iperf3 -u` for throughput and packet loss
- `socat` or `nc -u` for simple payload checks
- `tcpdump` for path visibility
- `tc netem` and `tc tbf` for delay, jitter, loss, reorder, and rate limits

If a tiny built-in UDP generator is added later, it should be a convenience, not
a replacement for standard tools.

## First Milestone Behavior

Before crypto and authentication, the lab may run in `security.mode = "none"`.
That mode is intentionally unsafe and must emit loud warnings in terminal output
and logs.

The first runnable lab should prove:

- two unprivileged Gatherlink processes can start on one host
- a UDP payload can enter the client-side service
- the payload can cross the Gatherlink frame boundary
- the server-side process can emit the original UDP payload
- counters show packets and bytes moving
- terminal logs show service bind, path registration, forwarding, and shutdown

Multipath scheduling can start simple. A fixed path or fixed round-robin is
acceptable for the first lab as long as the behavior is explicit in logs and
config. Adaptive scheduling should wait until receiver metrics are trustworthy.

## Cleanup Requirements

The lab must be safe to rerun.

`lab-down` should remove namespaces, veth pairs, shaping rules, temporary pid
files, logs, and sockets created by `lab-up`. It should tolerate partially
created state from failed runs.

Gatherlink configs and source files must not be generated into untracked random
locations. Generated lab runtime files should live under a predictable ignored
directory such as `.lab/`.
