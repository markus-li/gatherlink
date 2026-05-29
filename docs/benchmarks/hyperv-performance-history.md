# Hyper-V Performance Log

This log records benchmark evidence gathered from the local three-VM Hyper-V
lab. Keep each entry clear about what was compared and what the likely
bottleneck was.

## Current Comparison Model

The benchmark stack is intentionally layered:

| Layer | Purpose | Tool |
| --- | --- | --- |
| Private LAN | Host and virtual switch baseline; no tunnel | `tools/hyperv/run_private_lan_speed.sh` |
| One-hop WireGuard kernel | Kernel WireGuard cost over one VM path | `tools/hyperv/run_wireguard_onehop_speed.sh --implementation kernel` |
| One-hop WireGuard userspace | `wireguard-go` cost over the same one VM path | `tools/hyperv/run_wireguard_onehop_speed.sh --implementation userspace` |
| One-hop WireGuard GotaTun | Optional GotaTun cost over the same one VM path | `tools/hyperv/run_wireguard_onehop_speed.sh --implementation gotatun` |
| One-hop WireGuard BoringTun | Optional Cloudflare BoringTun cost over the same one VM path | `tools/hyperv/run_wireguard_onehop_speed.sh --implementation boringtun` |
| Direct WireGuard | Kernel WireGuard baseline through the same VM links | `tools/hyperv/run_direct_wireguard_routing_speed.sh` |
| Gatherlink raw one-hop | Direct endpoint Gatherlink transport cost without relay or WireGuard | `tools/hyperv/run_gatherlink_onehop_speed.sh` |
| Gatherlink raw relay | Gatherlink transport and relay cost without WireGuard | `tools/hyperv/run_relay_udp_speed.sh` |
| WireGuard over Gatherlink relay | Product-relevant combined path | `tools/hyperv/run_relay_wireguard_speed.sh` |
| Matrix | Repeatable comparison wrapper | `tools/hyperv/run_performance_matrix.sh` |

## 2026-05-22 WireGuard Through Gatherlink Scheduler Sweep

Evidence:

```text
.gatherlink/hyperv-performance/wg-through-gl-scheduler-sweep-current/
.gatherlink/hyperv-performance/wg-through-gl-scheduler-sweep-short-flowlet/
.gatherlink/hyperv-performance/wg-through-gl-scheduler-sweep-mid-flowlet/
.gatherlink/hyperv-performance/wg-through-gl-single-path-control/
.gatherlink/hyperv-performance/wg-through-gl-best-two-confirmation/
.gatherlink/hyperv-performance/wg-through-gl-coordinated-parallel12/
.gatherlink/hyperv-performance/wg-through-gl-coordinated-parallel24/
.gatherlink/hyperv-performance/wg-through-gl-coordinated-parallel48/
.gatherlink/hyperv-performance/raw-gl-coordinated-1000-control/
.gatherlink/hyperv-performance/raw-gl-coordinated-1500-control/
```

Purpose: compare raw Gatherlink UDP with WireGuard-over-Gatherlink on the same
one-hop three-path VM topology. This isolates the scheduler behavior needed by
WireGuard's encrypted UDP carrier from raw application UDP behavior.

Primary command shape:

```bash
tools/hyperv/run_wireguard_gatherlink_scheduler_sweep.sh \
  --duration 8 \
  --schedulers round_robin,capacity_aware,latency_guarded_capacity,flowlet_adaptive,coordinated_adaptive,ordered_multipath_capacity_aware \
  --raw-target-mbit 2500 \
  --payload-size 1300 \
  --parallel 6 \
  --udp-rate 2000M \
  --udp-length 1300 \
  --link-mtu 1500 \
  --wg-mtu 1380 \
  --path-mtu 1472 \
  --security-mode authenticated \
  --active-paths a,b,c \
  --path-capacity-mbits a:5000,b:5000,c:5000 \
  --flowlet-idle-us 50000 \
  --flowlet-max-hold-us 60000000
```

Fast reading table:

| Run | Scheduler / shape | Raw Gatherlink sink | Raw packet delta | WG-over-GL TCP | WG-over-GL UDP | Reading |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Full sweep, 8s | `round_robin` | 1117.67 Mbit/s | 785899 | 800.66 Mbit/s | 705.40 Mbit/s | Baseline; neither raw nor WG prefers blind striping here. |
| Full sweep, 8s | `capacity_aware` | 1264.20 Mbit/s | 613591 | 870.98 Mbit/s | 673.53 Mbit/s | Better raw split; WG TCP improves, UDP does not. |
| Full sweep, 8s | `latency_guarded_capacity` | 1288.28 Mbit/s | 641755 | 854.76 Mbit/s | 647.12 Mbit/s | No latency spread in this test, so the guard adds little. |
| Full sweep, 8s | `flowlet_adaptive` | 1362.95 Mbit/s | 585121 | 900.91 Mbit/s | 721.41 Mbit/s | Best raw row and effectively tied for best WG TCP. |
| Full sweep, 8s | `coordinated_adaptive` | 1162.66 Mbit/s | 716186 | 900.98 Mbit/s | 702.38 Mbit/s | Best safe default for mixed WG traffic because it can switch policy from telemetry. |
| Full sweep, 8s | `ordered_multipath_capacity_aware` | 1052.27 Mbit/s | 864383 | 893.51 Mbit/s | 733.44 Mbit/s | Best WG UDP in this short run, but weak raw behavior and sensitive to flowlet timing. |
| Short flowlet, 8s | `coordinated_adaptive`, 25ms idle, 100ms hold, run 4 | 1449.71 Mbit/s | 453996 | 867.12 Mbit/s | 742.56 Mbit/s | Shorter holds help raw/UDP bursts but reduce WG TCP stability. |
| Mid flowlet, 8s | `flowlet_adaptive`, 50ms idle, 500ms hold, run 32 | 1310.75 Mbit/s | 617389 | 866.15 Mbit/s | 609.14 Mbit/s | Middle ground did not beat long-hold TCP. |
| Single path control, 8s | `capacity_aware`, path a only | 1450.12 Mbit/s | 0 | 850.31 Mbit/s | 711.08 Mbit/s | Pinning to one path is clean but below the best three-path WG TCP row. |
| Best two confirmation, 20s | `flowlet_adaptive`, long hold | 979.80 Mbit/s | 2652848 | 840.34 Mbit/s | 602.04 Mbit/s | Longer run showed VM/runtime variability and raw overdrive loss. |
| Best two confirmation, 20s | `coordinated_adaptive`, long hold | 1136.63 Mbit/s | 2327210 | 848.51 Mbit/s | 697.05 Mbit/s | Best longer-run WG result among the two candidates. |
| Parallelism check, 12s | `coordinated_adaptive`, 12 TCP streams | setup-only | - | 901.23 Mbit/s | 748.88 Mbit/s | The WG benchmark was under-driving TCP at 6 streams. |
| Parallelism check, 12s | `coordinated_adaptive`, 24 TCP streams | setup-only | - | 969.27 Mbit/s | 652.27 Mbit/s | Best WG-over-GL TCP result in this pass. |
| Parallelism check, 12s | `coordinated_adaptive`, 48 TCP streams | setup-only | - | 918.02 Mbit/s | 718.63 Mbit/s | More streams are not automatically better. |
| Raw control, 12s | `coordinated_adaptive`, 1 Gbit/s offered | 999.95 Mbit/s | 0 | - | - | Raw Gatherlink can carry 1 Gbit/s cleanly with this packet size. |
| Raw control, 12s | `coordinated_adaptive`, 1.5 Gbit/s offered | 1335.13 Mbit/s | 0 | - | - | Packets arrived, but receiver timing stretched; use paced targets when comparing. |

Conclusion:

- Best current scheduler for WireGuard-through-Gatherlink is
  `coordinated_adaptive` with long flowlet guardrails. It is not always the
  fastest single row, but it is the best product default because WireGuard
  traffic changes shape: TCP-over-WireGuard wants stable ordering, while UDP
  over WireGuard can tolerate more rotation.
- `flowlet_adaptive` is the best explicit “sticky WireGuard tunnel” policy when
  the operator wants to favor inner TCP stability and accepts less dynamic path
  switching.
- `ordered_multipath_capacity_aware` is still useful evidence, but not the
  WireGuard default. It can win short UDP rows and then collapse under a
  different flowlet/path-run shape, so it needs more targeted ordered-flow
  work before it becomes the safe WG default.
- The WireGuard-over-Gatherlink benchmark and scheduler sweep now default to
  24 TCP streams. Six streams underdrove the tunnel and made scheduler
  comparisons look worse than they were. The original full sweep above still
  records `--parallel 6` because that was the historical input for that run.
- Raw Gatherlink and WireGuard-over-Gatherlink should not use the same score.
  Raw UDP is useful for endpoint transport ceiling, but WireGuard represents a
  nested UDP tunnel with inner TCP sensitivity to jitter, reorder, and pacing.

## 2026-05-22 Local Three-Path WAN Scheduler Matrix

Evidence:

```text
.gatherlink/lab-profile-runs/full-wan-scheduler-matrix-final-pass-1/
.gatherlink/lab-profile-runs/full-wan-scheduler-matrix-jumbo-final-pass-1/
```

This is the first full local scheduler matrix from the 2026-05-22 tuning pass.
It is retained as historical evidence because it shows why later threshold and
queue-model fixes were made. For the current calibrated pass/fail reading, use
the later section `Final useful-threshold matrix after the clean-profile queue
fix`.

Command shape:

```bash
.venv/bin/python tools/run_three_path_profile_bench.py \
  --schedulers round_robin,weighted_round_robin,capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware \
  --cache-modes cold,warm \
  --duration 8 \
  --out .gatherlink/lab-profile-runs/full-wan-scheduler-matrix-final-pass-1

.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware \
  --cache-modes cold,warm \
  --duration 8 \
  --path-mtu 9000 \
  --payload-size 8192 \
  --out .gatherlink/lab-profile-runs/full-wan-scheduler-matrix-jumbo-final-pass-1
```

This matrix is local namespace scheduler evidence, not a Hyper-V throughput
ceiling test. `Delivery` is sink throughput divided by offered benchmark
pressure. It is not a WireGuard-relative ratio; transport comparison tables use
userspace WireGuard as the default baseline unless the table explicitly says
otherwise.

Profile shape quick reference:

| Profile | Configured path speed a/b/c | Latency/jitter/loss a/b/c | Queue limit a/b/c | Pressure | Expected capacity | Normal MTU | Jumbo MTU |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `acceptance-300-500-700` | 300/500/700 Mbit/s | clean/clean/clean | 131072/131072/131072 | 1550 Mbit/s | 1500 Mbit/s | 1200 | 9000 |
| `acceptance-uneven-high` | 600/900/1300 Mbit/s | clean/clean/clean | 4096/4096/4096 | 2900 Mbit/s | 2800 Mbit/s | 1200 | 9000 |
| `realworld-fiber-plus-5g` | 800/160/85 Mbit/s | 12ms +/- 3ms 0% / 45ms +/- 15ms 0.2% / 70ms +/- 25ms 0.5% | 4096/2048/2048 | 950 Mbit/s | 930 Mbit/s | 1200 | not in default matrix |
| `realworld-starlink-plus-5g` | 180/120/15 Mbit/s | 45ms +/- 25ms 0.3% / 55ms +/- 20ms 0.6% / 95ms +/- 35ms 1% | 2048/2048/512 | 250 Mbit/s | 245 Mbit/s | 1200 | not in default matrix |
| `realworld-starlink-plus-2x5g` | 180/140/90 Mbit/s | 45ms +/- 25ms 0.3% / 55ms +/- 20ms 0.6% / 80ms +/- 30ms 0.8% | 2048/2048/2048 | 350 Mbit/s | 340 Mbit/s | 1200 | not in default matrix |


Wide fast reading table, normal MTU from this initial matrix:

| Profile | MTU | Payload | Best scheduler/cache | Sink RX | Delivery | Gate | Path RX a/b/c | Drops a/b/c |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | 1200 | 1200 | `ordered_multipath_capacity_aware` warm | 1545.5 | 99.7% | target | 300.5/506.0/738.9 | 0/0/0 |
| `acceptance-uneven-high` | 1200 | 1200 | `ordered_multipath` cold | 1627.9 | 56.1% | fail | 333.3/533.1/761.5 | 0/0/0 |
| `realworld-fiber-plus-5g` | 1200 | 1200 | `latency_guarded_capacity` cold | 1096.7 | 99.7% | target | 840.7/167.4/88.6 | 2035/1115/728 |
| `realworld-starlink-plus-5g` | 1200 | 1200 | `capacity_aware` warm | 245.3 | 75.5% | pass | 126.1/106.4/12.9 | 97419/26738/3555 |
| `realworld-starlink-plus-2x5g` | 1200 | 1200 | `capacity_aware` cold | 320.4 | 76.3% | pass | 125.9/115.2/79.3 | 94268/44112/19358 |

Wide fast reading table, jumbo synthetic profiles from this initial matrix:

| Profile | MTU | Payload | Best scheduler/cache | Sink RX | Delivery | Gate | Path RX a/b/c | Drops a/b/c |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | 9000 | 8192 | `capacity_aware` cold | 1550.0 | 100.0% | target | 310.0/516.7/723.3 | 0/0/0 |
| `acceptance-uneven-high` | 9000 | 8192 | `capacity_aware` warm | 2871.0 | 99.0% | target | 621.4/926.4/1323.2 | 0/705/2838 |

Detailed scheduler rows, normal MTU:

| Path MTU | Payload | Profile | Scheduler | Cache | Offered | Sink RX | Delivery | Pass | Target | Path RX a/b/c | Drops a/b/c |
| ---: | ---: | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |
| 1200 | 1200 | `acceptance-300-500-700` | `round_robin` | `cold` | 1550 | 1354.0 | 87.4% | yes | no | 344.6/504.7/504.7 | 227087/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `round_robin` | `warm` | 1550 | 1342.8 | 86.6% | yes | no | 343.0/499.9/499.9 | 221071/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `round_robin` | `cold` | 2900 | 1540.0 | 53.1% | no | no | 513.3/513.3/513.3 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `round_robin` | `warm` | 2900 | 1529.1 | 52.7% | no | no | 509.7/509.7/509.7 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `round_robin` | `cold` | 1100 | 543.3 | 49.4% | no | no | 366.7/131.4/45.3 | 0/376900/504254 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `round_robin` | `warm` | 1100 | 544.3 | 49.5% | no | no | 366.7/132.0/45.7 | 0/376199/503757 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `round_robin` | `cold` | 325 | 225.5 | 69.4% | no | no | 107.7/107.0/10.8 | 568/1080/159620 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `round_robin` | `warm` | 325 | 225.7 | 69.5% | no | no | 107.7/107.1/10.9 | 517/1032/159558 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `round_robin` | `cold` | 420 | 317.9 | 75.7% | yes | no | 126.8/115.3/75.8 | 20693/38350/101161 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `round_robin` | `warm` | 420 | 317.8 | 75.7% | yes | no | 126.9/115.2/75.7 | 20647/38589/101202 |
| 1200 | 1200 | `acceptance-300-500-700` | `weighted_round_robin` | `cold` | 1550 | 1347.5 | 86.9% | yes | no | 342.9/502.3/502.3 | 225429/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `weighted_round_robin` | `warm` | 1550 | 1352.5 | 87.3% | yes | no | 343.2/504.7/504.7 | 228980/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `weighted_round_robin` | `cold` | 2900 | 1535.7 | 53.0% | no | no | 511.9/511.9/511.9 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `weighted_round_robin` | `warm` | 2900 | 1456.9 | 50.2% | no | no | 485.6/485.6/485.6 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `weighted_round_robin` | `cold` | 1100 | 545.8 | 49.6% | no | no | 366.7/132.9/46.3 | 0/374071/502893 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `weighted_round_robin` | `warm` | 1100 | 543.5 | 49.4% | no | no | 366.7/131.6/45.2 | 0/376406/504198 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `weighted_round_robin` | `cold` | 325 | 225.6 | 69.4% | no | no | 107.7/107.0/10.9 | 544/1128/159548 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `weighted_round_robin` | `warm` | 325 | 225.6 | 69.4% | no | no | 107.7/107.0/10.9 | 519/1119/159547 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `weighted_round_robin` | `cold` | 420 | 318.2 | 75.8% | yes | no | 126.9/115.5/75.8 | 20486/38276/101048 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `weighted_round_robin` | `warm` | 420 | 317.7 | 75.6% | yes | no | 126.8/115.3/75.6 | 20682/38455/101298 |
| 1200 | 1200 | `acceptance-300-500-700` | `capacity_aware` | `cold` | 1550 | 1519.3 | 98.0% | yes | yes | 304.0/506.7/708.5 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 1550 | 1501.0 | 96.8% | yes | yes | 300.3/500.6/700.2 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `capacity_aware` | `cold` | 2900 | 1454.9 | 50.2% | no | no | 318.9/478.3/657.7 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 2900 | 1573.2 | 54.2% | no | no | 337.7/506.5/729.0 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `capacity_aware` | `cold` | 1100 | 1068.2 | 97.1% | yes | yes | 818.9/162.8/86.5 | 33115/7904/3476 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `capacity_aware` | `warm` | 1100 | 935.9 | 85.1% | yes | no | 720.9/143.1/71.9 | 173560/37112/22489 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `capacity_aware` | `cold` | 325 | 245.0 | 75.4% | yes | no | 125.8/106.3/12.9 | 97939/26757/3563 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `capacity_aware` | `warm` | 325 | 245.3 | 75.5% | yes | no | 126.1/106.4/12.9 | 97419/26738/3555 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `capacity_aware` | `cold` | 420 | 320.4 | 76.3% | yes | no | 125.9/115.2/79.3 | 94268/44112/19358 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `capacity_aware` | `warm` | 420 | 320.2 | 76.2% | yes | no | 125.7/115.4/79.1 | 94770/43876/19510 |
| 1200 | 1200 | `acceptance-300-500-700` | `latency_guarded_capacity` | `cold` | 1550 | 1437.8 | 92.8% | yes | yes | 287.7/479.4/670.8 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 1550 | 1458.3 | 94.1% | yes | yes | 291.8/486.4/680.0 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `latency_guarded_capacity` | `cold` | 2900 | 1550.8 | 53.5% | no | no | 332.4/498.6/719.7 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 2900 | 1538.6 | 53.1% | no | no | 330.5/495.8/712.3 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `latency_guarded_capacity` | `cold` | 1100 | 1096.7 | 99.7% | yes | yes | 840.7/167.4/88.6 | 2035/1115/728 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `latency_guarded_capacity` | `warm` | 1100 | 1093.0 | 99.4% | yes | yes | 837.8/166.7/88.5 | 6197/2212/869 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `latency_guarded_capacity` | `cold` | 325 | 244.3 | 75.2% | yes | no | 125.1/106.4/12.9 | 99081/26766/3568 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `latency_guarded_capacity` | `warm` | 325 | 245.3 | 75.5% | yes | no | 126.0/106.4/12.9 | 97679/26765/3516 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `latency_guarded_capacity` | `cold` | 420 | 320.3 | 76.3% | yes | no | 125.8/115.3/79.3 | 94534/44199/19372 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `latency_guarded_capacity` | `warm` | 420 | 320.2 | 76.2% | yes | no | 125.9/115.1/79.2 | 94383/44362/19469 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath` | `cold` | 1550 | 1542.1 | 99.5% | yes | yes | 299.7/504.8/737.6 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 1550 | 1536.8 | 99.1% | yes | yes | 298.9/503.3/734.7 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `ordered_multipath` | `cold` | 2900 | 1627.9 | 56.1% | no | no | 333.3/533.1/761.5 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 2900 | 1601.6 | 55.2% | no | no | 328.2/524.6/748.8 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath` | `cold` | 1100 | 678.9 | 61.7% | no | no | 418.1/179.9/81.0 | 15924/174499/308186 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath` | `warm` | 1100 | 639.6 | 58.1% | no | no | 377.6/182.3/79.7 | 5404/187129/331556 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath` | `cold` | 325 | 226.9 | 69.8% | no | no | 109.2/107.2/10.5 | 6782/2552/149362 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath` | `warm` | 325 | 227.0 | 69.8% | no | no | 109.1/107.2/10.6 | 7247/2660/148709 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath` | `cold` | 420 | 317.9 | 75.7% | yes | no | 126.3/115.9/75.7 | 27637/38936/93875 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath` | `warm` | 420 | 317.0 | 75.5% | yes | no | 125.5/115.7/75.7 | 28831/39173/93863 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `cold` | 1550 | 1533.0 | 98.9% | yes | yes | 298.3/502.4/732.3 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 1550 | 1545.5 | 99.7% | yes | yes | 300.5/506.0/738.9 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `cold` | 2900 | 1416.9 | 48.9% | no | no | 310.1/496.2/610.6 | 0/0/0 |
| 1200 | 1200 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 2900 | 1290.1 | 44.5% | no | no | 293.4/460.8/535.9 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | `cold` | 1100 | 638.5 | 58.0% | no | no | 376.7/182.2/79.6 | 11646/186994/330673 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 1100 | 643.9 | 58.5% | no | no | 383.7/180.1/80.1 | 8854/182948/323487 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath_capacity_aware` | `cold` | 325 | 226.7 | 69.8% | no | no | 109.0/107.2/10.5 | 6799/2483/149532 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 325 | 226.6 | 69.7% | no | no | 108.9/107.1/10.5 | 6718/2582/149769 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | `cold` | 420 | 317.6 | 75.6% | yes | no | 126.2/115.8/75.6 | 27851/39123/93685 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | `warm` | 420 | 317.5 | 75.6% | yes | no | 126.1/115.8/75.7 | 27991/39166/93884 |

`pass` means the minimum benchmark gate was met. `target` means the current performance target was met.

Detailed scheduler rows, jumbo synthetic profiles:

| Path MTU | Payload | Profile | Scheduler | Cache | Offered | Sink RX | Delivery | Pass | Target | Path RX a/b/c | Drops a/b/c |
| ---: | ---: | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |
| 9000 | 8192 | `acceptance-300-500-700` | `capacity_aware` | `cold` | 1550 | 1550.0 | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 1550 | 1550.0 | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `capacity_aware` | `cold` | 2900 | 2867.9 | 98.9% | yes | yes | 620.6/925.4/1321.8 | 0/671/2791 |
| 9000 | 8192 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 2900 | 2871.0 | 99.0% | yes | yes | 621.4/926.4/1323.2 | 0/705/2838 |
| 9000 | 8192 | `acceptance-300-500-700` | `latency_guarded_capacity` | `cold` | 1550 | 1550.0 | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 1550 | 1550.0 | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `latency_guarded_capacity` | `cold` | 2900 | 2870.9 | 99.0% | yes | yes | 621.4/926.3/1323.1 | 0/709/2844 |
| 9000 | 8192 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 2900 | 2868.9 | 98.9% | yes | yes | 620.9/925.7/1322.3 | 0/687/2811 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath` | `cold` | 1550 | 1391.0 | 89.7% | yes | no | 331.2/520.1/539.7 | 19409/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 1550 | 1392.1 | 89.8% | yes | no | 331.2/520.3/540.5 | 19279/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath` | `cold` | 2900 | 2583.2 | 89.1% | yes | no | 629.1/926.4/1027.7 | 34513/4157/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 2900 | 2588.0 | 89.2% | yes | no | 629.1/926.4/1032.5 | 33884/4204/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `cold` | 1550 | 1392.1 | 89.8% | yes | no | 331.3/520.3/540.5 | 19275/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 1550 | 1391.8 | 89.8% | yes | no | 331.2/520.2/540.4 | 19315/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `cold` | 2900 | 2581.1 | 89.0% | yes | no | 628.8/926.4/1025.9 | 34786/4135/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 2900 | 2583.0 | 89.1% | yes | no | 629.1/926.4/1027.5 | 34604/4096/0 |

`pass` means the minimum benchmark gate was met. `target` means the current performance target was met.

Summary:

- Every current profile has at least one sensible scheduler that passes its gate.
  `capacity_aware` and `latency_guarded_capacity` remain the best general
  unordered UDP policies. `ordered_multipath_capacity_aware` is now the best
  normal-MTU 300/500/700 row in this run.
- `acceptance-uneven-high` at normal MTU remains a local host/runtime ceiling
  detector around 1.5-1.6 Gbit/s. That profile needs jumbo or a different host
  ceiling before normal-MTU target results are meaningful.
- Jumbo synthetic capacity scheduling remains strong: the 300/500/700 profile
  reaches the full offered 1550 Mbit/s with zero drops, and the uneven-high
  profile reaches 2871 Mbit/s on the warm `capacity_aware` row.
- Ordered jumbo mode is improved enough to pass, but not enough to be the jumbo
  throughput baseline. A bounded virtual catch-up experiment was tested after
  this matrix and made ordered jumbo slightly worse, so the next ordered-jumbo
  improvement should be a more deliberate share/deficit model rather than a
  simple wall-clock catch-up clamp.
  Evidence for the rejected experiment:
  `.gatherlink/lab-profile-runs/ordered-normal-after-catchup-bound/` and
  `.gatherlink/lab-profile-runs/ordered-jumbo-after-catchup-bound/`.
  A separate Rust-side weight-share penalty experiment also failed to improve
  the jumbo target enough to justify the added mechanism; it left
  `ordered_multipath_capacity_aware` around 1395 Mbit/s on the 300/500/700
  jumbo profile and still overfilled `path-a`. Evidence:
  `.gatherlink/lab-profile-runs/ordered-jumbo-weight-share/`.
- The lossy Starlink facsimiles pass but do not hit target. That is expected
  with the current pressure and netem loss/jitter shape; chasing target there
  should start by reviewing the profile pressure and expected-capacity model, not
  by making Rust infer policy from drops.

### Lab Hot-Path Follow-Up

Evidence:

```text
.gatherlink/lab-profile-runs/lab-summary-hotpath-cycles8-1472/
.gatherlink/lab-profile-runs/clean-high-cycles8-1452/
.gatherlink/lab-profile-runs/profile-defaults-smoke-shape-fixed/
.gatherlink/lab-profile-runs/realworld-cycles8-1452/
```

The local lab supervisor was still using outcome-returning PyO3 calls and
per-packet log formatting in the dataplane step. Switching the default lab path
to aggregate Rust summary drains, while keeping raw packet logs behind
`GATHERLINK_LAB_PACKET_LOG=1`, removed a lab-only bottleneck without moving
policy into Rust.

The first aggregate pass used too much Rust burst work per Python loop and could
overdrive shaped lower-speed queues. The current lab bound is 512 datagrams for
8 Rust cycles per supervisor step. That still lets the clean high-pressure
profile reach target while keeping monitor and control loops responsive.

Focused results after the bounded aggregate drain:

| Profile | MTU | Payload | Scheduler | Sink RX | Delivery | Gate | Path RX a/b/c | Drops a/b/c |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- |
| `acceptance-uneven-high` | 1452 | 1438 | `capacity_aware` | 2900.0 | 100.0% | target | 621.4/932.1/1346.4 | 0/0/0 |
| `acceptance-uneven-high` | 1452 | 1438 | `latency_guarded_capacity` | 2900.0 | 100.0% | target | 621.4/932.1/1346.4 | 0/0/0 |
| `acceptance-uneven-high` | 1452 | 1438 | `ordered_multipath` | 2722.7 | 93.9% | target | 584.9/926.0/1211.8 | 0/0/0 |
| `acceptance-uneven-high` | 1452 | 1438 | `ordered_multipath_capacity_aware` | 2741.2 | 94.5% | target | 584.9/926.0/1232.0 | 0/0/0 |
| `realworld-starlink-plus-2x5g` | 1200 | 1200 | `capacity_aware` | 318.2 | 75.8% | pass | 124.7/114.3/79.1 | 96924/45918/19574 |
| `realworld-starlink-plus-2x5g` | 1200 | 1200 | `latency_guarded_capacity` | 318.2 | 75.8% | pass | 124.6/114.5/79.1 | 97106/45690/19515 |

Profile defaults are now explicit in `docs/benchmarks/thresholds.json`.
`acceptance-uneven-high` uses IPv6-safe max-normal frames (`shape_mtu=1452`,
payload `1438`) because that profile is a clean synthetic packet-rate test.
Lossy real-world profiles keep 1200-byte payloads and do not force the veth MTU
down; a rejected 1452-byte run made the Starlink facsimiles fail by changing the
network-loss model rather than improving Gatherlink.

## 2026-05-21 V0.9.2 Full Comparison Refresh

Evidence:

```text
.gatherlink/hyperv-performance/20260521T150841Z-v092-full-comparison/
.gatherlink/hyperv-performance/20260521T150841Z-v092-full-comparison-targeted/
```

Matrix command shape:

```bash
tools/hyperv/run_performance_matrix.sh \
  --duration 15 \
  --parallel 6 \
  --udp-rate 2000M \
  --udp-length 1200 \
  --active-paths a,b,c \
  --scenarios private-lan,wireguard-kernel-onehop,wireguard-userspace-onehop,wireguard-gotatun-onehop,wireguard-boringtun-onehop,direct-wireguard,gatherlink-onehop-udp,gatherlink-relay-udp,wireguard-over-gatherlink-relay
```

`wireguard-gotatun-onehop` and `wireguard-boringtun-onehop` are optional.
Install them first with `tools/hyperv/install_gotatun_backend.sh` and
`tools/hyperv/install_boringtun_backend.sh`, and keep them labeled separately
from `wireguard-go` so backend comparisons do not become scheduler claims.

Targeted follow-up commands used the same 15 second duration, 1200 byte UDP
payload, 1450 byte Gatherlink path MTU, and 2000 Mbit/s offered rate for:

- one-path raw Gatherlink endpoint transport
- one-path and three-path raw Gatherlink relay transport with the compiled
  Rust UDP pressure helper
- one-path WireGuard-over-Gatherlink relay

Summary:

| Scenario | Topology | Paths | Test | Result | Compared Baseline | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| Private LAN TCP | VM B -> VM A, no tunnel | per path | TCP, 6 streams | 39.10-50.12 Gbit/s | host/vSwitch baseline | Very high virtual-switch ceiling; retransmits are visible, so this is a host ceiling signal, not a clean application target. |
| Private LAN UDP | VM B -> VM A, no tunnel | per path | 2 Gbit/s offered UDP | 2.00 Gbit/s, 0.00% loss | host/vSwitch baseline | Confirms the lab links can carry the offered UDP rate without tunnel overhead. |
| WireGuard kernel TCP | VM B -> VM A | per path | TCP, 6 streams | 4.22-4.31 Gbit/s | WireGuard one-hop baseline | Kernel WireGuard remains the practical single-link tunnel speed reference. |
| WireGuard kernel UDP | VM B -> VM A | per path | 2 Gbit/s offered UDP | 1.99-2.00 Gbit/s, 0.00% loss | WireGuard one-hop baseline | Clean at the offered rate on all three private paths. |
| WireGuard userspace TCP | VM B -> VM A | per path | TCP, 6 streams | 4.22-4.35 Gbit/s | userspace WireGuard reference | `wireguard-go` remains close to kernel WireGuard in this VM lab. |
| WireGuard userspace UDP | VM B -> VM A | per path | 2 Gbit/s offered UDP | 2.00 Gbit/s, 0.00% loss | userspace WireGuard reference | Userspace WireGuard is not the limiting comparison point at this offered rate. |
| Direct WireGuard routing | VM B -> VM C -> VM A | 3+3 WG links | TCP, 6 streams | 2.23 Gbit/s end-to-end | relay-route WireGuard baseline | Simultaneous hop total was 5.53 Gbit/s across all six hop tests; end-to-end routed TCP is the product-relevant baseline. |
| Raw Gatherlink one-hop | VM B -> VM A | 1 path | 2 Gbit/s offered UDP | 1.63 Gbit/s delivered | 81.5% of one-path WireGuard UDP offered rate | Good single-path userland result, but still not at WireGuard parity. |
| Raw Gatherlink one-hop | VM B -> VM A | 3 paths | 2 Gbit/s offered UDP | 674.90 Mbit/s delivered | 33.7% of offered rate | This run exposes multipath endpoint pressure; do not treat it as the single-path dataplane ceiling. |
| Raw Gatherlink relay | VM B -> VM C -> VM A | 3 paths | 2 Gbit/s offered UDP | 1.33 Gbit/s delivered | 59.7% of direct WireGuard routed TCP | Matrix relay run; VM B reported UDP receive-buffer pressure during this run. |
| Raw Gatherlink relay, Rust pressure | VM B -> VM C -> VM A | 1 path | 2 Gbit/s offered UDP | 1.35 Gbit/s delivered | 67.6% of offered rate | Uses the compiled UDP pressure helper to avoid Python generator overhead. |
| Raw Gatherlink relay, Rust pressure | VM B -> VM C -> VM A | 3 paths | 2 Gbit/s offered UDP | 1.01 Gbit/s delivered | 50.6% of offered rate | Worse than the matrix relay run, showing the current multipath relay behavior is still noisy under pressure. |
| WireGuard over Gatherlink relay | VM B -> VM C -> VM A | 1 path | TCP, 6 streams | 1.11 Gbit/s | 49.8% of direct WireGuard routed TCP | Clean retransmit-free tunnel-through-Gatherlink result, but still about half the direct WG route. |
| WireGuard over Gatherlink relay | VM B -> VM C -> VM A | 1 path | 2 Gbit/s offered UDP | 854.30 Mbit/s, 0.00% loss | 42.7% of offered rate | Clean UDP delivery at the measured ceiling. |
| WireGuard over Gatherlink relay | VM B -> VM C -> VM A | 3 paths | TCP, 6 streams | 1.07 Gbit/s | 47.9% of direct WireGuard routed TCP | Three paths did not improve this flow; ordering and queue pressure dominate. |
| WireGuard over Gatherlink relay | VM B -> VM C -> VM A | 3 paths | 2 Gbit/s offered UDP | 879.00 Mbit/s, 0.00% loss | 44.0% of offered rate | Slightly above the one-path UDP result, but not close enough to the 90% WireGuard target. |

Reading:

- Gatherlink is not yet at the "within 90% of WireGuard" performance goal in
  this Hyper-V lab. The routed WireGuard-over-Gatherlink TCP result is about
  48% of the direct WireGuard routed TCP baseline.
- Single-path raw Gatherlink endpoint transport is much stronger than the
  three-path endpoint result in this run. That points at multipath ordering,
  path scheduling, receive pressure, or service-level queue behavior rather
  than a simple "Rust cannot push bytes" limit.
- Raw relay behavior is still noisy. The matrix relay run delivered about
  1.33 Gbit/s, while the targeted Rust pressure relay run delivered about
  1.01 Gbit/s over three paths at the same offered rate.
- WireGuard-over-Gatherlink remains clean at the delivered ceiling, with no
  UDP loss and zero TCP retransmits in these runs. The problem is throughput
  headroom, not correctness at the measured rate.
- Next optimization work should focus on pressure-aware multipath release,
  receiver queue behavior, per-path/socket CPU locality, and packet-rate
  reduction before adding new protocol features.

## Known Evidence Before Matrix Tooling

These runs were collected before the matrix wrapper existed, but they are still
useful as current local evidence.

| Scenario | Result | Notes |
| --- | ---: | --- |
| Direct WireGuard B -> C -> A route | about 1.97 Gbit/s TCP | Baseline route through VM C using three WireGuard links on each hop. |
| Raw Gatherlink untrusted relay | clean at 1.2 Gbit/s offered | No WireGuard ordering layer; proves routed carrier path can exceed 1 Gbit/s in this VM lab. |
| WireGuard over Gatherlink relay, one active path | about 991 Mbit/s UDP, near-zero loss | Proved the combined stack can be clean when ordering pressure is removed. |
| WireGuard over Gatherlink relay, three paths after bounded flowlet control fix | 1.2 Gbit/s UDP with about 0.01% loss | Good clean point with bounded flowlet policy surviving control metadata exchange. |
| WireGuard over Gatherlink relay, WG MTU 1360 | 1.5 Gbit/s UDP with about 0.02% loss | Larger WG MTU lowered packet-rate pressure enough to improve the clean point. |
| WireGuard over Gatherlink relay, TCP at WG MTU 1360 | about 0.95-1.0 Gbit/s | TCP remains the more conservative usable-throughput signal in this topology. |

## Current Bottleneck Reading

The evidence points to packet rate and ordering sensitivity before raw byte
copy limits:

- raw Gatherlink UDP can run clean at the same rates where WireGuard over
  Gatherlink becomes sensitive
- a single WireGuard-over-Gatherlink path is clean, while blind multipath
  striping hurts ordering-sensitive WireGuard traffic
- increasing WireGuard MTU materially improves clean UDP throughput, which
  points to packet-rate pressure rather than pure byte throughput
- bounded flowlet policy is necessary so Python-owned service scheduling is not
  overwritten by control metadata, but unbounded stickiness would hide path
  aggregation problems rather than solve them
- simply increasing reorder hold can stall traffic; reorder needs bounded,
  observable behavior and should be tested with the matrix tooling rather than
  hand-tuned constants

CPU and memory are not ruled out. For future runs, collect per-node snapshots
and host counters with the benchmark scripts. If CPU is saturated during clean
single-layer baselines, allocate more VM vCPUs before changing Gatherlink
dataplane design. If `/proc/net/snmp` UDP errors climb, treat kernel/socket
buffer pressure as the bottleneck before tuning protocol logic.

## WireGuard Design Clues To Test Against

The relevant public WireGuard material points at these implementation lessons:

- WireGuard Linux kernel integration notes:
  <https://www.wireguard.com/papers/wireguard-netdev22.pdf>
- WireGuard performance roadmap:
  <https://www.wireguard.com/performance/>
- Linux UDP GSO/GRO background:
  <https://developers.redhat.com/articles/2021/11/05/improve-udp-performance-rhel-85>

- kernel WireGuard was designed around preallocated peer/device state,
  authenticated packets before state mutation, and avoiding runtime allocation
  in the hot path
- the Linux implementation work highlights GSO, queueing, zero-copy, FPU
  batching, multicore crypto, and sticky socket routing as important performance
  considerations
- WireGuard's own performance roadmap calls out GRO, lock-free queues, core
  autoscaling, CPU packet locality, and qdisc integration
- userspace `wireguard-go` can be configured with the normal `wg` tool through
  the same UAPI socket model, so it is a useful comparison for Gatherlink's
  userland dataplane
- Tailscale's userspace WireGuard work found that TUN TSO/GRO plus
  `sendmmsg`/`recvmmsg` reduced syscall and packet-rate pressure materially

Gatherlink lessons to test, not blindly assume:

- prefer fewer syscalls per unit of traffic where the kernel API supports it
- keep packet ownership and metadata allocation out of the hot path
- preserve CPU/cache locality for a flowlet without letting one flow hold a path
  forever
- separate packet-rate bottlenecks from crypto bottlenecks by comparing
  private LAN, kernel WireGuard, userspace WireGuard, raw Gatherlink, and
  WireGuard-over-Gatherlink runs

The practical Gatherlink gap list is:

- **GSO/GRO-like payload coalescing**: WireGuard can process larger packet
  bundles before segmenting. Gatherlink currently batches syscalls, but it still
  mostly treats UDP datagrams as independent work items. This is likely the
  biggest userland feature to revisit for high packet-rate relay traffic.
- **Ordered parallel crypto queues**: WireGuard assigns counters in order,
  encrypts/decrypts across cores, then releases per-peer packets in order.
  Gatherlink has compact scheduling primitives but not yet a dedicated ordered
  worker-ring for relay/core crypto.
- **Hot-path buffer ownership**: WireGuard's design avoids allocation after
  configuration. Gatherlink relay now unwraps in-place into reusable receive
  buffers, but endpoint core batching and fragmentation/coalescing should keep
  getting audited for accidental per-packet allocation.
- **CPU and socket locality**: WireGuard is deliberate about per-peer queues,
  CPU selection, and sticky routing. Gatherlink has explicit path sockets, but
  the VM tooling should grow optional CPU affinity and per-service CPU reporting
  before interpreting high-rate benchmark failures as protocol problems.
- **Kernel UDP features and buffers**: userland Gatherlink depends on socket
  buffers, `recvmmsg`/`sendmmsg`, host vSwitch behavior, and kernel UDP tuning.
  Drops in `/proc/net/snmp`, `ss -u -i`, or service counters should be captured
  with every benchmark before changing protocol logic.

## 2026-05-21 Tooling Smoke

Current tool-generated comparison:

```text
.gatherlink/hyperv-performance/20260521T052355Z-current-relay-comparison/
```

Settings:

- duration: 5 seconds
- UDP offered rate: 500 Mbit/s
- UDP block size: 1200 bytes
- Gatherlink path MTU: 1450
- WireGuard MTU: 1360
- active Gatherlink paths: a,b,c
- flowlet idle: 50000 us
- flowlet max hold: 60000000 us
- reorder hold: 150000 us

Results:

| Scenario | Result |
| --- | ---: |
| Raw Gatherlink relay UDP generator | 499.99 Mbit/s |
| Raw Gatherlink relay UDP sink | 500.28 Mbit/s, packet delta 0 |
| WireGuard over Gatherlink relay TCP | 976.02 Mbit/s, retransmits 0 |
| WireGuard over Gatherlink relay UDP | 498.22 Mbit/s, loss 0.00% |

Interpretation:

- at a modest 500 Mbit/s offered rate, the routed Gatherlink relay path is
  clean and the WireGuard-over-Gatherlink relay path is clean
- TCP-over-WireGuard through Gatherlink reached almost 1 Gbit/s even in the
  short smoke run, so the combined stack is healthy at ordinary Gbit-class
  validation rates
- this was not a max-speed run; use it as a tooling smoke and baseline sanity
  check before longer comparisons

## 2026-05-21 Endpoint Isolation And Rejected Tuning

Purpose:

Separate endpoint Gatherlink cost from relay cost, then check whether simple
constant tuning can move WireGuard-over-Gatherlink toward the direct WireGuard
baseline.

Current commands and outputs are under:

```text
.gatherlink/hyperv-performance/manual-wireguard-onehop-kernel-current/
.gatherlink/hyperv-performance/manual-wg-gl-onehop-sticky/
.gatherlink/hyperv-performance/manual-wg-gl-onehop-idle1ms/
.gatherlink/hyperv-performance/manual-wg-gl-onehop-onepath-a/
.gatherlink/hyperv-performance/manual-wg-gl-onehop-onepath-a-plaintext/
.gatherlink/hyperv-performance/manual-raw-onehop-onepath-a-after-idle1ms/
```

Useful results:

| Scenario | Topology | Setting | Result | Reading |
| --- | --- | --- | ---: | --- |
| Direct kernel WireGuard | VM B -> VM A | per private path | 4.55-4.65 Gbit/s TCP per path | Current local WireGuard speed reference. |
| WG over Gatherlink one-hop | VM B -> VM A | 3 paths, sticky flowlet, 5 ms idle sleep | 1.28 Gbit/s TCP, 1.21 Gbit/s UDP | Relay is not required to see the ceiling. |
| WG over Gatherlink one-hop | VM B -> VM A | 3 paths, sticky flowlet, 1 ms idle sleep | 1.39 Gbit/s TCP, 1.36 Gbit/s UDP | Lower idle latency helps and is kept. |
| WG over Gatherlink one-hop | VM B -> VM A | 1 path, authenticated | 1.44 Gbit/s TCP, 1.40 Gbit/s UDP | One ordered path is currently better than trying to stripe one WireGuard peer flow. |
| WG over Gatherlink one-hop | VM B -> VM A | 1 path, plaintext Gatherlink | 1.45 Gbit/s TCP, 1.59 Gbit/s UDP | AEAD/replay costs some UDP headroom, but TCP ceiling is not mainly crypto. |
| Raw Gatherlink one-hop | VM B -> VM A | 1 path, 2.5 Gbit/s offered | all packets delivered; sink rate 2.12 Gbit/s | The endpoint runner can move more raw UDP than the WireGuard-over-Gatherlink ceiling. |

Rejected or non-winning tuning:

- 200 us idle sleep lowered idle ping but hurt throughput, likely from loop
  wakeup contention.
- receiver reorder hold of 0 us did not improve WireGuard-over-Gatherlink.
- core batch size 128 and 1024 were both worse than the 512 default for this
  WireGuard shape.
- increasing Rust-side burst cycles from 64 to 256 made WireGuard behavior
  worse, likely by delaying the opposite direction.
- interleaving path receive before and after service forwarding inside one Rust
  burst did not improve the measured ceiling.
- WireGuard MTU 1280 was worse than the current 1380 setting in this lab.
- higher UDP offered rate did not lift the WG-over-Gatherlink ceiling.

Kept changes from this pass:

- the production runner idle sleep is now 1 ms, not 5 ms
- benchmark scripts can isolate one-hop WireGuard-over-Gatherlink
- one-hop scripts can switch between authenticated and plaintext Gatherlink
  for diagnosis
- services can carry a Python-owned `scheduler_path_run_datagrams` primitive so
  future scheduler policies can bound hot path runs without baking policy into
  Rust

Current bottleneck reading:

Gatherlink is still below the 90 percent WireGuard target for
WireGuard-over-Gatherlink. The newest evidence says not to chase relay-only
fixes first. The next serious optimization should profile endpoint packet
handoff: WireGuard UDP packet sizes, app-facing socket send/receive cost,
per-packet AEAD/replay cost, and Python-supervised loop wakeups under a real
WireGuard workload. Raw Gatherlink is healthy enough that this should be treated
as a combined-stack endpoint problem, not as proof that the Rust carrier path is
generally too slow.

## 2026-05-21 Userspace WireGuard Smoke

Current tool-generated comparison:

```text
.gatherlink/hyperv-performance/20260521T053154Z-wireguard-kernel-vs-userspace/
```

Settings:

- duration: 5 seconds
- UDP offered rate: 500 Mbit/s
- UDP block size: 1200 bytes
- WireGuard MTU: 1360
- active paths: a,b,c

Results:

| Scenario | Path 1 | Path 2 | Path 3 |
| --- | ---: | ---: | ---: |
| Kernel WireGuard TCP | 4252.45 Mbit/s | 4334.47 Mbit/s | 4368.77 Mbit/s |
| Kernel WireGuard UDP | 499.92 Mbit/s, 0.00% loss | 499.94 Mbit/s, 0.00% loss | 499.94 Mbit/s, 0.00% loss |
| Userspace `wireguard-go` TCP | 4163.23 Mbit/s | 4395.57 Mbit/s | 4249.49 Mbit/s |
| Userspace `wireguard-go` UDP | 499.93 Mbit/s, 0.00% loss | 499.93 Mbit/s, 0.00% loss | 499.92 Mbit/s, 0.00% loss |

Interpretation:

- `wireguard-go` is available in the Debian VM lab and can be benchmarked with
  the same `wg` configuration flow
- at 500 Mbit/s offered UDP, userspace WireGuard and kernel WireGuard are both
  clean, so this rate is a smoke test rather than a stress comparison
- the `wireguard-go` startup warning about kernel support is expected on these
  guests; verify userspace runs by checking the interface is `tun type tun` and
  that a `wireguard-go` process exists during the test

## 2026-05-21 Userspace WireGuard 2G Probe

Current tool-generated comparison:

```text
.gatherlink/hyperv-performance/20260521T053814Z-wireguard-kernel-vs-userspace-2g-rerun/
```

Settings:

- duration: 8 seconds
- UDP offered rate: 2000 Mbit/s per path
- UDP block size: 1200 bytes
- WireGuard MTU: 1360
- active paths: a,b,c

Results:

| Scenario | Path 1 | Path 2 | Path 3 |
| --- | ---: | ---: | ---: |
| Kernel WireGuard TCP | 4007.62 Mbit/s | 4088.66 Mbit/s | 4269.23 Mbit/s |
| Kernel WireGuard UDP | 1977.24 Mbit/s, 0.00% loss | 1995.64 Mbit/s, 0.00% loss | 1989.51 Mbit/s, 0.00% loss |
| Userspace `wireguard-go` TCP | 4235.87 Mbit/s | 4343.58 Mbit/s | 4295.12 Mbit/s |
| Userspace `wireguard-go` UDP | 1996.53 Mbit/s, 0.00% loss | 1999.60 Mbit/s, 0.00% loss | 1995.56 Mbit/s, 0.00% loss |

Interpretation:

- userspace WireGuard is not inherently too slow in this lab; it can carry a
  clean 2 Gbit/s UDP offered load per private path
- kernel and userspace WireGuard are close enough here that Gatherlink should
  compare itself against both, not dismiss userspace as an invalid baseline
- this strengthens the hypothesis that Gatherlink's remaining combined-stack
  limit is mostly packet-rate/syscall/ordering pressure around relay and
  WireGuard-over-Gatherlink, not merely the fact that Gatherlink is userspace
- the next Gatherlink profiling pass should measure syscall count, carrier
  datagram count, AEAD packets per second, and reorder/duplicate counters for
  the same offered traffic

## 2026-05-21 Single-Path Stack Comparison

Exploratory matrix:

```text
.gatherlink/hyperv-performance/20260521T054805Z-single-path-stack-comparison/
```

Settings:

- duration: 8 seconds
- UDP offered rate: 2000 Mbit/s
- UDP block size: 1200 bytes
- WireGuard MTU: 1360
- active paths: a

Results:

| Scenario | Result |
| --- | ---: |
| Kernel WireGuard one-hop TCP | 4713.53 Mbit/s, retransmits 3609 |
| Kernel WireGuard one-hop UDP | 1998.00 Mbit/s, 0.00% loss |
| Userspace `wireguard-go` one-hop TCP | 4777.07 Mbit/s, retransmits 3800 |
| Userspace `wireguard-go` one-hop UDP | 1999.81 Mbit/s, 0.00% loss |
| Raw Gatherlink relay UDP generator | 1999.99 Mbit/s |
| Raw Gatherlink relay UDP sink | 1360.08 Mbit/s, packet delta 184722 |
| WireGuard over Gatherlink relay TCP | 1044.08 Mbit/s, retransmits 0 |
| WireGuard over Gatherlink relay UDP | 1029.92 Mbit/s, 0.00% loss |

Interpretation:

- both kernel WireGuard and userspace `wireguard-go` carry a clean 2 Gbit/s UDP
  offered load over one private path, so userspace WireGuard itself is not the
  explanation for Gatherlink's lower combined-stack ceiling
- the raw Gatherlink number in this run was still the B -> C -> A untrusted
  relay topology with only `path-a` active; it isolates multipath ordering but
  not relay forwarding
- the immediate next benchmark needs direct two-node raw Gatherlink with
  `tools/hyperv/run_gatherlink_onehop_speed.sh` so endpoint transport cost and
  relay forwarding cost can be separated

## 2026-05-21 Single-Path With Direct Gatherlink

Tool-generated matrix:

```text
.gatherlink/hyperv-performance/20260521T055910Z-single-path-with-direct-gatherlink/
```

Settings:

- duration: 8 seconds
- UDP offered rate: 2000 Mbit/s
- UDP block size: 1200 bytes
- active paths: a
- Gatherlink path MTU: 1450 for generated configs

Results:

| Scenario | Result |
| --- | ---: |
| Kernel WireGuard one-hop TCP | 4720.06 Mbit/s, retransmits 3396 |
| Kernel WireGuard one-hop UDP | 1994.57 Mbit/s, 0.00% loss |
| Userspace `wireguard-go` one-hop TCP | 4595.12 Mbit/s, retransmits 4042 |
| Userspace `wireguard-go` one-hop UDP | 1997.31 Mbit/s, 0.00% loss |
| Raw Gatherlink one-hop UDP generator | 1999.98 Mbit/s |
| Raw Gatherlink one-hop UDP sink | 1996.18 Mbit/s, packet delta 0 |
| Raw Gatherlink relay UDP generator | 1999.80 Mbit/s |
| Raw Gatherlink relay UDP sink | 1290.35 Mbit/s, packet delta 247209 |
| WireGuard over Gatherlink relay TCP | 1032.08 Mbit/s, retransmits 10 |
| WireGuard over Gatherlink relay UDP | 1043.45 Mbit/s, 0.00% loss |

Interpretation:

- direct endpoint Gatherlink over one path is effectively at the same 2 Gbit/s
  offered UDP point as kernel and userspace WireGuard in this lab
- the one-path relay topology is the clear bottleneck: the same endpoint
  transport that is clean directly loses or queues heavily when traffic is
  wrapped through the untrusted relay hop
- the next speed work should focus on relay hop execution: per-hop AEAD
  allocation/copy cost, relay process topology, syscall batching behavior,
  queue/backlog telemetry, and CPU locality on VM C

An attempted relay runner batch increase from 256 to 1024 packets did not help
in a follow-up run:

```text
.gatherlink/hyperv-performance/20260521T060758Z-single-path-relay-batch1024/
```

The relay UDP sink fell to 1192.49 Mbit/s and WireGuard-over-Gatherlink relay
UDP fell to 831.06 Mbit/s. Treat oversized relay batches as suspect until a
dedicated queue-latency profile says otherwise.

## 2026-05-21 Relay Unwrap-Only Pass

The relay implementation was corrected from "open and reseal a hop packet" to
the intended onion-style behavior: authenticate and remove only the current
outer relay-hop envelope, then forward the remaining opaque packet. The relay
hot path now decrypts the hop envelope in-place into reusable receive buffers,
so a batch can forward slices from the same buffers instead of allocating a
fresh plaintext vector per packet.

Short one-path matrix:

```text
.gatherlink/hyperv-performance/20260521T072824Z-single-path-relay-unwrap-only/
```

Results:

| Scenario | Result |
| --- | ---: |
| Raw Gatherlink one-hop UDP generator | 1999.97 Mbit/s |
| Raw Gatherlink one-hop UDP sink | 1679.02 Mbit/s, packet delta 0 |
| Raw Gatherlink relay UDP generator | 1999.97 Mbit/s |
| Raw Gatherlink relay UDP sink | 1319.11 Mbit/s, packet delta 92048 |
| WireGuard over Gatherlink relay TCP | 1133.49 Mbit/s, retransmits 0 |
| WireGuard over Gatherlink relay UDP | 870.71 Mbit/s, 0.00% loss |

Interpretation:

- unwrap-only forwarding removes the incorrect relay semantics and reduces
  relay topology complexity by removing the extra endpoint-side exit relay
  processes
- TCP-over-WireGuard through Gatherlink improved compared with the immediately
  prior short run, but raw relay still cannot absorb a forced 2 Gbit/s single
  path without loss
- the next performance slice should be measurement tooling, not blind tuning:
  capture relay VM CPU, per-process CPU, UDP socket drops, and per-service
  counters during the same run
- the next code candidates are ordered parallel crypto/forwarding workers,
  larger coalesced payload units where safe, CPU affinity controls in VM tooling,
  and continued allocation audits in endpoint core receive/deliver paths

Follow-up after aggregating relay forwarded-packet diagnostics outside the hot
loop:

```text
.gatherlink/hyperv-performance/20260521T073702Z-single-path-relay-unwrap-diagcadence/
```

| Scenario | Result |
| --- | ---: |
| Raw Gatherlink relay UDP generator | 1999.78 Mbit/s |
| Raw Gatherlink relay UDP sink | 1311.36 Mbit/s, packet delta 100831 |
| WireGuard over Gatherlink relay TCP | 1006.95 Mbit/s, retransmits 0 |
| WireGuard over Gatherlink relay UDP | 870.13 Mbit/s, 0.00% loss |

This did not move the relay bottleneck, which is useful evidence: ordinary
forwarded-packet diagnostics were not the dominant limiter in this run. The next
benchmark pass should capture CPU and kernel UDP counters alongside throughput
before another protocol tuning attempt.

## 2026-05-21 Ordered Multipath Scheduler Pass

MPTCP research changed the scheduler direction. For one ordered service flow,
the right model is a service/global sequence space plus path selection by
predicted delivery, not blind per-packet round robin. Gatherlink can do this
without changing the packet header because the compact encrypted logical frame
already carries a global per-session/service `u64 sequence`.

The first implementation adds Python policy name `ordered_multipath` and a Rust
virtual-arrival executor. Python still owns policy and telemetry smoothing; Rust
only executes compiled latency, capacity, queue, loss, and MTU facts.

Functional VM checks:

```text
.gatherlink/hyperv-performance/20260521T094448Z-relay-rust-abc-ordered-inspect/
```

At 500 Mbit/s offered over three active relay paths, the ordered scheduler
delivered all packets:

| Scenario | Result |
| --- | ---: |
| Raw Gatherlink relay UDP generator | 500.00 Mbit/s |
| Raw Gatherlink relay UDP sink | 500.22 Mbit/s, packet delta 0 |

Stress runs showed the first policy is not yet a throughput win for a single
ordered flow:

| Scenario | Result |
| --- | ---: |
| one path, compiled UDP pressure, 1500 Mbit/s offered | 1501.25 Mbit/s sink, packet delta 0 |
| three paths, round-robin, 1500 Mbit/s offered | earlier runs stayed below the one-path clean point |
| three paths, `ordered_multipath`, 1500 Mbit/s offered | 956-1258 Mbit/s sink across trial variants, with packet delta |

Interpretation:

- the new mode is correctly wired as a Python-selected scheduler and executes in
  Rust without header growth
- the first Rust virtual-arrival primitive is too optimistic when actual relay
  jitter is higher than configured static latency facts
- path status showed even path split and very high receiver reorder counters,
  so the next pass needs live in-flight/arrival telemetry feeding Python and a
  stricter sender-side bound before using multiple paths for one WireGuard-like
  flow
- until that follow-up exists, one-path or flowlet-sticky scheduling remains the
  safer performance mode for WireGuard-over-Gatherlink

### Ordered Multipath Credit Follow-Up

The next pass added two pieces while keeping the same boundary:

- Python compiles ordered-mode reorder budgets and bandwidth-delay-product
  in-flight credits from configured or telemetry-derived capacity and latency
  facts.
- Rust only executes those already-compiled primitives with a tiny virtual
  in-flight queue. It still does not own policy and the packet header did not
  change.

The VM tooling also gained `--path-capacity-mbit` and `--reorder-hold-us auto`
so the generated configs can test realistic scheduler facts instead of forcing
every path to look like a 5 Gbit/s path with a 150 ms reorder window.

Useful runs:

| Scenario | Result |
| --- | ---: |
| `20260521T102019Z-relay-rust-abc-ordered-credit-auto-1200m` | 1200.65 Mbit/s sink, packet delta 0 |
| `20260521T102218Z-relay-rust-abc-ordered-credit-auto-1500m` | 1474.89 Mbit/s sink, packet delta 0 |
| `20260521T102942Z-relay-rust-abc-ordered-credit-auto-1600m` | 1456.34 Mbit/s sink, packet delta 62,219 |
| `20260521T102413Z-relay-rust-abc-ordered-credit-auto-1800m` | 1470.28 Mbit/s sink, packet delta 47,705 |

Interpretation:

- the credit-bound ordered scheduler now has a clean three-path relay point near
  the earlier one-path clean ceiling
- this is a real safety improvement over the first ordered implementation, which
  dropped packets at lower three-path pressure points
- it is not yet a throughput win over the clean one-path result; the next
  optimization should collect live path arrival/emit timing and CPU counters
  during the relay run so Python can lower or raise path credits from real
  delivery behavior instead of static configured capacity alone
- receiver reorder counters remain high even when application delivery is clean;
  treat this as an important signal before using ordered multipath for
  WireGuard-like single-flow traffic

## Repeatable Short Matrix

Use this when checking whether a code change moved the needle without spending
an hour:

```bash
tools/hyperv/run_performance_matrix.sh \
  --duration 10 \
  --udp-rate 1000M \
  --udp-length 1200 \
  --scenarios private-lan,gatherlink-relay-udp,wireguard-over-gatherlink-relay
```

Use the full matrix when comparing against WireGuard routing:

```bash
tools/hyperv/run_performance_matrix.sh \
  --duration 30 \
  --parallel 6 \
  --udp-rate 1200M \
  --udp-length 1200
```

For exploratory WireGuard-over-Gatherlink tuning, change one variable at a
time:

```bash
tools/hyperv/run_relay_wireguard_speed.sh \
  --duration 20 \
  --udp-rate 1500M \
  --udp-length 1200 \
  --wg-mtu 1360 \
  --path-mtu 1450 \
  --flowlet-idle-us 50000 \
  --flowlet-max-hold-us 60000000
```

Record the resulting `report.md` path and any bottleneck conclusion in this
file only after the run has been reviewed.

## v0.9.2 Performance Regression Pass

This pass investigated the speed drop seen after the v0.9.2 transport,
relay, scheduler, and helper work. The goal was to preserve the features while
recovering the earlier clean Hyper-V VM performance.

Important runs:

| Run | Shape | Result |
| --- | --- | ---: |
| `manual-relay-probed-2g-fixed` | raw Gatherlink B -> C -> A relay, 3 paths, 2 Gbit/s offered, old test sink buffers | service received all packets; app sink missed 70,855 packets |
| `manual-relay-probed-2g-buffered-tool` | same, but `udp_pressure` requests large UDP buffers | service received all packets; app sink received 1,773,377 of 1,785,702 packets |
| `manual-relay-run256-2g` | same, but Rust path burst run lowered from 4096 to 256 | worse app delivery; rejected |
| `manual-relay-burst256-2g` | same, but Python/Rust outer burst cycles raised from 64 to 256 | worse app delivery; rejected |
| `manual-wg-gl-onepath-default` | WireGuard over Gatherlink relay, 1 path | TCP 1166 Mbit/s, UDP 1200 Mbit/s |
| `manual-wg-gl-threepath-default-2` | WireGuard over Gatherlink relay, 3 paths, sticky flowlet | TCP 1087 Mbit/s, UDP 1151 Mbit/s |
| `manual-wg-gl-flowhold-5000` | WireGuard over Gatherlink, 3 paths, 5 ms max flowlet hold | TCP 714 Mbit/s, many retransmits |
| `manual-wg-gl-noflow-reorder50000` | WireGuard over Gatherlink, 3 paths, no flowlet, 50 ms reorder hold | TCP 1257 Mbit/s, UDP 841 Mbit/s |
| `manual-wg-gl-ordered-cap800` | WireGuard over Gatherlink, 3 paths, no flowlet, ordered scheduler, 800 Mbit/s path hint | TCP 1156 Mbit/s, UDP 918 Mbit/s |
| `manual-wg-gl-capacity-cap800` | WireGuard over Gatherlink, 3 paths, no flowlet, capacity scheduler, 800 Mbit/s path hint | TCP 1203 Mbit/s, UDP 906 Mbit/s |
| `manual-wg-gl-noflow-rr-250000` | WireGuard over Gatherlink, 3 paths, no flowlet, 250 ms reorder hold | TCP 1133 Mbit/s, UDP 975 Mbit/s |
| `manual-wg-gl-noflow-rr-500000` | WireGuard over Gatherlink, 3 paths, no flowlet, 500 ms reorder hold | TCP 1101 Mbit/s, UDP 1196 Mbit/s |
| `manual-wg-onehop-onepath-a-jumbo9000-5gudp` | direct kernel WireGuard, 1 path, link MTU 9000, WG MTU 8500 | TCP 11174 Mbit/s, UDP 5000 Mbit/s offered clean |
| `manual-gl-raw-onehop-onepath-a-jumbo9000-plain-8g` | raw Gatherlink, 1 path, plaintext, link MTU 9000 | 8001 Mbit/s delivered, 0 packet delta |
| `manual-gl-raw-onehop-onepath-a-jumbo9000-restored-5g` | raw Gatherlink, 1 path, authenticated, link MTU 9000 | 5000 Mbit/s delivered, 0 packet delta |
| `manual-gl-raw-onehop-onepath-a-jumbo9000-native-55g` | raw Gatherlink, 1 path, authenticated, native Rust build, link MTU 9000 | 5500 Mbit/s delivered, 0 packet delta |
| `manual-gl-raw-onehop-onepath-a-jumbo9000-native-6g` | same as above, 6 Gbit/s offered | 5073 Mbit/s delivered; first degraded point |
| `manual-wg-gl-onehop-onepath-a-jumbo9000-plain-native` | WireGuard over Gatherlink, 1 path, plaintext Gatherlink, native Rust build, jumbo | TCP 5313 Mbit/s, UDP 4985 Mbit/s clean |
| `manual-wg-gl-onehop-onepath-a-jumbo9000-native` | WireGuard over Gatherlink, 1 path, authenticated Gatherlink, native Rust build, jumbo | TCP 3699 Mbit/s, UDP 3767 Mbit/s clean |

Findings:

- Native Rust builds matter. Rebuilding the VM bindings with
  `RUSTFLAGS="-C target-cpu=native"` restored the one-hop raw Gatherlink path
  to a clean 2 Gbit/s offered-rate run.
- The old 150 ms benchmark reorder default was too high for local multipath
  relay testing. A 2 ms hold matches the intended low-latency lab default and
  avoids huge receiver-side queues during high-rate tests.
- The Hyper-V path MTU benchmark default should use 1472 bytes for the private
  UDP path. For WireGuard-over-Gatherlink, the best balanced tested defaults
  are path MTU 1472, WireGuard MTU 1380, and iperf UDP length 1300.
- The benchmark `tools/udp_pressure.rs` sink was a measurement bottleneck
  because it did not request large UDP socket buffers. Production Gatherlink
  sockets already did this; the test tool now does too.
- Lowering Rust's burst path send run from 4096 to 256 made raw relay delivery
  worse. Raising the Python/Rust outer burst cycles from 64 to 256 also made
  raw relay delivery worse. Both tuning attempts were rejected.
- Sticky WireGuard flowlets keep the combined WireGuard-over-Gatherlink case
  clean, but they also prevent a single WireGuard UDP peer flow from using all
  three paths effectively.
- Aggressive striping of a single WireGuard UDP flow currently hurts inner TCP.
  Short flowlet holds and no-flowlet modes produced more retransmits or lower
  UDP throughput. This is not a packet-header issue; it is a sender pacing,
  reorder, and congestion-control interaction.
- Passing realistic per-path capacity hints into the WireGuard relay benchmark
  did not make the existing ordered/capacity modes aggregate one WireGuard flow
  well enough. Very large receiver reorder holds reduce some retransmits but do
  not create a throughput win; they mostly trade latency and queueing for a
  similar 1.1-1.2 Gbit/s result.
- A benchmark-only `--link-mtu` knob now exists for one-hop Gatherlink,
  WireGuard, and WireGuard-over-Gatherlink scripts. This keeps normal-MTU and
  jumbo experiments repeatable instead of relying on manual VM interface state.
- Jumbo MTU separates packet-rate limits from crypto limits. Plaintext
  Gatherlink carried the full 8 Gbit/s raw offered rate on one path, proving
  the socket/framing path can move the bytes. Authenticated Gatherlink was
  clean at 5.5 Gbit/s with a native Rust build and degraded at 6 Gbit/s, making
  AEAD/replay the current one-path jumbo ceiling.
- Lowering the AEAD worker split threshold from 4096 frames to 256 frames made
  authenticated jumbo delivery worse on the two-vCPU VMs and was reverted.
  Scoped thread creation is not the next useful lever for ordinary runtime
  batches in this lab.
- `cargo add ring` could not be evaluated in the offline VM/WSL environment
  because crates.io DNS was unavailable. The next fair crypto experiment is an
  assembly-backed AEAD backend comparison, not a protocol shortcut.

Current interpretation:

- Raw Gatherlink relay is healthy again for the clean local lab shape. At
  2 Gbit/s offered, the service received every packet in the buffered-tool run;
  remaining loss was at the temporary local UDP pressure sink.
- Raw one-hop Gatherlink with jumbo MTU and plaintext can carry at least
  8 Gbit/s in this VM lab. With authenticated transport and a native Rust
  build, the clean one-path jumbo ceiling is currently about 5.5 Gbit/s.
- WireGuard-over-Gatherlink is still not at the desired 90 percent of direct
  WireGuard for all scenarios. In jumbo one-hop tests it can match a 5 Gbit/s
  UDP offer when Gatherlink is plaintext, but authenticated Gatherlink drops
  that to roughly 3.8 Gbit/s. The next real optimization should not retune
  constants blindly. It should compare optimized AEAD backends and add a
  designed single-flow multipath mode with bounded sender-side in-flight work,
  receiver reorder feedback, and an explicit policy decision from Python about
  when a WireGuard-like flow is allowed to stripe.
- Keep flowlet-sticky scheduling as the safe default for WireGuard-like helper
  traffic until that single-flow multipath mode is proven.

2026-05-21 ordered-multipath follow-up:

- A config-normalization bug meant raw JSON configs with a top-level
  `scheduler.mode` could validate but expand to the default `round_robin`
  runtime mode. The CLI/runtime path now preserves that scheduler policy, and
  tests cover the `validate_config_dict -> expand_config` path.
- Rust burst forwarding had a second shortcut that reused the current batch
  path before consulting the ordered scheduler. Ordered multipath now forces a
  per-datagram path decision first; adjacent same-path datagrams can still be
  coalesced afterward.
- In the shaped 300/500/700 Mbit/s Hyper-V run, ordered mode now sends in the
  intended capacity shape. Jumbo 8200-byte app payloads over 9000 MTU delivered
  about 1485 Mbit/s from a 1500 Mbit/s offer with only about 260 packets of
  application delta.
- Normal-MTU 1400-byte app payloads also split correctly by capacity, but the
  Python UDP benchmark sink/harness is no longer a reliable high-packet-rate
  truth source at that scale. The receiver service saw the carrier frames, but
  sink completion lagged behind the harness. Future normal-MTU comparisons
  should use the buffered Rust pressure tool or iperf-style sinks and should
  record sink CPU plus Linux UDP counters.
- Receiver reorder work is now bounded. If missing earlier sequence numbers
  would otherwise let the reorder buffer grow without bound, Rust releases the
  oldest buffered payload after the bounded work limit instead of trading
  correctness of UDP ordering hints for unbounded memory and latency.

2026-05-22 benchmark-tool validation:

- `tools/hyperv/run_private_lan_speed.sh` now records three raw-LAN views in one
  report: per-path `iperf3`, simultaneous `iperf3 -u`, and simultaneous
  `tools/udp_pressure.rs`. This keeps Gatherlink-specific UDP pressure testing
  anchored to an independent iperf baseline.
- `tools/hyperv/run_wireguard_onehop_speed.sh` now records the same
  simultaneous `udp_pressure` view through kernel or userspace WireGuard. This
  proves the internal UDP generator behaves like the iperf reference after
  encapsulation, not only on raw LAN.
- Pacing validation: with 300 Mbit/s offered per path at normal MTU, iperf3 UDP
  and `udp_pressure` both reported about 300 Mbit/s per path with 0 percent
  loss. With 1000 Mbit/s offered per path at jumbo MTU, both reported about
  1000 Mbit/s per path with 0 percent loss.
- Unbounded validation: on one raw private-LAN path with 1500 MTU and 1200-byte
  UDP payloads, unbounded iperf3 UDP reported about 4.8-5.2 Gbit/s while
  unpaced `udp_pressure` reported about 3.79 Gbit/s end-to-end. The internal
  tool is more conservative for small packets, but stable and well above the
  Gatherlink normal-MTU numbers being investigated.
- Unbounded jumbo validation: on one raw private-LAN path with 9000 MTU and
  8200-byte UDP payloads, unbounded iperf3 UDP reported about 18.8-19.4 Gbit/s
  while unpaced `udp_pressure` reported about 17.4 Gbit/s end-to-end. At jumbo
  sizes the internal tool is close enough to iperf3 to be a useful max-speed
  stress generator.
- WireGuard smoke validation: on one kernel WireGuard path with 1500 link MTU,
  1360 WireGuard MTU, and 1200-byte UDP payloads at 200 Mbit/s, iperf3 reported
  199.95-199.96 Mbit/s with 0 percent loss and `udp_pressure` reported
  200.00 Mbit/s generated with 200.03 Mbit/s received.
- Production receiver-feedback smoke:
  `.gatherlink/hyperv-performance/20260522T014452Z-onehop-feedback-status-smoke/`
  confirmed `path_pressure_count=3` in production runner status on both peers.
  The follow-up normal-MTU ordered run at 1500 Mbit/s,
  `.gatherlink/hyperv-performance/20260522T014541Z-onehop-ordered-normal-prod-feedback-1500/`,
  delivered all 669,611 generated packets with zero application packet delta.
- Conclusion: `udp_pressure` is valid for Gatherlink benchmarking, but reports
  a slightly lower small-packet ceiling than iperf3. Keep iperf3 in the raw LAN
  and raw WireGuard baselines so future performance claims are not based only
  on project-owned tooling.

2026-05-27 UDP pressure sink correction:

- A later pure-WireGuard check showed the earlier high-rate `udp_pressure`
  small-packet receive ceiling was partly a tool artifact. The sender could
  generate the requested rate, but the sink used one `recv_from` syscall per
  datagram and under-reported against equivalent `iperf3 -u` receives.
- `tools/udp_pressure.rs` now uses batched Linux `recvmmsg` receives when
  available, with the portable `recv_from` path kept as a fallback. This keeps
  the benchmark tool closer to the way a high-rate UDP application should read.
- Fresh userspace-WireGuard, clean three-path simultaneous run, path MTU 1500,
  WireGuard MTU 1380, 1200-byte UDP payloads, 2800 Mbit/s offered per path:
  `udp_pressure` sink summed 2544.20 Mbit/s and simultaneous `iperf3 -u` summed
  2551.68 Mbit/s. That is 99.7 percent of the independent iperf reference for
  the same simultaneous shape. The single-path-alone iperf rows in the same
  report are intentionally higher and are not the fair comparison for a
  simultaneous multipath pressure run.
- Fresh kernel-WireGuard run with the same shape: `udp_pressure` sink summed
  3254.13 Mbit/s and simultaneous `iperf3 -u` summed 3591.67 Mbit/s, or
  90.6 percent of the independent iperf reference. This is close enough for
  pressure testing, but keep both rows in reports when making performance
  claims.
- Follow-up parallel-flow check: `udp_pressure` now supports `--flows`,
  `--workers`, and port striping so it can be compared to multi-stream UDP
  baselines instead of only one UDP 4-tuple per path. In a clean userspace
  WireGuard run with `iperf3 -u -P8`, `udp_pressure --flows 8`, 1200-byte
  payloads, path MTU 1500, and WireGuard MTU 1380, `udp_pressure` summed
  2109.69 Mbit/s and simultaneous UDP iperf summed 2135.75 Mbit/s, or
  98.8 percent of the matching UDP iperf reference.
- The same run's TCP `iperf3 -P8` rows summed 8053.73 Mbit/s. That is a TCP
  path-set ceiling, not a UDP pressure target: the matching multi-stream UDP
  iperf result is about 2.1 Gbit/s in this userspace-WireGuard/normal-MTU lab
  shape. Use TCP rows to compare TCP-over-WireGuard behavior, and UDP rows to
  validate raw UDP pressure tooling.
- Optional feedback check: with `PERF_UDP_PRESSURE_FEEDBACK=1`, eight
  port-striped pressure flows, and eight sink workers, `udp_pressure` summed
  2117.69 Mbit/s while matching `iperf3 -u -P8` summed 2130.43 Mbit/s, or
  99.4 percent of the independent UDP reference. Feedback is useful as an
  optional receiver-paced stress mode, but it does not turn UDP pressure into
  the TCP `iperf3 -P8` ceiling. The same run's TCP rows summed
  8156.56 Mbit/s.
- Send batching and UDP GSO follow-up: Linux `sendmmsg` batching is now used by
  the pressure sender, and UDP GSO is available behind
  `--udp-gso-segments N` / `PERF_UDP_PRESSURE_GSO_SEGMENTS=N`. With eight
  flows, eight sink workers, feedback enabled, normal MTU, and GSO at eight
  1200-byte segments, userspace WireGuard received 6034.80 Mbit/s across the
  three paths. Same-run TCP `iperf3 -P8` summed 8171.49 Mbit/s, so this
  pressure shape reached 73.9 percent of the TCP path-set ceiling while greatly
  exceeding the matching UDP iperf row. GSO at sixteen segments was lower at
  5893.14 Mbit/s, so eight is the better tested value for this lab shape so
  far.
- Flow-paced near-MTU follow-up: the sender now reuses its `sendmmsg` batch
  structures, divides aggregate feedback/target rate across flows, and exposes
  receive-batch tuning. The WireGuard benchmark script also accepts
  `--udp-length auto` plus `--udp-payload-margin BYTES`; with WG MTU 1420 this
  resolves to a 1392-byte UDP payload (`wg_mtu - 28`). In the best repeat from
  this pass, eight flows/workers, `send-batch=128`, `GSO=8`, feedback enabled,
  normal path MTU, and auto UDP length delivered 7006.05 Mbit/s across the
  three userspace-WireGuard paths. Same-run TCP `iperf3 -P8` summed
  7174.39 Mbit/s, so that pressure shape reached 97.7 percent of the TCP
  path-set ceiling. Repeat checks did not hold that ratio: the same shape
  delivered 6594.65 Mbit/s against an 8273.01 Mbit/s TCP same-run ceiling
  (79.7 percent), and bounded 2800 Mbit/s-per-path pressure delivered
  6354.19 Mbit/s against an 8339.38 Mbit/s TCP same-run ceiling (76.2 percent).
  Treat the 97.7 percent row as a useful outlier that proves the shape can get
  close under favorable host scheduling, not as the stable expected result.
- Follow-up probe and bounded-feedback work: `udp_pressure` gained feedback
  initial/max caps so a run can avoid both the initial flood and feedback-driven
  collapse. Hyper-V scripts can set `PERF_COLLECT_NODE_PROBES=1` to capture CPU,
  process, and path-interface counters during the pressure phase. In
  `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-probe-v2/`,
  the near-MTU GSO pressure shape delivered 7001.34 Mbit/s against an
  8443.61 Mbit/s same-run TCP path-set ceiling (82.9 percent). Probe data
  showed `udp_pressure` senders at about 6-7 percent of one core each, sink
  processes at about 22 percent of one core each, and `wireguard-go` processes
  around 56-84 percent of one core per path. Current evidence says the benchmark
  generator is no longer the main ceiling; userspace WireGuard and receive-side
  processing dominate this lab shape.
- Evidence:
  `.gatherlink/hyperv-performance/20260527T-v094-direct-wg-userspace-clean-udp-pressure-2800m-batched-sink/`
  and
  `.gatherlink/hyperv-performance/20260527T-v094-direct-wg-kernel-clean-udp-pressure-2800m-batched-sink/`.
  Parallel-flow evidence:
  `.gatherlink/hyperv-performance/20260527T-v094-direct-wg-userspace-clean-highudp-8000m-iperfudp8-pressure8/`.
  Feedback evidence:
  `.gatherlink/hyperv-performance/20260527T-v094-direct-wg-userspace-clean-udp-pressure-unbounded-feedback8/`.
  GSO evidence:
  `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-gso8-feedback/`
  and `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-gso16-feedback/`.
  Flow-paced near-MTU evidence:
  `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-gso8-auto-wgmtu1420-feedback/`
  and
  `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-gso8-send128-auto-wgmtu1420-feedback/`.
  Probe evidence:
  `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-userspace-probe-v2/`.

Repeatable commands from this pass:

```bash
RUSTFLAGS="-C target-cpu=native" \
  .venv/bin/maturin develop --manifest-path crates/pybindings/Cargo.toml --release

tools/hyperv/run_relay_rust_udp_speed.sh \
  --duration 10 \
  --target-mbit 2000 \
  --payload-size 1400 \
  --active-paths a,b,c

tools/hyperv/run_relay_wireguard_speed.sh \
  --duration 10 \
  --parallel 6 \
  --udp-rate 2000M \
  --active-paths a,b,c
```

## 2026-05-22 Local Scheduler Matrix Follow-Up

Evidence:

```text
.gatherlink/lab-profile-runs/full-wan-scheduler-matrix-profile-defaults-pass1/
.gatherlink/lab-profile-runs/burst-cycles-8-targeted/
.gatherlink/lab-profile-runs/burst-cycles-4-targeted/
.gatherlink/lab-profile-runs/burst-cycles-2-targeted/
.gatherlink/lab-profile-runs/full-wan-scheduler-matrix-profile-defaults-pass2/
.gatherlink/lab-profile-runs/starlink-plus-5g-updated-threshold-pass1/
```

The profile-default matrix was rerun after the lab hot-path drain changes. A
short 6 second full pass showed transient shaped-queue pressure on the
300/500/700 profile, so the follow-up separated the lab-supervisor burst count
from scheduler policy. Lowering aggregate drain cycles from 8 to 4 or 2 did not
help; it starved the 300/500/700 profile while the current 8-cycle default hit
target in the focused run. The override remains available as a bounded lab-only
diagnostic knob, not a production setting.

Focused burst-cycle comparison:

| cycles | profile | scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| ---: | --- | --- | ---: | ---: | --- | --- | --- |
| 8 | `acceptance-300-500-700` | `capacity_aware` | 1502.3 | 96.9% | target | 302.2/500.8/699.3 | 9173/18132/26867 |
| 4 | `acceptance-300-500-700` | `capacity_aware` | 1341.7 | 86.6% | pass | 270.9/447.7/623.1 | 46120/79301/112539 |
| 2 | `acceptance-300-500-700` | `capacity_aware` | 1341.2 | 86.5% | pass | 270.9/447.6/622.7 | 46231/79550/112886 |
| 8 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | 2681.9 | 92.5% | target | 584.9/926.0/1171.0 | 0/0/0 |
| 4 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | 2900.0 | 100.0% | target | 584.9/926.0/1389.1 | 0/0/0 |
| 2 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | 2900.0 | 100.0% | target | 584.9/926.0/1389.1 | 0/0/0 |

The longer 10 second matrix is the better current scheduler evidence:

| profile | MTU | payload | best scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | 1200 | 1200 | `latency_guarded_capacity` warm | 1543.7 | 99.6% | target | 310.0/515.2/718.5 | 0/2667/8857 |
| `acceptance-uneven-high` | 1452 | 1438 | `round_robin` warm | 2626.3 | 90.6% | target | 695.8/967.7/962.7 | 237876/0/0 |
| `realworld-fiber-plus-5g` | 1200 | 1200 | `capacity_aware` warm | 915.6 | 83.2% | pass | 703.7/137.1/74.9 | 266867/62480/27740 |
| `realworld-starlink-plus-5g` | 1200 | 1200 | `capacity_aware` warm | 243.6 | 74.9% | overload below pass | 124.2/106.4/12.9 | 124940/33486/4448 |
| `realworld-starlink-plus-2x5g` | 1200 | 1200 | `latency_guarded_capacity` cold | 318.2 | 75.8% | pass | 124.9/114.1/79.2 | 121248/57732/24385 |

Reading:

- The lab default drain bound should stay at 8 cycles. Lower values are useful
  as diagnostics, but they do not improve the current acceptance gate.
- `latency_guarded_capacity` is the best normal-MTU scheduler for the clean
  300/500/700 profile in this run. `capacity_aware` remains the best family for
  lossy real-world facsimiles.
- `acceptance-uneven-high` still behaves like a host/runtime ceiling probe. Its
  best row hits target only by overfilling `path-a`; the no-drop capacity-aware
  rows are close but below target in the full matrix. Use the focused high-clean
  runs when checking raw lab hot-path capacity, and use the full matrix to spot
  scheduler stability under repeated runs.
- `realworld-starlink-plus-5g` at the old 325 Mbit/s pressure was an overload
  test, not a fair scheduler target. Follow-up pressure sweeps showed useful
  delivery tops out around 243 Mbit/s; the current threshold file uses 250
  Mbit/s pressure and 245 Mbit/s expected capacity for that facsimile.

Updated Starlink+one-5G threshold validation:

| scheduler | cache | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | --- | ---: | ---: | --- | --- | --- |
| `capacity_aware` | warm | 230.5 | 92.2% | target | 124.7/94.1/11.7 | 36619/1188/233 |
| `latency_guarded_capacity` | warm | 230.4 | 92.2% | target | 124.7/94.0/11.7 | 36562/1277/225 |
| `adaptive` | warm | 230.3 | 92.1% | target | 124.3/94.4/11.6 | 36767/1238/265 |
| `flowlet_adaptive` | warm | 230.3 | 92.1% | target | 124.3/94.4/11.6 | 36768/1190/256 |

Additional useful-pressure sweeps for the other real-world facsimiles:

```text
.gatherlink/lab-profile-runs/fiber-plus-5g-pressure-900/
.gatherlink/lab-profile-runs/fiber-plus-5g-pressure-950/
.gatherlink/lab-profile-runs/fiber-plus-5g-pressure-1000/
.gatherlink/lab-profile-runs/fiber-plus-5g-pressure-1050/
.gatherlink/lab-profile-runs/fiber-plus-5g-pressure-1100/
.gatherlink/lab-profile-runs/starlink-plus-2x5g-pressure-325/
.gatherlink/lab-profile-runs/starlink-plus-2x5g-pressure-350/
.gatherlink/lab-profile-runs/starlink-plus-2x5g-pressure-375/
.gatherlink/lab-profile-runs/starlink-plus-2x5g-pressure-400/
.gatherlink/lab-profile-runs/starlink-plus-2x5g-pressure-420/
```

| profile | pressure | best scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | ---: | --- | ---: | ---: | --- | --- | --- |
| `realworld-fiber-plus-5g` | 900 | `capacity_aware` | 898.1 | 99.8% | target | 688.6/137.1/72.5 | 712/907/785 |
| `realworld-fiber-plus-5g` | 950 | `adaptive` | 933.6 | 98.3% | target | 718.5/138.7/76.3 | 17120/12176/1069 |
| `realworld-fiber-plus-5g` | 1000 | `capacity_aware` | 929.6 | 93.0% | target | 715.3/138.6/75.6 | 96593/28359/10204 |
| `realworld-fiber-plus-5g` | 1050 | `capacity_aware` | 926.7 | 88.3% | pass | 712.9/138.6/75.2 | 175481/44131/19022 |
| `realworld-starlink-plus-2x5g` | 325 | `adaptive` | 304.5 | 93.7% | target | 124.2/110.0/70.3 | 35768/1426/1230 |
| `realworld-starlink-plus-2x5g` | 350 | `flowlet_adaptive` | 315.6 | 90.2% | target | 124.7/115.1/75.9 | 57174/8005/1304 |
| `realworld-starlink-plus-2x5g` | 375 | `capacity_aware` | 319.8 | 85.3% | pass | 124.7/115.1/80.0 | 80745/24583/3135 |
| `realworld-starlink-plus-2x5g` | 400 | `flowlet_adaptive` | 318.9 | 79.7% | pass | 124.7/114.6/79.5 | 102674/43017/15142 |

These thresholds are intentionally useful-delivery gates, not maximum-overload
tests. Keep the higher-pressure rows as overload evidence and regression
signals, but do not require them to hit the normal scheduler target.

Final useful-threshold matrix after the clean-profile queue fix:

```text
.gatherlink/lab-profile-runs/full-wan-scheduler-matrix-clean-queue-pass1/
```

| profile | best scheduler | sink rx | delivery | gate | target rows | path rx a/b/c | drops a/b/c |
| --- | --- | ---: | ---: | --- | ---: | --- | --- |
| `acceptance-300-500-700` | `ordered_multipath_capacity_aware` warm | 1550.0 | 100.0% | target | 5 | 340.8/510.2/699.0 | 0/0/0 |
| `acceptance-uneven-high` | `capacity_aware` warm | 2900.0 | 100.0% | target | 4 | 621.4/932.1/1346.4 | 0/0/0 |
| `realworld-fiber-plus-5g` | `capacity_aware` warm | 934.3 | 98.3% | target | 4 | 719.0/139.1/76.2 | 15658/11994/1235 |
| `realworld-starlink-plus-5g` | `flowlet_adaptive` warm | 230.7 | 92.3% | target | 4 | 124.6/94.5/11.6 | 36178/1125/249 |
| `realworld-starlink-plus-2x5g` | `flowlet_adaptive` warm | 315.8 | 90.2% | target | 4 | 124.4/115.5/75.9 | 57678/7145/1246 |

Each profile now has at least one target-reaching scheduler in the local WAN
facsimile matrix. The short 300/500/700 row is a scheduler comparison probe;
with a 1550 Mbit/s UDP generator aimed at 1500 Mbit/s of shaped path capacity,
longer runs can still show qdisc drops because the sender does not back off.
The clean-profile success criteria are useful delivery and a path split close
to 300/500/700, not a promise that over-capacity UDP never drops. The lossy
real-world facsimiles still show expected qdisc drops because those profiles
intentionally include constrained queues, jitter, and loss.

Longer clean-profile overpressure check:

```text
.gatherlink/lab-profile-runs/acceptance-300-long-clean-queue-check/
```

| duration | scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| ---: | --- | ---: | ---: | --- | --- | --- |
| 30s | `capacity_aware` warm | 1475.7 | 95.2% | target | 295.9/492.3/687.6 | 0/56162/131173 |
| 30s | `ordered_multipath_capacity_aware` warm | 1497.3 | 96.6% | target | 299.8/498.6/700.7 | 0/0/11368 |

That longer check is a better signal for ordered single-flow tuning than the
short matrix alone: ordered capacity-aware keeps the intended path split and
has far fewer drops under sustained slight overpressure.

First `coordinated_adaptive` smoke matrix:

```text
.gatherlink/lab-profile-runs/profile-shape-hints-useful-pass1/
```

| profile | scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | --- | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | `coordinated_adaptive` warm | 1529.7 | 98.7% | target | 310.0/516.7/703.0 | 0/0/36881 |
| `realworld-fiber-plus-5g` | `coordinated_adaptive` warm | 932.5 | 98.2% | target | 717.5/138.8/76.2 | 18849/12560/1246 |
| `realworld-starlink-plus-5g` | `coordinated_adaptive` warm | 230.2 | 92.1% | target | 124.4/94.1/11.7 | 37162/1173/244 |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive` warm | 315.3 | 90.1% | target | 124.3/115.4/75.6 | 59076/6707/1253 |

This first coordinator pass is intentionally conservative. It behaves like the
known-good capacity fallback for these WAN profiles rather than jumping into
ordered mode too early. That is the desired starting point: tune the transition
thresholds with evidence, not by making `coordinated_adaptive` chase every
short-run winner.

This pass also includes the lab-profile startup hint fix: the benchmark wrapper
copies the selected profile's delay/jitter shape onto temporary paths before
services start. The lab still applies the same shape with `tc`; the copy only
keeps Python scheduler startup facts aligned with the scenario being measured.

Full `coordinated_adaptive` comparison against the main scheduler set:

```text
.gatherlink/lab-profile-runs/coordinated-adaptive-full-normal-pass1/
.gatherlink/lab-profile-runs/coordinated-adaptive-full-jumbo-pass1/
```

Normal-MTU WAN profile summary:

| profile | best scheduler | best sink rx | coordinated sink rx | coordinated vs best | coordinated gate | coordinated path rx a/b/c | coordinated drops a/b/c |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | `latency_guarded_capacity` | 1546.5 | 1515.1 | 98.0% | target | 310.0/514.2/691.0 | 0/4742/59178 |
| `acceptance-uneven-high` | `latency_guarded_capacity` | 2900.0 | 2900.0 | 100.0% | target | 621.4/932.1/1346.4 | 0/0/0 |
| `realworld-fiber-plus-5g` | `capacity_aware` | 934.3 | 933.3 | 99.9% | target | 718.2/138.9/76.2 | 17340/12223/1268 |
| `realworld-starlink-plus-5g` | `capacity_aware` | 230.5 | 230.3 | 99.9% | target | 124.6/94.1/11.7 | 36737/1200/251 |
| `realworld-starlink-plus-2x5g` | `flowlet_adaptive` | 315.7 | 315.4 | 99.9% | target | 124.5/115.3/75.6 | 58687/7013/1316 |

Explicit `coordinated_adaptive` normal-MTU rows:

| profile | scheduler | cache | path cap a/b/c | offered | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | `coordinated_adaptive` | warm | 300/500/700 | 1550 | 1515.1 | 97.8% | target | 310.0/514.2/691.0 | 0/4742/59178 |
| `acceptance-uneven-high` | `coordinated_adaptive` | warm | 600/900/1300 | 2900 | 2900.0 | 100.0% | target | 621.4/932.1/1346.4 | 0/0/0 |
| `realworld-fiber-plus-5g` | `coordinated_adaptive` | warm | 800/160/85 | 950 | 933.3 | 98.2% | target | 718.2/138.9/76.2 | 17340/12223/1268 |
| `realworld-starlink-plus-5g` | `coordinated_adaptive` | warm | 180/120/15 | 250 | 230.3 | 92.1% | target | 124.6/94.1/11.7 | 36737/1200/251 |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive` | warm | 180/140/90 | 350 | 315.4 | 90.1% | target | 124.5/115.3/75.6 | 58687/7013/1316 |

Jumbo synthetic summary:

| profile | best scheduler | best sink rx | coordinated sink rx | coordinated vs best | coordinated gate | coordinated path rx a/b/c | coordinated drops a/b/c |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | `coordinated_adaptive` | 1550.0 | 1550.0 | 100.0% | target | 310.0/516.7/723.3 | 0/0/0 |
| `acceptance-uneven-high` | `coordinated_adaptive` | 2857.7 | 2857.7 | 100.0% | target | 621.4/919.7/1316.5 | 0/1899/4562 |

Explicit `coordinated_adaptive` jumbo rows:

| profile | scheduler | cache | path cap a/b/c | offered | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `acceptance-300-500-700` | `coordinated_adaptive` | warm | 300/500/700 | 1550 | 1550.0 | 100.0% | target | 310.0/516.7/723.3 | 0/0/0 |
| `acceptance-uneven-high` | `coordinated_adaptive` | warm | 600/900/1300 | 2900 | 2857.7 | 98.5% | target | 621.4/919.7/1316.5 | 0/1899/4562 |

Reading: the coordinator is already safe as a conservative auto policy. It does
not beat every specialist in every profile, and it should not be tuned against
that standard. The next useful improvement is narrower: teach it when the clean
300/500/700 profile should move from capacity fallback toward
`latency_guarded_capacity` or ordered capacity-aware based on sustained drop and
path-split evidence, without making lossy WAN facsimiles jump into ordered mode.

Ordered sustained-pressure follow-up after treating `reorder_policies.max_hold`
as a cap instead of the actual clean-path hold:

```text
.gatherlink/lab-profile-runs/ordered-vs-capacity-post-commit-pass1/
```

| duration | scheduler | sink rx | delivery | gate | path rx a/b/c | drops a/b/c |
| ---: | --- | ---: | ---: | --- | --- | --- |
| 30s | `capacity_aware` warm | 1501.7 | 96.9% | target | 300.8/501.0/699.9 | 0/22673/84458 |
| 30s | `ordered_multipath_capacity_aware` warm | 1440.1 | 92.9% | target | 297.7/477.5/665.1 | 0/0/0 |

This is the right ordered-pressure shape for now: ordered capacity-aware is a
little lower in delivered rate, but it trades throughput for clean delivery:
the path split remains near 300/500/700 and it avoids the sustained qdisc drops
seen in unordered capacity under the same slight overpressure.

Follow-up queue-limit A/B for the clean 300/500/700 profile:

```text
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-4096/
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-8192/
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-16384/
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-32768/
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-65536/
.gatherlink/lab-profile-runs/acceptance-300-clean-limit-131072/
```

| clean queue limit | best scheduler | sink rx | delivery | path rx a/b/c | drops a/b/c |
| ---: | --- | ---: | ---: | --- | --- |
| 4096 | `ordered_multipath_capacity_aware` | 1423.4 | 91.8% | 324.8/500.8/597.8 | 127761/35398/75775 |
| 32768 | `ordered_multipath_capacity_aware` | 1539.8 | 99.3% | 329.1/522.4/688.3 | 13886/0/5840 |
| 65536 | `ordered_multipath_capacity_aware` | 1550.0 | 100.0% | 338.3/510.3/701.3 | 0/0/0 |
| 131072 | `latency_guarded_capacity` | 1550.0 | 100.0% | 310.0/516.7/723.3 | 0/0/0 |

The clean profile is a synthetic capacity probe, not a forced-drop or
realistic-buffer test, so it now uses a larger qdisc limit. Real-world facsimile
profiles keep their smaller queues to preserve jitter/loss behavior.

## 2026-05-22 coordinated adaptive tuning pass 2

Code changes covered by this pass:

- capacity confidence is now an explicit Python scheduler fact used for
  diagnostics and coordinator decisions, without secretly changing
  `capacity_aware` path shares
- `coordinated_adaptive` decision diagnostics now include compact signals such
  as `capacity_hints`, `capacity_confident`, `jitter_pressure`,
  `latency_spread`, `reorder_pressure`, `queue_pressure`, and `loss_pressure`
- high jitter can steer the coordinator toward `flowlet_adaptive`
- `flowlet_adaptive` lowers the compiled weight of paths with high jitter
- ordered multipath includes jitter in its Python-compiled reorder budget
- benchmark reports now group one `coordinated_adaptive` baseline row with the
  specialist schedulers for the same profile/cache/MTU/payload, including both
  `% wg-user` and `% coord` comparison columns

Verification:

```text
cargo test
.venv/bin/pytest -q
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high,realworld-fiber-plus-5g,realworld-starlink-plus-5g,realworld-starlink-plus-2x5g \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --out .gatherlink/lab-profile-runs/coordinated-adaptive-tuning-normal-pass2
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --path-mtu 9000 \
  --payload-size 8192 \
  --out .gatherlink/lab-profile-runs/coordinated-adaptive-tuning-jumbo-pass2
```

Results:

- Rust tests passed
- Python tests passed: 471 passed
- normal-MTU profile matrix passed all `coordinated_adaptive` target gates
- jumbo synthetic matrix passed all `coordinated_adaptive` target gates

Full scroll table for pattern spotting. This table is intentionally wide. Each
profile/cache/MTU/payload group shows one `coordinated_adaptive` baseline row,
then each specialist scheduler row. The `% coord` column shows how much
throughput the current row delivered compared with the coordinated policy for
the same group. The `% wg-user` column compares the row against the userspace
WireGuard baseline for the same profile, not kernel WireGuard.

Coordinated adaptive vs userspace WireGuard:

| path mtu | payload | profile | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1200 | 1200 | `acceptance-300-500-700` | `warm` | 300/500/700 | 1550 | 1500 | 1530.1 | 98.7% | 102.0% | yes | yes | 310.0/516.7/703.4 | 0/0/36436 |
| 1452 | 1438 | `acceptance-uneven-high` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `warm` | 800/160/85 | 950 | 930 | 932.5 | 98.2% | 100.3% | yes | yes | 717.6/138.7/76.2 | 18470/12804/1247 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `warm` | 180/120/15 | 250 | 245 | 230.4 | 92.1% | 94.0% | yes | yes | 124.6/94.1/11.7 | 36730/1207/235 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `warm` | 180/140/90 | 350 | 340 | 315.4 | 90.1% | 92.8% | yes | yes | 124.7/115.1/75.6 | 58191/7423/1307 |
| 9000 | 8192 | `acceptance-300-500-700` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | yes | yes | 310.0/516.6/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.7 | 98.5% | 102.1% | yes | yes | 621.4/919.7/1316.6 | 0/1894/4554 |

Normal-MTU full matrix:

| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1200 | 1200 | `acceptance-300-500-700` | `coordinated_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1530.1 | 98.7% | 102.0% | 100.0% | yes | yes | 310.0/516.7/703.4 | 0/0/36436 |
| 1200 | 1200 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1511.5 | 97.5% | 100.8% | 98.8% | yes | yes | 310.0/512.6/688.9 | 0/7711/63348 |
| 1200 | 1200 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 300/500/700 | 1550 | 1500 | 1511.7 | 97.5% | 100.8% | 98.8% | yes | yes | 310.0/512.7/688.9 | 0/7506/63028 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 101.3% | yes | yes | 340.7/510.3/699.0 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1490.2 | 96.1% | 99.3% | 97.4% | yes | yes | 345.3/521.4/626.5 | 13018/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `flowlet_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1519.8 | 98.1% | 101.3% | 99.3% | yes | yes | 328.8/504.5/686.4 | 0/23192/33264 |
| 1452 | 1438 | `acceptance-uneven-high` | `coordinated_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 600/900/1300 | 2900 | 2800 | 2768.4 | 95.5% | 98.9% | 95.5% | yes | yes | 558.3/884.0/1326.0 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2822.4 | 97.3% | 100.8% | 97.3% | yes | yes | 569.3/901.4/1352.1 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `flowlet_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 1029.5 | 35.5% | 36.8% | 35.5% | no | no | 90.7/306.6/632.2 | 300290/608903/716742 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `coordinated_adaptive` | `warm` | 800/160/85 | 950 | 930 | 932.5 | 98.2% | 100.3% | 100.0% | yes | yes | 717.6/138.7/76.2 | 18470/12804/1247 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 934.1 | 98.3% | 100.4% | 100.2% | yes | yes | 718.9/139.0/76.3 | 15982/12146/1206 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `latency_guarded_capacity` | `warm` | 800/160/85 | 950 | 930 | 896.1 | 94.3% | 96.4% | 96.1% | yes | yes | 686.4/139.2/76.2 | 14899/11795/1230 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath` | `warm` | 800/160/85 | 950 | 930 | 588.6 | 62.0% | 63.3% | 63.1% | no | no | 356.1/157.8/74.7 | 17015/294993/415720 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 590.8 | 62.2% | 63.5% | 63.4% | no | no | 354.9/160.3/75.6 | 17308/291175/414673 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `flowlet_adaptive` | `warm` | 800/160/85 | 950 | 930 | 878.4 | 92.5% | 94.5% | 94.2% | yes | yes | 669.0/139.0/76.3 | 17400/11576/1105 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `coordinated_adaptive` | `warm` | 180/120/15 | 250 | 245 | 230.4 | 92.1% | 94.0% | 100.0% | yes | yes | 124.6/94.1/11.7 | 36730/1207/235 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 230.5 | 92.2% | 94.1% | 99.9% | yes | yes | 124.7/94.1/11.7 | 36392/1183/237 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `latency_guarded_capacity` | `warm` | 180/120/15 | 250 | 245 | 230.5 | 92.2% | 94.1% | 99.9% | yes | yes | 124.8/94.1/11.7 | 36323/1214/251 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath` | `warm` | 180/120/15 | 250 | 245 | 187.9 | 75.2% | 76.7% | 81.6% | yes | no | 90.2/85.6/12.2 | 3127/1081/121803 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 186.6 | 74.6% | 76.2% | 81.0% | no | no | 89.4/85.2/12.0 | 2970/1021/124636 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `flowlet_adaptive` | `warm` | 180/120/15 | 250 | 245 | 229.6 | 91.9% | 93.7% | 99.7% | yes | yes | 124.5/93.6/11.6 | 34332/1238/3939 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `coordinated_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.4 | 90.1% | 92.8% | 100.0% | yes | yes | 124.7/115.1/75.6 | 58191/7423/1307 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 315.1 | 90.0% | 92.7% | 99.9% | yes | yes | 124.1/115.4/75.6 | 59480/6852/1271 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `latency_guarded_capacity` | `warm` | 180/140/90 | 350 | 340 | 315.5 | 90.1% | 92.8% | 100.0% | yes | yes | 124.4/115.4/75.6 | 58659/6800/1240 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath` | `warm` | 180/140/90 | 350 | 340 | 297.7 | 85.1% | 87.6% | 94.4% | yes | no | 122.2/107.8/67.8 | 66459/24366/12377 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 297.9 | 85.1% | 87.6% | 94.5% | yes | no | 122.0/107.8/68.1 | 66635/23709/12516 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `flowlet_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.5 | 90.2% | 92.8% | 100.0% | yes | yes | 124.4/115.3/75.9 | 57700/7611/1292 |

Jumbo full matrix:

| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 9000 | 8192 | `acceptance-300-500-700` | `coordinated_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.6/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 300/500/700 | 1550 | 1500 | 1434.7 | 92.6% | 95.6% | 92.6% | yes | yes | 379.9/518.0/536.8 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1434.9 | 92.6% | 95.7% | 92.6% | yes | yes | 379.7/518.0/537.2 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `flowlet_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1474.2 | 95.1% | 98.3% | 95.1% | yes | yes | 0.0/587.2/887.0 | 0/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `coordinated_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.7 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.7/1316.6 | 0/1894/4554 |
| 9000 | 8192 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.7 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.7/1316.6 | 0/1892/4554 |
| 9000 | 8192 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.8 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.7/1316.6 | 0/1893/4551 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 600/900/1300 | 2900 | 2800 | 2566.2 | 88.5% | 91.7% | 89.8% | yes | no | 622.1/919.7/1024.4 | 45119/5809/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2557.6 | 88.2% | 91.3% | 89.5% | yes | no | 622.2/919.8/1015.6 | 46150/6094/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `flowlet_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 1525.8 | 52.6% | 54.5% | 53.4% | no | no | 169.1/426.1/930.6 | 41600/69358/98722 |

Reading:

- `coordinated_adaptive` is now product-shaped as a default auto policy: it
  hit target in every tested profile and protects the WAN facsimiles from
  ordered-mode collapse.
- The specialist rows still matter. `ordered_multipath` remains excellent on
  clean normal-MTU 300/500/700, but it is still the wrong choice for the
  jittery/lossy real-world profiles.
- The grouped report layout is the right way to read this data: read each
  `coordinated_adaptive` baseline once, then compare specialists through
  `% coord` and `% wg-user`.
- Next tuning should focus on making ordered mode recover more of the clean
  jumbo case without harming the real-world coordinator decisions.

## 2026-05-22 scheduler safety tuning pass 3

Code changes covered by this pass:

- `ordered_multipath_capacity_aware` now reduces both weight and in-flight
  credit when Python sees path drops, send failures, reorder pressure, queue
  pressure, loss, or jitter. This keeps the mode useful for clean ordered
  traffic without pretending it is safe for every WAN facsimile.
- `flowlet_adaptive` now uses shorter default flowlet windows
  (`25ms` idle, `100ms` max hold) and a small hot-burst path-run bound of `4`
  datagrams, while explicit per-service config still wins.
- `coordinated_adaptive` now prefers flowlet behavior before ordered behavior
  when jitter is present, and keeps latency-guarded capacity ahead of plain
  capacity when latency spread and pressure appear together.

Verification:

```text
.venv/bin/pytest -q tests/python/test_scheduler_policies.py tests/python/test_config_expansion.py tests/python/test_runtime_reload.py
.venv/bin/ruff check python/gatherlink/scheduling/policies.py python/gatherlink/scheduling/coordinator.py python/gatherlink/config/expansion.py tests/python/test_scheduler_policies.py tests/python/test_config_expansion.py
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,realworld-fiber-plus-5g,realworld-starlink-plus-2x5g \
  --schedulers ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 5 \
  --out .gatherlink/lab-profile-runs/scheduler-tuning-pass3-smoke
```

Smoke results:

| profile | scheduler | sink rx | % wg-user | target | path rx a/b/c | drops a/b/c | reading |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| `acceptance-300-500-700` | `coordinated_adaptive` | 1550.0 | 103.3% | yes | 310.0/516.7/723.3 | 0/0/0 | Auto policy reaches the clean capacity target. |
| `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | 1550.0 | 103.3% | yes | 301.2/507.3/741.5 | 0/0/0 | Ordered capacity-aware remains strong on clean links. |
| `acceptance-300-500-700` | `flowlet_adaptive` | 1550.0 | 103.3% | yes | 310.0/516.7/723.3 | 0/0/0 | Shorter flowlets do not harm the clean synthetic profile. |
| `realworld-fiber-plus-5g` | `coordinated_adaptive` | 933.8 | 100.4% | yes | 718.3/138.9/76.6 | 8505/6153/375 | Coordinator avoids ordered collapse and tracks the safe flowlet result. |
| `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | 606.4 | 65.2% | no | 369.1/160.6/76.8 | 10086/135640/199914 | Ordered mode is still intentionally not selected for this jitter/loss shape. |
| `realworld-fiber-plus-5g` | `flowlet_adaptive` | 933.2 | 100.3% | yes | 717.8/138.9/76.4 | 9418/5845/424 | Flowlet is the useful specialist here. |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive` | 315.8 | 92.9% | yes | 124.6/115.6/75.7 | 29309/3302/599 | Coordinator stays above target with less slow-path pain than ordered. |
| `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | 296.4 | 87.2% | no | 120.6/108.3/67.5 | 31402/14378/7292 | Ordered remains a clean-link or explicitly selected tool, not the WAN default. |
| `realworld-starlink-plus-2x5g` | `flowlet_adaptive` | 315.7 | 92.9% | yes | 124.6/115.3/75.8 | 28658/3877/653 | Flowlet tracks coordinated adaptive closely. |

Reading:

- The useful gain is mostly in policy safety: `coordinated_adaptive` now has a
  clearer reason to stay with flowlet/capacity behavior in real-world profiles.
- `ordered_multipath_capacity_aware` still deserves future work, but the next
  improvements should focus on making it self-throttle earlier under pressure,
  not on making the coordinator choose it more often.

## 2026-05-22 scheduler safety tuning pass 4

Command:

```text
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high,realworld-fiber-plus-5g,realworld-starlink-plus-5g,realworld-starlink-plus-2x5g \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --out .gatherlink/lab-profile-runs/scheduler-tuning-pass4-normal
```

Full warm normal-MTU matrix:

| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1200 | 1200 | `acceptance-300-500-700` | `coordinated_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1516.2 | 97.8% | 101.1% | 100.0% | yes | yes | 310.0/514.7/691.5 | 0/3713/57691 |
| 1200 | 1200 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1513.9 | 97.7% | 100.9% | 99.8% | yes | yes | 310.0/513.6/690.3 | 0/5935/60783 |
| 1200 | 1200 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 300/500/700 | 1550 | 1500 | 1512.6 | 97.6% | 100.8% | 99.8% | yes | yes | 310.0/513.0/689.6 | 0/6978/62233 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1533.8 | 99.0% | 102.3% | 101.2% | yes | yes | 344.8/512.1/676.9 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `flowlet_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1520.8 | 98.1% | 101.4% | 100.3% | yes | yes | 329.1/505.2/686.5 | 0/21981/32609 |
| 1452 | 1438 | `acceptance-uneven-high` | `coordinated_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2821.3 | 97.3% | 100.8% | 97.3% | yes | yes | 569.0/900.9/1351.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `flowlet_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 1133.4 | 39.1% | 40.5% | 39.1% | no | no | 170.2/305.1/658.1 | 411170/476665/647780 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `coordinated_adaptive` | `warm` | 800/160/85 | 950 | 930 | 933.8 | 98.3% | 100.4% | 100.0% | yes | yes | 718.5/139.1/76.2 | 16664/12037/1182 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 933.4 | 98.2% | 100.4% | 100.0% | yes | yes | 718.1/139.0/76.3 | 17412/12191/1172 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `latency_guarded_capacity` | `warm` | 800/160/85 | 950 | 930 | 879.6 | 92.6% | 94.6% | 94.2% | yes | yes | 669.0/138.8/76.2 | 19750/12505/1239 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 592.1 | 62.3% | 63.7% | 63.4% | no | no | 357.8/159.3/75.0 | 17383/288935/413939 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `flowlet_adaptive` | `warm` | 800/160/85 | 950 | 930 | 933.8 | 98.3% | 100.4% | 100.0% | yes | yes | 718.6/139.0/76.3 | 17115/11545/1139 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `coordinated_adaptive` | `warm` | 180/120/15 | 250 | 245 | 230.2 | 92.1% | 94.0% | 100.0% | yes | yes | 124.4/94.1/11.6 | 36987/1201/266 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 230.4 | 92.2% | 94.0% | 100.1% | yes | yes | 124.6/94.1/11.7 | 36678/1207/259 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `latency_guarded_capacity` | `warm` | 180/120/15 | 250 | 245 | 230.6 | 92.2% | 94.1% | 100.2% | yes | yes | 124.8/94.1/11.7 | 36305/1167/241 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 185.2 | 74.1% | 75.6% | 80.5% | no | no | 97.2/79.8/8.2 | 47563/6599/77651 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `flowlet_adaptive` | `warm` | 180/120/15 | 250 | 245 | 230.1 | 92.0% | 93.9% | 100.0% | yes | yes | 124.5/94.1/11.6 | 35438/1145/2100 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `coordinated_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.3 | 90.1% | 92.7% | 100.0% | yes | yes | 124.6/115.1/75.6 | 58396/7449/1301 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 315.5 | 90.1% | 92.8% | 100.1% | yes | yes | 124.5/115.3/75.6 | 58568/7021/1277 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `latency_guarded_capacity` | `warm` | 180/140/90 | 350 | 340 | 315.4 | 90.1% | 92.8% | 100.0% | yes | yes | 124.3/115.4/75.6 | 58913/6770/1278 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 295.3 | 84.4% | 86.9% | 93.7% | yes | no | 121.0/106.3/68.0 | 65362/28123/14728 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `flowlet_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.6 | 90.2% | 92.8% | 100.1% | yes | yes | 124.3/115.4/75.8 | 57820/7305/1327 |

Reading:

- `coordinated_adaptive` is still the best default shape: it hits target in all
  five warm profiles and either matches or sits within noise of the best safe
  specialist.
- `ordered_multipath_capacity_aware` is now clearly useful for clean normal-MTU
  capacity profiles. It beat the coordinator on `acceptance-300-500-700` and
  remained healthy on `acceptance-uneven-high`.
- Ordered mode is still not a WAN/jitter default. The coordinator should keep
  avoiding it for fiber-plus-5G and Starlink facsimiles until the receiver
  feedback loop can prove lower pressure before switching.
- `flowlet_adaptive` is the right jitter specialist but not a clean high-rate
  capacity splitter. That is acceptable for now because the coordinated policy
  selects capacity-aware behavior in clean high-rate profiles.

## 2026-05-22 scheduler safety tuning pass 5

Code changes covered by this pass:

- `ordered_multipath_capacity_aware` now compiles a larger reorder budget only
  for large-MTU paths. This recovered clean jumbo capacity profiles without
  forcing the same relaxed hold onto normal-MTU traffic.
- `coordinated_adaptive` now requires both jitter pressure and latency spread
  before selecting `flowlet_adaptive`. Brief jitter on otherwise clean links is
  treated as noise and remains on the known-good capacity fallback.

Verification:

```text
.venv/bin/pytest -q tests/python/test_scheduler_policies.py tests/python/test_config_expansion.py
.venv/bin/ruff check python/gatherlink/scheduling/policies.py python/gatherlink/scheduling/coordinator.py tests/python/test_scheduler_policies.py
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high,realworld-fiber-plus-5g,realworld-starlink-plus-5g,realworld-starlink-plus-2x5g \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --out .gatherlink/lab-profile-runs/scheduler-tuning-pass5-normal
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700,acceptance-uneven-high \
  --schedulers capacity_aware,latency_guarded_capacity,ordered_multipath,ordered_multipath_capacity_aware,flowlet_adaptive,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --path-mtu 9000 \
  --payload-size 8192 \
  --out .gatherlink/lab-profile-runs/scheduler-tuning-pass5-jumbo
```

Normal-MTU matrix:

| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1200 | 1200 | `acceptance-300-500-700` | `coordinated_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1533.0 | 98.9% | 102.2% | 98.9% | yes | yes | 310.0/516.7/706.3 | 0/0/31425 |
| 1200 | 1200 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 314.1/508.3/727.7 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1264.5 | 81.6% | 84.3% | 81.6% | yes | no | 301.2/480.2/484.5 | 0/0/0 |
| 1200 | 1200 | `acceptance-300-500-700` | `flowlet_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1521.7 | 98.2% | 101.4% | 98.2% | yes | yes | 329.4/505.9/686.4 | 0/22162/30833 |
| 1452 | 1438 | `acceptance-uneven-high` | `coordinated_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2900.0 | 100.0% | 103.6% | 100.0% | yes | yes | 621.4/932.1/1346.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2652.9 | 91.5% | 94.7% | 91.5% | yes | yes | 568.5/852.7/1231.7 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 600/900/1300 | 2900 | 2800 | 2632.8 | 90.8% | 94.0% | 90.8% | yes | yes | 564.2/846.3/1222.4 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 600/900/1300 | 2900 | 2800 | 2859.4 | 98.6% | 102.1% | 98.6% | yes | yes | 576.7/913.1/1369.7 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2832.8 | 97.7% | 101.2% | 97.7% | yes | yes | 571.5/904.8/1357.3 | 0/0/0 |
| 1452 | 1438 | `acceptance-uneven-high` | `flowlet_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 1134.2 | 39.1% | 40.5% | 39.1% | no | no | 161.9/326.2/646.1 | 389999/505674/639236 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `coordinated_adaptive` | `warm` | 800/160/85 | 950 | 930 | 933.6 | 98.3% | 100.4% | 100.0% | yes | yes | 718.3/138.9/76.3 | 16993/12229/1094 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 906.7 | 95.4% | 97.5% | 97.1% | yes | yes | 698.4/137.2/76.2 | 32826/15815/1236 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `latency_guarded_capacity` | `warm` | 800/160/85 | 950 | 930 | 901.6 | 94.9% | 96.9% | 96.6% | yes | yes | 691.2/136.9/76.2 | 35218/16453/1260 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath` | `warm` | 800/160/85 | 950 | 930 | 589.7 | 62.1% | 63.4% | 63.2% | no | no | 355.8/158.7/75.1 | 17808/290151/417801 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 800/160/85 | 950 | 930 | 589.6 | 62.1% | 63.4% | 63.2% | no | no | 357.4/157.7/74.6 | 18080/291691/415921 |
| 1200 | 1200 | `realworld-fiber-plus-5g` | `flowlet_adaptive` | `warm` | 800/160/85 | 950 | 930 | 906.0 | 95.4% | 97.4% | 97.0% | yes | yes | 696.0/138.9/76.2 | 17286/11826/1214 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `coordinated_adaptive` | `warm` | 180/120/15 | 250 | 245 | 230.2 | 92.1% | 93.9% | 100.0% | yes | yes | 124.5/94.0/11.7 | 37032/1253/258 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 229.5 | 91.8% | 93.7% | 99.7% | yes | yes | 123.7/94.1/11.7 | 38394/1178/232 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `latency_guarded_capacity` | `warm` | 180/120/15 | 250 | 245 | 229.3 | 91.7% | 93.6% | 99.6% | yes | yes | 123.6/94.1/11.7 | 38616/1207/256 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath` | `warm` | 180/120/15 | 250 | 245 | 185.2 | 74.1% | 75.6% | 80.5% | no | no | 97.1/79.8/8.3 | 47646/6649/77586 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `ordered_multipath_capacity_aware` | `warm` | 180/120/15 | 250 | 245 | 185.2 | 74.1% | 75.6% | 80.5% | no | no | 96.8/80.2/8.2 | 47849/6660/77212 |
| 1200 | 1200 | `realworld-starlink-plus-5g` | `flowlet_adaptive` | `warm` | 180/120/15 | 250 | 245 | 230.6 | 92.2% | 94.1% | 100.2% | yes | yes | 124.6/94.4/11.6 | 36124/1237/268 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `coordinated_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.4 | 90.1% | 92.8% | 100.0% | yes | yes | 124.4/115.3/75.6 | 58759/6952/1241 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 315.1 | 90.0% | 92.7% | 99.9% | yes | yes | 125.0/114.5/75.6 | 57622/8622/1296 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `latency_guarded_capacity` | `warm` | 180/140/90 | 350 | 340 | 315.4 | 90.1% | 92.8% | 100.0% | yes | yes | 124.3/115.5/75.6 | 59028/6763/1309 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath` | `warm` | 180/140/90 | 350 | 340 | 293.3 | 83.8% | 86.3% | 93.0% | yes | no | 120.8/106.1/66.5 | 69090/28686/14638 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `ordered_multipath_capacity_aware` | `warm` | 180/140/90 | 350 | 340 | 298.3 | 85.2% | 87.7% | 94.6% | yes | no | 120.5/106.0/71.8 | 56590/29571/15778 |
| 1200 | 1200 | `realworld-starlink-plus-2x5g` | `flowlet_adaptive` | `warm` | 180/140/90 | 350 | 340 | 315.6 | 90.2% | 92.8% | 100.0% | yes | yes | 124.5/115.2/75.8 | 57369/7877/1325 |

Jumbo synthetic matrix:

| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |
| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 9000 | 8192 | `acceptance-300-500-700` | `coordinated_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.6/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `latency_guarded_capacity` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 310.0/516.7/723.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath` | `warm` | 300/500/700 | 1550 | 1500 | 1434.4 | 92.5% | 95.6% | 92.5% | yes | yes | 380.3/517.8/536.3 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `ordered_multipath_capacity_aware` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 359.7/528.5/661.7 | 0/0/0 |
| 9000 | 8192 | `acceptance-300-500-700` | `flowlet_adaptive` | `warm` | 300/500/700 | 1550 | 1500 | 1550.0 | 100.0% | 103.3% | 100.0% | yes | yes | 331.6/495.4/723.0 | 0/0/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `coordinated_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.8 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.8/1316.6 | 0/1888/4550 |
| 9000 | 8192 | `acceptance-uneven-high` | `capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.7 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.7/1316.6 | 0/1896/4556 |
| 9000 | 8192 | `acceptance-uneven-high` | `latency_guarded_capacity` | `warm` | 600/900/1300 | 2900 | 2800 | 2857.7 | 98.5% | 102.1% | 100.0% | yes | yes | 621.4/919.7/1316.6 | 0/1895/4556 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath` | `warm` | 600/900/1300 | 2900 | 2800 | 2565.3 | 88.5% | 91.6% | 89.8% | yes | no | 622.1/919.7/1023.5 | 44608/6463/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `ordered_multipath_capacity_aware` | `warm` | 600/900/1300 | 2900 | 2800 | 2828.7 | 97.5% | 101.0% | 99.0% | yes | yes | 621.9/919.7/1287.1 | 5315/5566/0 |
| 9000 | 8192 | `acceptance-uneven-high` | `flowlet_adaptive` | `warm` | 600/900/1300 | 2900 | 2800 | 2648.1 | 91.3% | 94.6% | 92.7% | yes | yes | 552.3/844.0/1251.9 | 6945/9859/21630 |

Follow-up rerun:

```text
.venv/bin/python tools/run_three_path_profile_bench.py \
  --profiles acceptance-300-500-700 \
  --schedulers ordered_multipath_capacity_aware,coordinated_adaptive \
  --cache-modes warm \
  --duration 10 \
  --out .gatherlink/lab-profile-runs/scheduler-tuning-pass5-rerun-300
```

The rerun put `ordered_multipath_capacity_aware` back at `1538.3 Mbit/s`
(`99.2%` delivery, `102.6%` of userspace WireGuard), so the lower full-matrix
row above is treated as lab variance rather than a known policy regression.

Reading:

- Large-MTU ordered-capacity behavior is materially better. In the jumbo matrix
  it now reaches target on both synthetic profiles; previously the high uneven
  profile was the clearest ordered-capacity miss.
- The coordinator guardrail is correct: clean high-rate cases stay on
  capacity-like behavior, while real-world profiles still hit target through
  coordinated adaptive.
- `flowlet_adaptive` remains a specialist for real-world jitter and should not
  be tuned to win synthetic high-rate capacity profiles. The coordinator is the
  product policy that keeps this specialization safe.

## 2026-05-23 WireGuard-over-Gatherlink shaped-fiber tuning pass

Goal: make sure raw Gatherlink had not regressed, then tune the weaker
WireGuard-over-Gatherlink real-world profile without removing the newer
scheduler/control features.

Raw guardrail reruns:

| Shape | Scheduler / knobs | Offered | Delivered | Packet delta | Evidence |
| --- | --- | ---: | ---: | ---: | --- |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1000 | 1000.04 | 0 | `.gatherlink/hyperv-performance/20260523-raw-clean-1000-coordinated-rerun/` |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1500 | 1397.86 | 0 | `.gatherlink/hyperv-performance/20260523-raw-clean-1500-coordinated-rerun/` |
| `realworld-starlink-plus-5g` | `capacity_aware`, no flowlet | 250 | 246.82 | 1481 | `.gatherlink/hyperv-performance/20260523-raw-realworld-starlink-plus-5g-250-capacity-rerun/` |
| `realworld-starlink-plus-2x5g` | `capacity_aware`, no flowlet | 350 | 281.34 | 81580 | `.gatherlink/hyperv-performance/20260523-raw-realworld-starlink-plus-2x5g-350-capacity/` |
| `realworld-fiber-plus-5g` | `capacity_aware`, no flowlet | 850 | 731.79 | 124680 | `.gatherlink/hyperv-performance/20260523-raw-realworld-fiber-plus-5g-850-capacity/` |
| `realworld-fiber-plus-5g` | `capacity_aware`, path-run 32, conservative caps | 900 | 851.32 | 30692 | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-pathrun32-conservative-rerun/` |

WireGuard-over-Gatherlink tuning attempts on `realworld-fiber-plus-5g`:

| Scheduler / knobs | TCP | UDP | Reading | Evidence |
| --- | ---: | ---: | --- | --- |
| `flowlet_adaptive` defaults | 690.89 | 655.92 | Best specialist row; pins the nested WireGuard tunnel to the best path and approaches direct WireGuard path-a. | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-no-flowlet-sweep/` |
| `coordinated_adaptive`, 50ms idle, 250ms max hold | 692.49 | 636.80 | Best coordinator TCP row. Useful default candidate for WG services because it keeps coordinator policy range while matching the flowlet TCP result. | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-coordinated-hold250ms-rerun/` |
| `coordinated_adaptive`, 50ms idle, 1s max hold | 687.74 | 645.24 | Similar to 250ms; slightly better UDP and slightly lower TCP. | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-coordinated-hold1s-rerun/` |
| `flowlet_adaptive`, paths a+b only | 691.59 | 604.35 | Dropping path-c did not beat the three-path flowlet result. | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-ab-sweep/` |
| `coordinated_adaptive`, path-run 32, conservative caps | 283.38 | 168.06 | Rejected: raw delivery improved, nested WG got much worse. | `.gatherlink/hyperv-performance/20260523-wg-realworld-fiber-plus-5g-pathrun32-conservative-rerun/` |

Direct WireGuard comparison for the same shaped fiber profile:

| Mode | Scope | TCP | UDP | Evidence |
| --- | --- | ---: | ---: | --- |
| userspace | path-a | 415.78 | 661.82 | `.gatherlink/hyperv-performance/20260523-direct-wg-userspace-realworld-fiber-plus-5g/` |
| userspace | simultaneous a+b+c UDP sum | n/a | 951.44 | `.gatherlink/hyperv-performance/20260523-direct-wg-userspace-realworld-fiber-plus-5g/` |
| kernel | simultaneous a+b+c UDP sum | n/a | 926.21 | `.gatherlink/hyperv-performance/20260523-direct-wg-kernel-realworld-fiber-plus-5g/` |

Reading:

- Raw Gatherlink did not regress on clean links. The fresh 1.5 Gbit/s clean run
  improved over the previous current-log guardrail while still reporting zero
  application packet delta.
- Shaped raw fiber and Starlink+2x5G remain sensitive to Hyper-V shaping. A
  path-run burst can improve raw delivery there, but it is currently harmful
  for nested WireGuard and should not be generalized as a WG tuning.
- For one nested WireGuard tunnel, flowlet/coordinator hold policies now reach
  the direct-WireGuard best-path ceiling. They do not yet reach the sum of
  simultaneous direct-WireGuard paths, which is the next frontier if aggregate
  WG-over-GL is still the desired target.

## 2026-05-23 Raw regression and real-world WireGuard hold sweep

Goal: run a broader raw Gatherlink guardrail set after the WireGuard tuning
work, then check whether shorter bounded holds could make one WireGuard policy
good for both TCP-like and UDP-like real-world traffic.

Raw regression matrix:

| Shape | Scheduler / knobs | Offered | Delivered | Packet delta | Evidence |
| --- | --- | ---: | ---: | ---: | --- |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1000 | 999.88 | 0 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/clean-1000-coord/` |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1500 | 1421.45 | 0 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/clean-1500-coord/` |
| `realworld-fiber-plus-5g` | `capacity_aware`, no flowlet | 850 | 730.51 | 88713 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/fiber-850-cap/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, no flowlet | 850 | 735.66 | 86801 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/fiber-850-coord/` |
| `realworld-fiber-plus-5g` | `capacity_aware`, path-run 32, conservative caps | 900 | 856.03 | 29805 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/fiber-900-cap-run32/` |
| `realworld-starlink-plus-5g` | `capacity_aware`, no flowlet | 250 | 243.02 | 982 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/star5g-250-cap/` |
| `realworld-starlink-plus-2x5g` | `capacity_aware`, no flowlet | 350 | 279.98 | 55332 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/star2x5g-350-cap/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no flowlet | 350 | 278.16 | 56354 | `.gatherlink/hyperv-performance/20260523-raw-regression-matrix/star2x5g-350-coord/` |

WireGuard-over-Gatherlink real-world hold sweep:

| Shape | Scheduler / knobs | TCP | UDP | Reading | Evidence |
| --- | --- | ---: | ---: | --- | --- |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 691.34 | 653.37 | Repeat confirms the bounded coordinator is close to the flowlet specialist. | `.gatherlink/hyperv-performance/20260523-wg-realworld-tuning-matrix/fiber-hold250/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 1s max hold | 674.13 | 558.97 | Worse in this rerun; do not prefer 1s hold for shaped fiber. | `.gatherlink/hyperv-performance/20260523-wg-realworld-tuning-matrix/fiber-hold1000/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 63.69 | 158.26 | Best Starlink+5G TCP row in this pass; UDP remains single-path-like. | `.gatherlink/hyperv-performance/20260523-wg-hold-sweep/star5g-hold150000/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 100ms max hold | 50.60 | 148.83 | Lower TCP and lower UDP than 150ms. | `.gatherlink/hyperv-performance/20260523-wg-hold-sweep/star5g-hold100000/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no hold | 42.90 | 273.10 | Best UDP aggregation in the rerun, but TCP remains poor. | `.gatherlink/hyperv-performance/20260523-wg-realworld-tuning-matrix/star2x5g-nohold/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, 50ms idle, 100ms max hold | 64.45 | 146.87 | Best TCP row in the short-hold pass, but it gives up UDP aggregation. | `.gatherlink/hyperv-performance/20260523-wg-hold-sweep/star2x5g-hold100000/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 60.77 | 157.68 | Slightly more UDP than 100ms, slightly less TCP. | `.gatherlink/hyperv-performance/20260523-wg-hold-sweep/star2x5g-hold150000/` |

Reading:

- There was no clean-link raw regression. The latest 1.5 Gbit/s run improved to
  1421.45 Mbit/s with no application packet delta.
- Raw Starlink+5G remains target-ish. Raw fiber and Starlink+2x5G are still
  dominated by shaped-path loss in the Hyper-V lab, not by a clean-link
  dataplane ceiling.
- For WireGuard-over-Gatherlink real-world shapes, there is no single hold
  value that wins both TCP-like and UDP-like traffic. This should remain an
  explicit policy choice: bounded hold for stable tunnel/TCP behavior, no hold
  for UDP aggregation.

## 2026-05-23 Extended raw guardrails and WireGuard MTU sweep

Goal: broaden raw Gatherlink checks after tuning, then test the most likely
WireGuard-side tuning knob: WireGuard interface MTU.

Extended raw guardrails:

| Shape | Scheduler / knobs | Link MTU | Path MTU | Payload | Offered | Delivered | Packet delta | Evidence |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1500 | 1472 | 1300 | 1000 | 999.98 | 0 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/clean-1000-coord/` |
| clean | `coordinated_adaptive`, 50ms idle, 60s max hold | 1500 | 1472 | 1300 | 1500 | 1331.61 | 0 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/clean-1500-coord/` |
| `acceptance-300-500-700` | `capacity_aware`, no flowlet | 1500 | 1200 | 1200 | 1550 | 664.81 | 473874 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/accept-1550-cap/` |
| `acceptance-300-500-700` | `capacity_aware`, no flowlet | 9000 | 9000 | 8192 | 1550 | 1433.34 | 0 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/jumbo-accept-1550-cap/` |
| `acceptance-uneven-high` | `coordinated_adaptive`, no flowlet | 9000 | 9000 | 8192 | 2900 | 1676.20 | 144511 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/jumbo-uneven-2900-coord/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, no flowlet | 1500 | 1472 | 1200 | 850 | 733.71 | 82790 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/fiber-850-coord/` |
| `realworld-fiber-plus-5g` | `capacity_aware`, path-run 32 | 1500 | 1472 | 1200 | 900 | 849.86 | 35114 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/fiber-900-cap-run32/` |
| `realworld-starlink-plus-5g` | `capacity_aware`, no flowlet | 1500 | 1472 | 1200 | 250 | 247.24 | 927 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/star5g-250-cap/` |
| `realworld-starlink-plus-2x5g` | `capacity_aware`, no flowlet | 1500 | 1472 | 1200 | 350 | 280.19 | 53314 | `.gatherlink/hyperv-performance/20260523-raw-extended-guardrails/star2x5g-350-cap/` |

MTU sweep highlights:

| Shape | Scheduler / knobs | WG MTU | TCP | UDP | Reading | Evidence |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1200 | 617.53 | 272.37 | Too small for this profile; UDP collapsed. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/fiber-coord-hold250-mtu1200/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1280 | 666.59 | 630.12 | Best UDP row in this sweep. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/fiber-coord-hold250-mtu1280/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1360 | 679.30 | 587.16 | Best TCP row in this sweep. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/fiber-coord-hold250-mtu1360/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1420 | 74.46 | 589.49 | Rejected: high MTU hurt TCP badly. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/fiber-coord-hold250-mtu1420/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 1380 | 68.12 | 157.73 | Best TCP row in this sweep. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/star5g-coord-hold150-mtu1380/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 1420 | 33.16 | 157.01 | Rejected: high MTU hurt TCP again. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/star5g-coord-hold150-mtu1420/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no hold | 1200 | 37.35 | 285.91 | Best UDP aggregation row in this sweep. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/star2x5g-nohold-mtu1200/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no hold | 1360 | 41.24 | 274.76 | Better TCP than 1200, lower UDP. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/star2x5g-nohold-mtu1360/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no hold | 1420 | 25.79 | 271.89 | Rejected for TCP. | `.gatherlink/hyperv-performance/20260523-wg-mtu-sweep/star2x5g-nohold-mtu1420/` |

Reading:

- WireGuard interface MTU is a real Gatherlink helper tuning lever. The right
  value is profile-sensitive; it should be operator-tunable or helper-planned,
  not hardcoded globally.
- MTU 1420 is a bad normal-MTU Gatherlink-over-Hyper-V choice for TCP-like
  WireGuard traffic in these runs. Keep 1380 as a general starting point and
  test 1280/1200 when the profile is lossy or UDP-first.
- Persistent keepalive was already present in the Hyper-V WireGuard setup. It
  is useful for liveness/NAT behavior, but it is not the throughput knob exposed
  by this pass.
- The normal-MTU synthetic shaped rows are packet-rate/host-shaping sensitive.
  Jumbo rows show that larger packets reduce pressure, but jumbo is not a
  realistic default for WAN-facing examples.

## 2026-05-23 WireGuard MTU confirmation pass

Goal: rerun the best-looking MTU/hold combinations from the exploratory MTU
sweep with 10-second runs before turning them into guidance.

| Shape | Scheduler / knobs | WG MTU | TCP | UDP | Reading | Evidence |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1280 | 674.10 | 629.36 | Good UDP-first fiber value, but not the best TCP value. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/fiber-hold250-mtu1280/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1360 | 686.62 | 599.27 | Middle ground; no longer best after confirmation. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/fiber-hold250-mtu1360/` |
| `realworld-fiber-plus-5g` | `coordinated_adaptive`, 50ms idle, 250ms max hold | 1380 | 695.03 | 607.45 | Best fiber TCP row so far; keep 1380 as the general starting point. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/fiber-hold250-mtu1380/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 1280 | 62.59 | 158.72 | Slightly better than 1380 in this 10s run, but close enough to treat both as viable. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/star5g-hold150-mtu1280/` |
| `realworld-starlink-plus-5g` | `coordinated_adaptive`, 50ms idle, 150ms max hold | 1380 | 59.81 | 158.56 | Confirms 1380 remains viable; earlier 68.12 Mbit/s TCP row was run variance. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/star5g-hold150-mtu1380/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, no hold | 1200 | 35.58 | 275.13 | Confirms UDP-first no-hold mode, though below the shorter 285.91 Mbit/s exploratory run. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/star2x5g-nohold-mtu1200/` |
| `realworld-starlink-plus-2x5g` | `coordinated_adaptive`, 50ms idle, 100ms max hold | 1380 | 51.39 | 139.09 | Lower than the earlier short-hold pass; keep this as stability mode rather than speed winner. | `.gatherlink/hyperv-performance/20260523-wg-realworld-confirm/star2x5g-hold100-mtu1380/` |

Reading:

- MTU 1380 remains the safest default. It produced the best confirmed
  fiber+5G TCP row and stayed acceptable on Starlink+5G.
- MTU 1280 is a useful lossy-profile test value. It was close to 1380 on
  Starlink+5G and slightly better for fiber UDP, but it is not a universal
  upgrade.
- MTU 1200 is useful only as a UDP-first uneven-profile test value so far.
- WG-over-GL still reaches the useful direct-WireGuard best-path target for
  TCP-like traffic on at least one scheduler/setting family, but it still does
  not make one nested WireGuard tunnel aggregate to the sum of all paths.

## 2026-05-23 Arrival-guarded scheduler first VM check

Goal: add the Python-owned `arrival_guarded_capacity` scheduler to the VM
comparison set without changing Rust. This policy compiles to the existing
weighted Rust primitive, but Python demotes paths whose predicted arrival falls
outside the receiver reorder budget.

Fiber+5G facsimile, normal MTU, WireGuard MTU 1380, 8-second runs:

| Scheduler | Raw GL sink | Raw packet delta | WG TCP | WG UDP | Reading | Evidence |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `capacity_aware` | 747.33 | 82427 | 688.78 | 690.02 | Best UDP row in this focused pass. | `.gatherlink/hyperv-performance/20260523-arrival-guard-fiber/` |
| `arrival_guarded_capacity` | 747.35 | 82449 | 686.21 | 608.99 | Safe raw behavior, but not better for this WG shape. | `.gatherlink/hyperv-performance/20260523-arrival-guard-fiber/` |
| `coordinated_adaptive` | 747.25 | 82459 | 688.38 | 637.35 | Still the better general default candidate. | `.gatherlink/hyperv-performance/20260523-arrival-guard-fiber/` |

Starlink+5G facsimile, normal MTU, WireGuard MTU 1280, 8-second runs:

| Scheduler | Raw GL sink | Raw packet delta | WG TCP | WG UDP | Reading | Evidence |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `capacity_aware` | 167.39 | 67287 | 52.80 | 158.21 | Baseline for the focused profile. | `.gatherlink/hyperv-performance/20260523-arrival-guard-star5g/` |
| `arrival_guarded_capacity` | 168.00 | 67323 | 53.84 | 158.10 | Slight TCP improvement over capacity-only, with matching UDP. | `.gatherlink/hyperv-performance/20260523-arrival-guard-star5g/` |
| `coordinated_adaptive` | 167.92 | 67328 | 54.54 | 158.34 | Best row in this focused pass. | `.gatherlink/hyperv-performance/20260523-arrival-guard-star5g/` |

Reading:

- The first implementation is safe enough to keep as a coordinator candidate:
  raw Gatherlink output stayed in-family and no Rust hot-path primitive changed.
- It is not yet a manual default. The coordinator still matched or beat it in
  the two real-world WireGuard sweeps.
- Next tuning, if pursued, should improve the arrival prediction and demotion
  thresholds rather than move policy into Rust.

## 2026-05-28 UDP pressure benchmark-tool calibration

Goal: make sure `udp_pressure` is not the benchmark bottleneck before using it
to judge Gatherlink endpoint or WireGuard-over-Gatherlink performance. These
runs compare the same high-rate pressure shape against raw private LAN and
kernel WireGuard. All rows used normal 1500-byte path MTU, eight flows/workers,
send/receive batching at 128, UDP GSO at eight segments, node probes enabled,
and near-maximum payload sizes for the tested layer.

| Shape | Pressure mode | Payload | UDP pressure sink total | Matching `iperf3 -u` simultaneous total | Matching kernel-WG TCP total | Reading | Evidence |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Raw private LAN | feedback, full receive copy | 1472 | 13.88 Gbit/s | 19.07 Gbit/s | n/a | The sender was not the bottleneck; sink CPU/copy work showed up in node probes. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-raw-lan-gso8-aimd-probe/` |
| Raw private LAN | feedback, truncate receive | 1472 | 14.08 Gbit/s | 19.15 Gbit/s | n/a | Truncate helped one path but not the aggregate; feedback remained too conservative for raw high-ceiling probing. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-raw-lan-gso8-aimd-trunc-probe/` |
| Raw private LAN | fixed 9 Gbit/s per path, truncate receive | 1472 | 19.75 Gbit/s | 15.85 Gbit/s | n/a | This proves the generator plus truncate sink can exceed the matching raw UDP baseline; use this shape for generator-ceiling proof. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-raw-lan-gso8-fixed9g-trunc-probe/` |
| Raw private LAN | feedback, relaxed backoff, truncate receive | 1472 | 16.56 Gbit/s | 28.65 Gbit/s | n/a | Still underfilled; receiver feedback should be treated as anti-flood control, not the raw maximum-speed proof. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-raw-lan-gso8-regression-probe-trunc/` |
| Kernel WireGuard | feedback, full receive copy | 1392 | 7.97 Gbit/s | 5.16 Gbit/s | 8.93 Gbit/s | Kernel WireGuard changes the ceiling: pressure exceeded `iperf3 -u` and reached about 89% of kernel-WG TCP. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-aimd-probe/` |
| Kernel WireGuard | feedback, truncate receive | 1392 | 8.10 Gbit/s | 4.87 Gbit/s | 9.17 Gbit/s | Best kernel-WG pressure row in this pass, about 88% of the matching TCP path-set total. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-aimd-trunc-probe/` |
| Kernel WireGuard | fixed 9 Gbit/s per path, truncate receive | 1392 | 6.79 Gbit/s | 5.08 Gbit/s | 8.75 Gbit/s | Fixed target is not the winner inside kernel WireGuard; the tunnel stack benefits from bounded feedback. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed9g-trunc-probe/` |

Reading:

- `udp_pressure` is now capable of exceeding `iperf3 -u` on raw LAN when run
  with explicit high fixed targets and truncate receive, so it is no longer
  obviously too weak to drive Gatherlink tests.
- The receiver-paced feedback controller is useful for avoiding uncontrolled
  flood, but it is not the high-ceiling proof path. For maximum raw LAN proof,
  use fixed targets or unbounded runs with node probes.
- Kernel WireGuard is a better ceiling than userspace WireGuard for this
  calibration pass. With feedback and truncate receive, `udp_pressure` reached
  about 88% of matching kernel-WG TCP while exceeding matching kernel-WG
  `iperf3 -u`.
- Keep node probes enabled while tuning this tool. The useful signal in this
  pass came from seeing sink CPU dominate raw LAN and WireGuard/kernel work
  dominate tunneled runs.

## 2026-05-28 Kernel WireGuard UDP pressure tuning pass

Goal: increase UDP pressure over kernel WireGuard after proving raw LAN was not
the generator ceiling. The test shape used three clean VM paths, normal 1500-byte
path MTU, WireGuard MTU 1420, `--udp-length auto` resolving to 1392 bytes,
eight flows/workers, send/receive batch 128, truncate receive, and node probes.
Percentages compare against the simultaneous kernel-WireGuard TCP total from
the same run unless stated otherwise.

| Shape | GSO | Pressure control | UDP pressure sink total | Matching kernel-WG TCP total | % of kernel-WG TCP | Matching `iperf3 -u` total | Reading | Evidence |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| Kernel WG clean | 4 | feedback, 500ms samples | 7.34 Gbit/s | 9.03 Gbit/s | 81.3% | 5.29 Gbit/s | Too small; syscall/packet pressure is higher. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso4-structured-feedback500-probe/` |
| Kernel WG clean | 6 | feedback, 500ms samples | 7.69 Gbit/s | 8.81 Gbit/s | 87.3% | 4.96 Gbit/s | Better, but still below GSO 8. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso6-structured-feedback500-probe/` |
| Kernel WG clean | 8 | feedback, 500ms samples | 7.93 Gbit/s | 9.03 Gbit/s | 87.8% | 5.09 Gbit/s | Best feedback/GSO row in this sweep. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-structured-feedback500-probe/` |
| Kernel WG clean | 10 | feedback, 500ms samples | 7.37 Gbit/s | 9.11 Gbit/s | 80.9% | 5.16 Gbit/s | Too large; burst shape hurts delivery. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso10-structured-feedback500-probe/` |
| Kernel WG clean | 8 | fixed 2.8 Gbit/s per path | 6.83 Gbit/s | 9.13 Gbit/s | 74.8% | 5.19 Gbit/s | Under target; not enough pressure. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed2800-trunc-probe/` |
| Kernel WG clean | 8 | fixed 3.0 Gbit/s per path | 7.99 Gbit/s | 9.05 Gbit/s | 88.2% | 5.20 Gbit/s | Similar to feedback, useful bounded baseline. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3g-trunc-probe/` |
| Kernel WG clean | 8 | fixed 3.1 Gbit/s per path | 8.24 Gbit/s | 9.00 Gbit/s | 91.6% | 4.78 Gbit/s | First row above 90% of matching kernel-WG TCP. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3100-trunc-probe/` |
| Kernel WG clean | 8 | fixed 3.2 Gbit/s per path | 8.48 Gbit/s | 9.05 Gbit/s | 93.6% | 5.08 Gbit/s | Best row in this pass; current kernel-WG UDP pressure recommendation. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3200-trunc-probe/` |
| Kernel WG clean | 8 | fixed 3.3 Gbit/s per path | 8.18 Gbit/s | 8.92 Gbit/s | 91.6% | 5.25 Gbit/s | Past the local optimum; delivery falls. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3300-trunc-probe/` |

Reading:

- For this clean kernel-WireGuard shape, `GSO=8` is still the best tested
  packetization. Smaller GSO raises packet/syscall pressure; larger GSO becomes
  too bursty.
- Receiver feedback is helpful when avoiding flood, but fixed bounded pressure
  around the measured path ceiling is better for maximum kernel-WG UDP proof.
- The current best UDP-pressure row is `8.48 Gbit/s`, or `93.6%` of the
  matching kernel-WireGuard TCP path-set total. It also exceeds matching
  `iperf3 -u` by a wide margin, so `iperf3 -u` is no longer the right ceiling
  for this generator shape.
- Keep node probes in this matrix. They showed the pressure tool itself was not
  CPU-saturated at the best rows; remaining headroom is mostly in the tunnel and
  kernel delivery behavior.

## 2026-05-28 Kernel WireGuard UDP pressure 30-second confirmation

Goal: check whether the short 8-second kernel-WireGuard UDP pressure optimum
holds over 30-second windows, then try the plausible last-percent knobs:
pressure knee, GSO neighbors, send/receive batch neighbors, and simple CPU
affinity. All rows used three clean VM paths, normal 1500-byte path MTU,
WireGuard MTU 1420, `--udp-length auto` resolving to 1392 bytes, eight
flows/workers, truncate receive, and node probes.

| Shape | GSO | Send batch | Recv batch | CPU set | Pressure control | UDP pressure sink total | Matching kernel-WG TCP total | % of kernel-WG TCP | Matching `iperf3 -u` total | Delivery | Reading | Evidence |
| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Kernel WG clean, 30s | 8 | 128 | 128 | default | fixed 3.20 Gbit/s per path | 7.97 Gbit/s | 8.89 Gbit/s | 89.6% | 5.19 Gbit/s | 83.1% | Longer confirmation is solid, but below the 8-second peak. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3200-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 128 | 128 | default | fixed 3.15 Gbit/s per path | 7.98 Gbit/s | 8.76 Gbit/s | 91.0% | 5.16 Gbit/s | 84.4% | Best 30s balance; slightly lower offered pressure improves delivery percentage. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 128 | 128 | default | fixed 3.25 Gbit/s per path | 7.22 Gbit/s | 8.89 Gbit/s | 81.2% | 5.28 Gbit/s | 74.1% | Past the knee; extra offered traffic becomes loss/queue churn. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3250-trunc-30s/` |
| Kernel WG clean, 30s | 7 | 128 | 128 | default | fixed 3.15 Gbit/s per path | 7.13 Gbit/s | 8.07 Gbit/s | 88.3% | 4.81 Gbit/s | 75.5% | Lower GSO is not a win. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso7-fixed3150-trunc-30s/` |
| Kernel WG clean, 30s | 9 | 128 | 128 | default | fixed 3.15 Gbit/s per path | 7.40 Gbit/s | 8.76 Gbit/s | 84.4% | 5.16 Gbit/s | 78.3% | Higher GSO is also not a win. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso9-fixed3150-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 96 | 128 | default | fixed 3.15 Gbit/s per path | 7.36 Gbit/s | 8.62 Gbit/s | 85.4% | 4.99 Gbit/s | 78.0% | Smaller send batch hurts. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-send96-fixed3150-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 160 | 128 | default | fixed 3.15 Gbit/s per path | 6.77 Gbit/s | 8.76 Gbit/s | 77.3% | 5.12 Gbit/s | 71.6% | Larger send batch hurts more. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-send160-fixed3150-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 128 | 128 | sender/sink 0-2 | fixed 3.15 Gbit/s per path | 6.64 Gbit/s | 8.62 Gbit/s | 77.0% | 5.04 Gbit/s | 70.6% | Simple taskset isolation hurts in the 4-vCPU VM; do not use as default. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-taskset0-2-trunc-30s/` |
| Kernel WG clean, 30s | 8 | 128 | 96 | default | fixed 3.15 Gbit/s per path | 7.00 Gbit/s | 8.74 Gbit/s | 80.1% | 4.91 Gbit/s | 74.1% | Smaller receive batch hurts. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-recv96-fixed3150-trunc-30s/` |

Reading:

- The best 30-second row is `GSO=8`, send/receive batch 128, fixed
  `3.15 Gbit/s` per path. It delivered `7.98 Gbit/s`, or `91.0%` of matching
  kernel-WireGuard TCP and `154.6%` of matching `iperf3 -u`.
- The remaining gap is unlikely to be a simple `udp_pressure` sender/sink knob.
  Probes show no UDP receive-buffer errors and no path interface drops. Userland
  pressure processes are not pegged, while node CPU is high enough to implicate
  kernel WireGuard, softirq, and VM scheduling.
- Do not chase the rejected knobs as defaults: GSO 7/9, send batch 96/160,
  receive batch 96, and taskset `0-2` all reduced delivered throughput.
- The likely remaining wins need a better lab or deeper host/kernel visibility:
  CPU/IRQ/queue placement at the Hyper-V or Linux networking layer, more vCPU
  headroom, or tracing where kernel WireGuard drops/queues packets under this
  pressure.

## 2026-05-28 Kernel WireGuard last-percent lab ceiling pass

Goal: test whether the remaining UDP-pressure gap is meaningfully improved by
host/guest shape changes rather than another sender knob. This pass kept the
same clean three-path kernel-WireGuard shape: normal 1500-byte path MTU,
WireGuard MTU 1420, 1392-byte UDP payloads, `GSO=8`, send/receive batch 128,
fixed `3.15 Gbit/s` offered per path, truncate receive, and 30-second windows.

| VM shape / knob | UDP pressure sink total | Matching kernel-WG TCP total | % of valid kernel-WG TCP | Matching `iperf3 -u` total | Node CPU A/B | UDP/socket errors | Reading | Evidence |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 4 vCPU, default queue | 7.97 Gbit/s | 8.93 Gbit/s | 89.2% | 4.21 Gbit/s | 82.9% / 75.5% | none | Fresh baseline with per-CPU and softirq probes; still close to the prior 30s best. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-probe2-30s/` |
| 6 vCPU, default queue | 7.13 Gbit/s | invalid | n/a | 3.37 Gbit/s | 73.9% / 61.2% | none | More vCPU reduced CPU pressure but hurt throughput and destabilized the same-run TCP baseline. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-probe2-6vcpu-30s/` |
| 6 vCPU, repeat | 6.10 Gbit/s | invalid | n/a | 2.59 Gbit/s | 72.9% / 65.6% | none | Repeat confirmed the 6-vCPU shape is worse in this Hyper-V lab; do not use it as the default. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-probe2-6vcpu-repeat-30s/` |
| 4 vCPU, `net.core.netdev_max_backlog=250000` | 1.94 Gbit/s | invalid | n/a | 1.08 Gbit/s | 54.5% / 77.0% | none | Large backlog was a clear regression; reset to the default `1000` after the run. | `.gatherlink/hyperv-performance/20260528T-udp-pressure-wg-kernel-gso8-fixed3150-netdevbacklog250k-30s/` |

Reading:

- The 4-vCPU VM shape remains the best tested Hyper-V shape for this lab.
  Raising all three VMs to 6 vCPU did not expose hidden headroom; it lowered
  delivered UDP pressure and made the simultaneous TCP comparison unreliable.
- Per-CPU probes show the 4-vCPU baseline is busy but balanced. UDP receive
  buffer errors, UDP send-buffer errors, UDP memory errors, and interface drops
  stayed at zero, so the loss is not a simple socket-buffer failure.
- `net.core.netdev_max_backlog=250000` is a rejected tuning knob for this
  workload. It likely adds queueing/latency pressure rather than useful
  throughput and should not be documented as a Gatherlink lab default.
- Current stable recommendation stays: 4 vCPU, 8GB RAM, normal 1500-byte path
  MTU, WireGuard MTU 1420, `GSO=8`, send/receive batch 128, truncate receive,
  fixed `3.15 Gbit/s` per path for sustained 30-second UDP-pressure validation.
- To chase the final few percent, the next evidence layer should be host-side
  Hyper-V/vSwitch/RSS visibility or Linux kernel tracing inside the guests. The
  simple benchmark knobs tested here are either neutral or clearly negative.

## 2026-05-28 Host and guest pressure tracing follow-up

Goal: separate Hyper-V/vSwitch behavior from guest kernel WireGuard behavior
after the last-percent lab ceiling pass. This pass added a lightweight host
counter probe and richer guest probes for per-CPU busy time, softirq deltas, and
`/proc/net/softnet_stat` deltas.

| Shape | UDP pressure sink total | Matching TCP total | Host switch drops | Guest softnet signal | Reading | Evidence |
| --- | ---: | ---: | --- | --- | --- | --- |
| Raw private LAN, 15s, 3 x 3.15 Gbit/s offered | 9.45 Gbit/s | per-path TCP 44.9-50.4 Gbit/s | host probe unavailable for this first run | no `time_squeeze`, no UDP errors | Raw Hyper-V private links were healthy at the intended pressure. | `.gatherlink/hyperv-performance/20260528T-private-lan-hostprobe-15s/` |
| Kernel WireGuard, 15s, same offered pressure | 1.49 Gbit/s | 1.17 Gbit/s simultaneous TCP | host probe unavailable for this first run | VM A CPU2 showed `time_squeeze=8234`; no UDP socket errors | Kernel-WireGuard receive/decap, not raw LAN, was the bottleneck. | `.gatherlink/hyperv-performance/20260528T-wg-kernel-hostprobe-15s/` |
| Kernel WireGuard, 15s, bounded host probe | 1.36 Gbit/s | 0.89 Gbit/s simultaneous TCP | path switches reported thousands/sec outgoing drops during the pressure window | VM A CPU2 showed `time_squeeze=8395`; no UDP socket errors | The bad state is visible both at Hyper-V switch egress and in one guest softnet queue. | `.gatherlink/hyperv-performance/20260528T-wg-kernel-hostprobe2-15s/` |
| Raw private LAN, 8s, bounded host probe | 5.94 Gbit/s through `udp_pressure`; `iperf3 -u` stayed near target with 0% loss | per-path TCP 39.6-57.5 Gbit/s | near-zero path switch drops compared with the bad WG run | no `time_squeeze`, no UDP errors | Raw path behavior remains materially healthier than kernel-WG behavior. The 8s `udp_pressure` row is shorter/noisier than the earlier 15s row. | `.gatherlink/hyperv-performance/20260528T-private-lan-hostprobe2-8s/` |

Reading:

- The lightweight host probe now defaults to the minimal counter set. Full
  Hyper-V switch-port and vNIC counters are still available, but the minimal
  profile is the right default for concurrent benchmark runs.
- Guest tracing shows a sharp difference between raw LAN and kernel WireGuard:
  raw LAN spreads receive work without softnet squeeze, while the bad
  WireGuard state concentrates receive work on one CPU and increments
  `time_squeeze`.
- RPS and larger `net.core.netdev_budget` were tried outside the table and did
  not recover kernel-WireGuard aggregate throughput. `netdev_max_backlog`
  remains a rejected knob.
- A recovery reboot exposed stale Windows ARP/portproxy state. Hyper-V reported
  the VMs restored to 4 vCPU and 8GB RAM, but the Default Switch management IPs
  were not rediscoverable afterward and stale portproxy entries briefly landed
  in WSL instead of the guests. Do not trust VM benchmark rows after a VM shape
  change until SSH is proven to land on a Debian VM, not WSL, and `nproc`/memory
  match the Hyper-V settings.

Next useful work:

- Repair the Hyper-V Default Switch management path before further VM benchmark
  runs. The ARP-based resolver must reject broadcast addresses and must verify
  that SSH lands on a non-WSL Debian guest before caching an address.
- Keep the current performance conclusion narrow: raw Hyper-V paths are healthy;
  kernel-WireGuard aggregate is currently degraded in this lab state; Gatherlink
  tuning should pause until the VM management path and kernel-WG baseline are
  trustworthy again.
