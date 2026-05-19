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
gatherlink lab apply-profile configs/lab/local-dual-path.json rate-10mbit
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/rate-limit-pair.json
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/remote-loss-local-clean.json
gatherlink lab apply-shape-config configs/lab/local-dual-path.json configs/lab/shaping/clear-all.json
gatherlink lab shape configs/lab/local-dual-path.json path-a --rate 5mbit --loss 2%
gatherlink lab shape configs/lab/local-dual-path.json path-b --mtu 1200
gatherlink lab shape configs/lab/local-dual-path.json path-a --state down
gatherlink lab clear-shape configs/lab/local-dual-path.json path-a
```

These commands may use root because they call `ip` and `tc` against the lab
network. They are not Gatherlink service behavior.

The first self-contained traffic check uses normal UDP sockets and does not need
root once the lab service is already running:

```bash
gatherlink lab smoke configs/lab/local-dual-path.json
```

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
quit, or use Ctrl-C.

Then inject traffic from another terminal or any UDP generator:

```bash
gatherlink lab send configs/lab/local-dual-path.json --count 10 --payload hello
```

The forwarder log shows the packet arriving at Gatherlink and being sent onward
over a selected path. The sink log shows the far-side UDP receive event and
payload. Aggregate mode shows packet count, byte count, current byte rate,
missed packet counters, reorder counters, and other status fields when the
running service reports them. Rust-owned dataplane services should expose their
native speed, loss, and reorder counters through the same IPC status shape.
External tools such as `nc -u`, `socat`, and `iperf3 -u` can be used against the
same addresses.

Aggregate and monitor columns use full names when they fit without widening the
table, and short names when the full title would make the live view noisy:

- `service`: registered service name from the local Gatherlink service registry
- `state`: lifecycle state reported by IPC, or `systemd` / `ipc_error`
- `pkts`: total packets observed by that service since it started
- `bytes`: total payload data observed by that service since it started,
  rendered in 1024-based units by default
- `speed`: current rate, sampled from payload byte deltas unless Rust reports a
  native current speed; press `b` to toggle bit/s and byte/s
- `miss`: packets known missing or lost; `-` until exposed by the
  running service/dataplane
- `ooo`: packets received out of order; `-` until exposed by the running
  service/dataplane
- `reord`: packets that required reorder buffering; `-`
  until exposed by the running service/dataplane
- `context`: service-specific context such as target address, listen address,
  last source address, latest payload size, or systemd unit

Standalone shaping configs live under `configs/lab/shaping/`. They can be
applied as named testbed states and may target `local`, `remote`, or `both` veth
ends for each path. `local` maps to the client-side namespace/interface in the
single-host lab, and `remote` maps to the server-side namespace/interface. This
keeps the config shape useful for future multi-host labs where local and remote
shaping are applied separately.

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
