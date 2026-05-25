# Benchmarks

Benchmarks are operational evidence, not release promises. Keep them separated
from product docs so tuning work can stay honest about what was measured, where
it was measured, and what changed between runs.

Use this collection for:

- repeatable benchmark strategy and command patterns
- current pass thresholds and performance targets in `thresholds.md`
- current Hyper-V VM lab measurements in `hyperv-performance-log.md`
- historical Hyper-V VM lab measurements in `hyperv-performance-history.md`
- current WireGuard-over-Gatherlink status and struggles in
  `wireguard-over-gatherlink-status.md`
- external aggregation product/user speed signals and proposed near-apples
  comparison profiles in `external-aggregation-comparison.md`
- baseline comparisons between plain LAN, WireGuard, Gatherlink, and combined
  WireGuard-over-Gatherlink scenarios
- bottleneck notes backed by counters, CPU observations, loss, retransmits, or
  controlled A/B runs

Do not use these notes as protocol requirements. If a benchmark proves a design
change is needed, promote the design change into the architecture/runtime docs.

## Tooling

The Hyper-V benchmark entrypoints are:

- `tools/hyperv/run_private_lan_speed.sh`: raw private-LAN baseline with no
  WireGuard and no Gatherlink. It records per-path `iperf3`, simultaneous
  `iperf3 -u`, and simultaneous `tools/udp_pressure.rs` results so the internal
  UDP pressure tool is continuously checked against an independent generator.
- `tools/hyperv/run_wireguard_onehop_speed.sh`: one-hop WireGuard baseline that
  can run Linux kernel WireGuard, userspace `wireguard-go`, optional
  `gotatun`, or optional Cloudflare `boringtun-cli`. It records per-path
  `iperf3`, simultaneous `iperf3 -u`, and simultaneous
  `tools/udp_pressure.rs` results through the WireGuard interface so the
  internal generator can be compared against an independent tool after
  encapsulation too. Use `--shape-profile` to apply the same simulated WAN
  path profile used by Gatherlink VM benchmarks.
- `tools/hyperv/install_gotatun_backend.sh`: optional VM-only installer for a
  pinned GotaTun source ref and Rust toolchain. This keeps GotaTun out of
  normal Gatherlink dependencies while making `wireguard-go` versus GotaTun
  comparisons repeatable.
- `tools/hyperv/install_boringtun_backend.sh`: optional VM-only installer for a
  pinned Cloudflare BoringTun CLI crate version. This keeps BoringTun out of
  normal Gatherlink dependencies while making Rust userspace WireGuard backend
  comparisons repeatable.
- `tools/hyperv/run_direct_wireguard_routing_speed.sh`: direct WireGuard
  B -> C -> A routing baseline
- `tools/hyperv/run_gatherlink_onehop_speed.sh`: raw UDP over direct two-node
  Gatherlink, without the relay VM and without WireGuard
- `tools/hyperv/apply_path_shape_profile.sh`: shared Hyper-V path shaping
  helper for clean, synthetic-capacity, and real-world facsimile profiles
- `tools/hyperv/run_relay_udp_speed.sh`: raw UDP over Gatherlink untrusted
  relay, without WireGuard
- `tools/hyperv/run_relay_wireguard_speed.sh`: WireGuard over Gatherlink
  untrusted relay
- `tools/hyperv/run_performance_matrix.sh`: orchestration wrapper that runs the
  scenarios above into one matrix directory
- `tools/run_three_path_profile_bench.py`: local three-path WAN profile runner
  that compares schedulers, cold/warm capacity cache behavior, and pass/target
  status from `thresholds.json`

Every scenario should write:

- `report.md` for operator-readable notes
- `report.json` or scenario JSON files for machine comparison
- command logs and node snapshots where possible

Local scheduler matrix runs prune bulky per-service runtime logs by default
after `summary.json`, before/after status snapshots, and command logs have been
written. Set `GATHERLINK_BENCH_KEEP_RUNTIME=1` only when debugging a single
short run; full matrices can otherwise fill the WSL filesystem with generated
service logs.

Every Gatherlink benchmark report must also include a separate WireGuard
baseline table for the equivalent shape:

- kernel WireGuard
- userspace WireGuard
- single-path runs when Gatherlink is tested on one path
- simultaneous per-path runs when Gatherlink is tested across multiple paths
- the same MTU, packet size, duration, offered pressure, and active path set
  whenever the tooling can make those equivalent

For multipath rows, the fair WireGuard baseline is the summed result from
equivalent WireGuard tunnels running at the same time across the same active
path set. Do not compare a three-path Gatherlink row against one direct
WireGuard tunnel or one best-path WireGuard result. Single-path WireGuard rows
are still useful path-quality references, but they are not the multipath
comparison baseline.

These baselines are the practical ceiling for outcome interpretation.
Gatherlink is a userland UDP transport, so exceeding a correct WireGuard
baseline should be treated as a measurement mistake until proven otherwise.

Current performance ledgers must keep comparison percentages in the row itself
whenever a reasonable baseline is known. A raw number without context is too
easy to misread. For WireGuard-over-Gatherlink rows, include at least:

- `% WG path-set TCP` for TCP or mixed TCP+UDP rows when a matching simultaneous
  userspace-WireGuard TCP baseline exists for the same active path set
- `% raw GL total` for mixed or UDP rows when a matching raw Gatherlink
  guardrail exists
- `% wg-user`, `% coord`, or the equivalent scheduler-relative columns in
  scheduler-matrix reports

If a percentage cannot be computed yet, write `n/a` in that column and explain
which baseline is missing in the row or nearby notes. Do not replace the
comparison table with narrative-only result rows; narrative readings are useful
only after the comparison columns are present.

When a current ledger row claims pass/fail, target, guardrail, regression, or
release-gate meaning, keep the gate facts visible in the table or in the
nearest schema note:

- baseline used for the gate
- `pass_threshold`
- `performance_target`
- compact gate status, using `fail`, `pass`, `target`, or `n/a`

Rows that are exploratory or missing a fair baseline should say so explicitly
instead of dropping the columns. When a table has more than one useful
baseline, use separate compact gate columns, such as `GL Gate` and `WG Gate`,
instead of hiding one comparison in prose. The current Hyper-V log has its own
schema contract in `hyperv-performance-log.md`; follow it when editing that
file.

## Strategy

Run benchmarks in layers:

1. Plain private LAN: proves the virtual links and host can carry the target.
2. One-hop WireGuard kernel and userspace: separates WireGuard implementation
   cost from Gatherlink and relay cost. Userspace rows should normally include
   `wireguard-go`; add GotaTun and BoringTun rows when those optional backends
   are installed.
3. Direct WireGuard routing: proves the kernel WireGuard baseline and route
   shape through VM C.
4. Gatherlink raw UDP: proves the userspace transport without WireGuard
   ordering/retransmit behavior.
5. WireGuard over Gatherlink: proves the combined product-relevant shape.
6. Relay variants: repeat the same layers with VM C as an untrusted transit
   node so relay work is measured separately from endpoint work.

Use `wireguard-over-gatherlink-status.md` for the current interpretation of
WireGuard-over-Gatherlink results. Keep exact run evidence in
`hyperv-performance-log.md` or generated benchmark reports.

Only compare runs with the same:

- VM shape and vCPU allocation
- path MTU and WireGuard MTU
- offered rate, packet size, duration, and TCP parallelism
- kernel socket-buffer tuning state
- active path set
- Gatherlink flowlet/reorder settings

For raw UDP baselines, prefer keeping both `iperf3 -u` and `udp_pressure` in
the same report. `iperf3 -u -b 0` is the independent unbounded UDP reference.
`udp_pressure --target-mbit` is the paced Gatherlink-friendly generator, and
unpaced `udp_pressure` is the stress tool used to prove the generator/sink are
not the bottleneck before blaming Gatherlink.

For WireGuard baselines, keep the same cross-check when possible. `iperf3`
remains the independent reference, while `udp_pressure` proves that Gatherlink's
own UDP generator behaves similarly after a tunnel has been added.

For single-path investigations, use the matrix wrapper with `--active-paths a`
and include `wireguard-kernel-onehop`, `wireguard-userspace-onehop`,
`wireguard-gotatun-onehop` and `wireguard-boringtun-onehop` when installed, and
`gatherlink-onehop-udp`. That isolates one direct VM path without multipath
ordering or relay forwarding. Add `gatherlink-relay-udp` and
`wireguard-over-gatherlink-relay` only when the question is how much the
untrusted relay shape changes the result.

If those differ, record the run as an exploratory data point rather than a
baseline.

For local scheduler/WAN-shape work, run the three-path profile wrapper:

```bash
.venv/bin/python tools/run_three_path_profile_bench.py \
  --schedulers capacity_aware,arrival_guarded_capacity,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware \
  --cache-modes cold,warm
```

That default run uses the normal local-lab packet shape: 1200 byte path MTU
and 1200 byte generated UDP payloads. The non-real-world scheduler profiles
must also be run with jumbo frames because they are synthetic capacity probes,
not WAN facsimiles:

```bash
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high \
  --schedulers capacity_aware,arrival_guarded_capacity,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware \
  --cache-modes cold,warm \
  --path-mtu 9000 \
  --payload-size 8192
```

Keep the real-world profiles at normal MTU unless the benchmark question is
explicitly jumbo behavior. Those profiles are intended to mimic ordinary WAN
links where jumbo end-to-end MTU is usually not available.

Cold-cache runs prove the configured/profile startup guess is good enough to
begin safely. Warm-cache runs prove sustained traffic and auto-detected capacity
improve the next run. The generated `report.md` shows configured path capacities
beside observed per-path receive rates, plus both the minimum `pass_threshold`
and the desired `performance_target`.

The wrapper copies the selected profile's shaping facts onto the temporary lab
paths before services start. That gives Python scheduler policy the same
startup delay/jitter hints that the lab later applies with `tc`; otherwise
latency-aware schedulers would be judged without the facts their profile names
claim to provide.

In the local scheduler/WAN-shape tables, `delivery` means delivered sink
throughput divided by the offered benchmark pressure. It is not a WireGuard
relative ratio. Transport performance comparison tables use userspace
WireGuard as the default baseline unless a row explicitly names a different
baseline.
