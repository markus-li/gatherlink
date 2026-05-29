# WireGuard Over Gatherlink Status

This is the canonical current-status note for WireGuard-over-Gatherlink
performance and the known struggles around it.

Use this file for interpretation and tradeoffs. Keep exact benchmark rows,
commands, and run logs in
[`hyperv-performance-log.md`](hyperv-performance-log.md) or generated
benchmark reports.

## Current Status

WireGuard over Gatherlink is functional and useful for the current personal,
lab, and small-site scope. It is good enough to exercise real deployments,
including unstable multi-path WAN-style links, and it is backed by lab and VM
testing.

It should not be described as native WireGuard-speed multipath acceleration.
Direct WireGuard remains the baseline ceiling until Gatherlink matches it under
equivalent VM, MTU, shaping, packet-size, and offered-load conditions.

The honest status is:

- raw Gatherlink UDP and scheduler-driven UDP aggregation are strong
- WireGuard-over-Gatherlink works and is practically useful
- WireGuard-over-Gatherlink is still the hardest performance path
- one WireGuard peer flow carried across multiple Gatherlink paths can be hurt
  by jitter, packet reordering, socket handoff cost, and packet-rate limits
- adding more paths is not always better for ordered tunnel traffic

## Why This Is Hard

WireGuard is already an encrypted UDP tunnel. Gatherlink must treat WireGuard
packets as opaque UDP service payloads. It must not inspect WireGuard payloads,
infer inner flows, parse routes, or take ownership of WireGuard security.

That means Gatherlink can only schedule the outer WireGuard UDP packets. When
those packets contain ordered traffic, especially TCP inside WireGuard, blind
striping across uneven paths can create delay variation and reordering that the
inner transport interprets as congestion or loss.

The important interacting costs are:

- path latency, jitter, loss, and capacity differences
- application-facing UDP socket receive/send overhead
- AEAD, replay-window, and authenticated-session processing
- packet-rate pressure at smaller MTUs
- scheduler timing, flowlet boundaries, and reorder hold behavior
- WireGuard MTU and the resulting outer packet shape

## Current Operator Guidance

Use `coordinated_adaptive` as the normal scheduler starting point for
WireGuard services. It is the default general-purpose policy for current
Gatherlink use.

For WireGuard services that are expected to carry TCP-heavy traffic, set
`scheduler.traffic_bias` to `tcp` when using `coordinated_adaptive`. This keeps
the policy in Python while biasing the coordinator toward `single_best_path`
unless telemetry proves a safer option. `flowlet_adaptive` can still be chosen
for sticky tunnel behavior under jitter, and `ordered_multipath_capacity_aware`
is only eligible after multiple paths have real low-pressure packet history.
High receiver reorder pressure falls back instead of trying another multipath
split. Python keeps the best stable path active and leaves the others as
drain/probe paths so Rust can keep executing ordinary compiled primitives.

Live scheduler reapply is available for TCP-heavy services, but it should not
create dataplane churn when the compiled primitives are unchanged. The v0.9.2
runner now skips no-op scheduler reapply calls; use live reapply for long-lived
services and path changes, but keep direct fixed-policy rows beside it in
benchmarks so the reapply loop itself remains visible.

For TCP-biased `single_best_path`, equal-capacity paths are intentionally
stable by configured order rather than live-latency tie-breaks. Latency samples
are useful for many schedulers, but using noisy low-millisecond telemetry to
move one opaque WireGuard/TCP tunnel between otherwise equal paths caused
retransmits during live reapply. Capacity still decides the selected path when
one path is clearly better; the stable tie-break only avoids unnecessary TCP
path churn.

For explicit ordered modes, Gatherlink now keeps protected opaque services such
as single-profile WireGuard on a service-level `single_best_path` primitive by
default. This is intentionally not a global path drain: other services, control
traffic, and future probes can still use active paths. The 2026-05-28
verification showed why this matters. Before the service-level guard, explicit
`ordered_multipath_capacity_aware` over one WireGuard service at 900 Mbit/s
fell to 284.75-487.46 Mbit/s with 3.7-48.4% iperf UDP loss depending on the
path set. After syncing the guard to the VMs, the same explicit ordered command
compiled the WireGuard service to `single_best_path` and reached
711.16 Mbit/s with 0.00% iperf UDP loss. A raw Gatherlink UDP control row with
the same ordered scheduler and `udp_bulk` service still delivered
999.96 Mbit/s with zero packet delta, so the guard protects opaque ordered
tunnels without weakening raw UDP aggregation.

The same protection is now deterministic for the TCP-biased coordinator as
well. When Python chooses an effective `single_best_path` for a protected
service, it also compiles the selected allowed path id instead of leaving Rust
to repeatedly infer a best path from noisy low-millisecond primitives. The
2026-05-28 live-reapply VM check carried the WireGuard service on path-a only,
reached 705.37 Mbit/s at 900 Mbit/s offered with 0.03% iperf UDP loss, and kept
raw Gatherlink UDP at 1000.01 Mbit/s with zero packet delta in the paired
guardrail. This keeps the boundary clean: Python decides the service path plan,
Rust executes the compact primitive.

The next primitive step is also in place: `inherit` can now be constrained by
Python-selected `allowed_path_ids` without turning into single-best selection.
That lets Python trial ordered scheduling across only a proven subset of paths
when protected-service demand exceeds the best path. Rust still does not decide
when this is safe; it only runs the compiled node scheduler through the allowed
path filter. The paired 2026-05-28 guardrails stayed clean after the change:
raw Gatherlink delivered 999.92 Mbit/s with zero packet delta, and the
TCP-biased WG-over-GL live-reapply row delivered 707.84 Mbit/s at 900 Mbit/s
offered with 0.00% iperf UDP loss while remaining pinned to path-a.

Python now applies the subset proof instead of requiring every healthy path to
be proven. For protected services, promotion to `inherit` requires the current
best path to be proven, at least two proven paths, and enough capacity in that
clean subset to cover observed service demand with headroom. Healthy but noisy
or unproven paths are left out of the allowed set. The 2026-05-28 VM guardrails
after this change delivered raw Gatherlink at 1000.13 Mbit/s with zero packet
delta and WG-over-GL at 733.16 Mbit/s with 0.00% iperf UDP loss. That WG row
still remained single-best because live safety gates did not yet prove ordered
aggregation; treat the result as a no-regression guardrail plus a cleaner
promotion mechanism, not as a solved ordered-TCP claim.

Before trying that subset promotion, protected services now make one more
Python-owned conservative choice: if the current low-latency path cannot cover
observed service demand but another single healthy path can, the service moves
to that sufficient path instead of immediately entering ordered multipath.
This keeps opaque TCP/WireGuard on one ordered path whenever possible. A short
2026-05-28 fiber+5G-shaped VM check after the change reached 685.93 Mbit/s TCP
with 321 retransmits on an 800/160/80 Mbit/s profile, so the behavior remains
in the existing useful band without claiming a new best row.

The coordinator also now uses existing duplicated control frames as low-rate
path probes. This avoids the chicken-and-egg where a protected service pinned
to path-a could never gather user-service packet proof on path-b or path-c.
Python consumes the per-path control counters already present in service
status; Rust still only reports counters and executes compiled primitives. The
2026-05-28 control-probe guardrail kept raw Gatherlink at 1000.12 Mbit/s with
zero packet delta. WG-over-GL stayed on `single_best_path` and delivered
692.58 Mbit/s at 900 Mbit/s offered with 0.00% iperf UDP loss because the live
latency window still showed high jitter on the active path. That is a useful
fail-safe result: more proof is available, but Python still refuses ordered
promotion when latency quality is not clean enough.

The follow-up latency-quality change keeps that decision better informed:
control metadata can now carry RTT, clock-error budget, directional jitter, and
p95 alongside latency source/confidence. Those are still Python-owned scheduler
facts. They are not data-frame header fields, and Rust does not use them to
make policy decisions.

Current receiver-feedback status: Python can now recognize a saturated
best-capacity path and has a bounded ordered-capacity escape candidate, but
opaque WireGuard/TCP still defaults to service-level `single_best_path` unless
the service explicitly opts into inheriting the node scheduler. The 2026-05-29
asymmetric 800/150 Mbit/s VM proof shows why. With the default guard, a
920 Mbit/s offered WG-over-GL UDP row delivered 663.10 Mbit/s by paired iperf
UDP with 12.49% loss, while 8-flow `udp_pressure` reported 677.45 Mbit/s. When
the same protected service was allowed to inherit ordered scheduling, it
collapsed to 165.49 Mbit/s with 52.68% loss because the slow path took too much
opaque WireGuard traffic. Treat ordered inherit as an experiment knob, not a
default recommendation.

`udp_pressure` is now close enough for tuning but still slightly below paired
iperf UDP in clean WG-over-GL runs. On the same fiber+5G shape at 650 Mbit/s
offered, iperf UDP delivered 646.24 Mbit/s with no loss while 8-flow
`udp_pressure` delivered 621.58 Mbit/s, or 96.2% of iperf. The 30-second
confirmation was 596.44 Mbit/s versus 618.89 Mbit/s, or 96.4%. Keep paired
iperf rows beside pressure rows when a benchmark claim is close to a gate.
infer TCP policy.

The 2026-05-29 VM guardrails found three useful correctness issues in that
telemetry path. First, optional latency-stat fields must accept zero as
"unknown/not warmed yet"; rejecting zero could stop startup with
`path latency value out of range`. Second, `data-traffic-one-way` samples are
not trusted for TCP promotion until their confidence is `good`; warming samples
remain diagnostic only. Third, per-path one-way latency now uses the selected
per-path peer clock offset instead of applying the global median offset to every
path. The paired raw Gatherlink guardrail stayed clean at 999.96 Mbit/s with
zero packet delta. WireGuard-over-Gatherlink stayed lossless but conservative
at 731.90, 713.31, and 639.91 Mbit/s across the three checks, all still
compiled to `single_best_path`. The reading is intentionally sober: the
estimator is safer and more specific, but VM timing under nested WireGuard load
is still too noisy to justify automatic ordered promotion.

A follow-up row changed rejected latency candidates to be non-destructive while
fresh accepted samples are still in the rolling window. That improved the
guardrail to 702.05 Mbit/s with 0.00% loss and made status more useful:
path-b/c kept good clock-synced facts while also showing their latest rejected
candidate reason. The scheduler still stayed on `single_best_path`, which is
the right result because those paths did not yet have enough low-jitter,
payload-proven evidence for one opaque WireGuard flow.

The best short row from the same slice was 743.07 Mbit/s after making sink
authoritative real-data confidence explicit. A later provenance-priority
correctness pass was tested as well, but its short VM row fell to
611.53 Mbit/s and did not surface real-data proof in final status, so it is
kept as correctness groundwork rather than a speed claim.

The next 2026-05-29 clock-consensus slice added minimum-delay weighted-median
offset selection, per-path base RTT/uncertainty/drift summaries, and a
single-path guardrail. The paired raw Gatherlink VM guardrail remained clean:
999.99 Mbit/s generated, 1000.19 Mbit/s received, and zero application-packet
delta. The short WireGuard-over-Gatherlink VM guardrail stayed functionally
clean with 0.00% iperf UDP loss, but delivered 654.70 Mbit/s, or 65.5% of that
paired raw Gatherlink guardrail. Treat this as better scheduler input and
operator visibility, not a speed win.

On the lossy `external-starlink-queue-dynamics` preset, the same clock facts
become more obviously useful. A capacity-aware raw Gatherlink run delivered
160.82 Mbit/s at 180M offered. WireGuard-over-Gatherlink on the same shape
delivered 162.72 Mbit/s with 8.45% iperf UDP loss: 101.2% of the paired raw
Gatherlink run and 97.5% of a direct kernel-WireGuard simultaneous UDP-pressure
sink sum of 166.87 Mbit/s. The monitor/status payload showed `good` per-path
clock confidence but large uncertainty, roughly 48/74/94 ms across the three
paths, which is the right caution signal for high-jitter lossy scheduling.
After the TCP latency-risk guard, the same UDP-biased profile stayed in-band at
162.15 Mbit/s with 8.44% iperf UDP loss, and the fixed UDP-pressure sink
reported 160.97 Mbit/s. That is the intended split: opaque/TCP services avoid
dangerous timing evidence, while UDP/bulk services can still use capacity-aware
striping on lossy multipath.

Two additional lossy/asymmetric checks clarify scheduler choice. Raw
Gatherlink on `realworld-starlink-plus-2x5g` delivered 346.05 Mbit/s at 350M
offered, or 98.9% delivery, so the normal three-path lossy facsimile remains
healthy. On `external-fiber-5g-asymmetric`, raw Gatherlink with
`single_best_path` delivered 747.55 Mbit/s at 750M offered with zero packet
delta. A WireGuard-over-Gatherlink capacity split on the same shape was
rejected: it collapsed to 141.53 Mbit/s with 82.90% iperf UDP loss and
multi-second data-latency/queue buildup. The conservative single-best run at
650M offered reached 638.86 Mbit/s with 0.00% iperf UDP loss, or 85.5% of the
paired raw Gatherlink baseline. This reinforces the current rule: opaque
WireGuard should stay conservative on heavily asymmetric links until ordered
multipath has stronger receiver-feedback control.

The first UDP-pressure check on that conservative fiber+5G row under-reported
the receiver rate because multi-worker sinks included idle worker wait time in
the final throughput denominator. The tool now reports active receive windows
for workers that actually saw packets. With 8 flows/workers it reached
609.95 Mbit/s, or 94.5% of the paired iperf UDP receiver row at 645.24 Mbit/s.
One-flow UDP-pressure still underfills this nested WireGuard path at roughly
505 Mbit/s, so benchmark reports should keep fanout explicit and continue
showing the paired iperf receiver row when validating WG-over-GL ceilings.

The TCP-biased coordinator is now the best clean-link result in the Hyper-V
one-hop lab: the 2026-05-24 refresh reached 952.28 Mbit/s TCP with zero
retransmits in a quick post-cleanup smoke, and the longer tuning row reached
929.74 Mbit/s TCP with zero retransmits after stale one-hop service cleanup was
fixed. On fiber+5G shaping it remained in the previous best band at
697.03 Mbit/s TCP, and a concurrent TCP+100M UDP run delivered 605.82 Mbit/s
TCP while UDP stayed at target with 0% loss.

Do not tune Starlink/mobile facsimiles from low TCP stream counts. The
2026-05-24 Starlink+5G p8 run underfilled the high-latency path and produced
misleading ~17 Mbit/s results. The comparable p24 run reached 48.44 Mbit/s,
which is close to the earlier direct-kernel-WireGuard shaped path-a baseline
of 56.66 Mbit/s. A p48 confirmation then reached 93.84 Mbit/s. Treat those
profiles as BDP-sensitive before blaming the Gatherlink scheduler, and include
high-concurrency rows when the scenario is meant to approximate many active TCP
flows through one WireGuard tunnel.

For mixed TCP+UDP on the same high-BDP Starlink+5G tunnel, the 2026-05-24 p48
run delivered 62.69 Mbit/s TCP while a concurrent 100M UDP stream stayed at
98.86 Mbit/s with 0% UDP loss. This is the more realistic "many TCP flows plus
some UDP" row; it should sit beside TCP-only rows, not replace them.

The Starlink+2x5G facsimile showed the same shape: p48 TCP-only reached
99.98 Mbit/s, while p48 mixed TCP+100M UDP delivered 66.11 Mbit/s TCP and
98.90 Mbit/s UDP. A `capacity_aware` contrast row delivered slightly lower TCP
at 61.89 Mbit/s, so the current conservative TCP-biased coordinator remains
the better default for these mixed tunnel checks.

Use `flowlet_adaptive` when the goal is sticky tunnel behavior and better
stability for TCP-like traffic inside WireGuard. This can sacrifice some raw
aggregation to avoid damaging ordered flows.

For clean VM links, do not assume service-level flowlet pinning or three-way
striping is a free win. The 2026-05-23 TCP-only tuning pass found that explicit
50 ms / 60 s service flowlet pinning could hide the actual multipath scheduler
and leave a busy WireGuard source pinned to one path for most of the run. A
later clean-service rerun showed why benchmark hygiene matters: with stale
one-hop services removed, single-path TCP reached 867.62 Mbit/s with zero
retransmits, while clean three-path `capacity_aware` reached 688.58 Mbit/s and
`ordered_multipath_capacity_aware` reached 791.17 Mbit/s. Treat flowlets and
ordered striping as explicit tunnel-stability experiments, not the generic
clean-link default.

Do not use ordered multipath modes as the default WireGuard recommendation yet.
They are useful research and benchmark tools, but they are not the current
safe default for production-like WireGuard services.

The 2026-05-24 ordered-mode sweep made that sharper rather than vaguer:
implicit Rust packet-thread pacing was a drag on clean TCP-over-WireGuard, so
ordered policies now bypass pacing unless an operator explicitly configures a
budget. Small early-arrival smoothing in Rust helps the ordered virtual
timeline, with multiplier `16` the current best balance in the clean VM sweep
at 711.24 Mbit/s. Multiplier `32` pushed raw throughput higher but created a
large TCP retransmit spike, so it is rejected for now. This is progress, not a
default change: `single_best_path` and TCP-biased `coordinated_adaptive` remain
the safe WireGuard default.

The later latency-provenance and TCP-bias passes improved the failure mode
rather than making ordered mode the default. Python now sends compact latency
source/confidence facts over control metadata, so peer schedulers can tell
real-data one-way samples from coarse clock-sync or peer-advertised values.
That made it clear that `ordered_multipath_capacity_aware` can degrade into a
useful best-path-like result when live telemetry is present, but also that
adding small slow paths to one opaque TCP-heavy WireGuard flow still hurts
throughput. The TCP-biased coordinator now intentionally selects
`single_best_path` for WireGuard/TCP until a future ordered scheduler has
stronger sender-side in-flight and receiver-feedback control. The 2026-05-24
final check reached 701.99 Mbit/s on the asymmetric fiber+5G profile and
799.28 Mbit/s on a clean three-path profile with this conservative policy.
Clean-link ordered mode remains behind the best coordinated/single-best rows
and can still add retransmits, so the production recommendation is unchanged:
use the TCP-biased coordinator unless explicitly testing ordered multipath.

The 2026-05-25 confirmation sweep kept that recommendation intact. Explicit
ordered mode was slower than coordinated TCP on both clean and fiber+5G rows,
p12 over-drove clean TCP, and WG MTU 1200 was worse than the current MTU 1280
recommendation on Starlink+5G. Treat those as rejected tuning contrasts unless
a future ordered scheduler proves stable gains against the same direct
userspace-WireGuard and raw-GL comparison rows.

A later 2026-05-25 ordered-feedback sweep tightened the evidence rather than
changing the recommendation. On clean three-path VM links, fresh simultaneous
userspace WireGuard reached 7336.51 Mbit/s TCP across the path set. The best
Gatherlink row in that sweep was `coordinated_adaptive` at 822.97 Mbit/s TCP
and 63.4% of that sweep's raw-GL row; `ordered_multipath_capacity_aware`
reached 746.97 Mbit/s TCP, or 90.8% of coordinated adaptive. On
`external-starlink-5g-high-bdp`, the matching direct userspace-WireGuard
baseline was 180.99 Mbit/s TCP and 289.87 Mbit/s UDP. Coordinated adaptive was
again best for TCP at 72.21 Mbit/s, while single-best carried the strongest
UDP component at 161.19 Mbit/s. Ordered-capacity reached only 52.66 Mbit/s TCP.
This means the new ordered credits are useful guardrails, not enough proof to
auto-promote ordered multipath for opaque WireGuard/TCP.

The same pass added the `external-starlink-queue-dynamics` VM shape. It is
intentionally harsher than the simpler Starlink+5G high-BDP shape: shallow
queues and high jitter make the usable ceiling far below the nominal path-rate
sum. Direct userspace WireGuard on path-a reached about 141 Mbit/s TCP, while
kernel WireGuard was lower on this particular TCP shape at about 82 Mbit/s.
WG-over-GL best-path rows reached about 73-75 Mbit/s. That confirms the
coordinator is choosing the right kind of policy, but queue-sensitive TCP
behavior remains the bottleneck and can dominate even the kernel/userspace
comparison. A smaller Gatherlink core batch size of `128` was slightly better
only in one path-a-only contrast. After the ordered idle-gap executor fix was
rebuilt into the VM extension, the best clean three-path TCP-biased
coordinated row used the normal `512` core batch size at 896.85 Mbit/s with
zero retransmits. Treat `--batch-size` as an explicit diagnostic knob, not a
universal TCP default.

A follow-up live-reapply check confirmed the stable tie-break fix: the same
clean p8 coordinated TCP shape that previously fell to 769.14 Mbit/s with
12996 retransmits now runs at 832.92 Mbit/s with zero retransmits. The best
no-reapply row is still faster at 896.85 Mbit/s, so this is a correctness and
stability fix rather than proof of TCP multipath aggregation.

The same pass retested clean p8 non-ordered alternatives. `arrival_guarded_capacity`
reached 675.91 Mbit/s, `flowlet_adaptive` with a 25 ms / 100 ms service flowlet
reached 775.38 Mbit/s, and `capacity_aware` reached 673.21 Mbit/s. All three
were below the TCP-biased single-best row and introduced retransmits. Keep them
available for UDP, mixed-service, and explicit scheduler research, but do not
promote them as the safe default for one opaque WireGuard/TCP flow.

Capacity/coordinated modes without flowlet stickiness can be better for raw UDP
aggregation or some lossy/uneven profiles. They can also hurt TCP-like tunnel
traffic when the packet stream is split too aggressively.

For advanced mixed traffic, use the dual-WireGuard profile: send TCP/default
traffic through a stable WireGuard-over-Gatherlink service and UDP/high-rate
traffic through a fast WireGuard-over-Gatherlink service. This avoids packet
inspection while giving the scheduler meaningful service-level intent. The
profile is not the default because it requires two WireGuard interfaces, two
Gatherlink services, and reviewed local firewall or policy-routing rules.
Current dual-profile configs now use small per-service path primitives. Python
still owns the policy decision: the runner compiles the stable service with
`scheduler_path_policy=single_best_path` and the fast service with
`scheduler_path_policy=weighted_round_robin`. It also compiles service-level
path eligibility and path weights. By default the stable profile uses the
highest hinted-capacity active path and the fast profile uses the smallest
remaining path set that can cover the configured UDP target with headroom;
operators or tests can override both sets explicitly with `--stable-paths`,
`--fast-paths`, and `--fast-path-headroom`. Rust does not learn WireGuard
meaning or TCP/UDP policy; it only executes the compiled selector, path
eligibility, path weights, flowlet, run, and fanout primitives.
The current VM proof for this mode is a clean-link 15-minute-per-mode run:
stable TCP averaged 849.62 Mbit/s, fast UDP sustained 300.00 Mbit/s with 0%
loss, and both profiles completed WireGuard ping checks through Gatherlink.
The TCP leg still showed retransmits, so treat this as a useful advanced
operator mode rather than proof that nested WireGuard TCP tuning is finished.
Concurrent dual-profile testing is now supported with
`run_dual_wireguard_gatherlink_speed.sh --mixed`. The first clean-link mixed
proof delivered 425.32 Mbit/s TCP on the stable interface and 222.23 Mbit/s UDP
on the fast interface at a 300M UDP target. Removing the stable flowlet let UDP
hit target but introduced many TCP retransmits. The next clean-link pass added
the same `scheduler.traffic_bias=tcp` knob to the dual-profile runner and
improved the mixed result to 562.66 Mbit/s TCP plus 295.33 Mbit/s UDP with zero
TCP retransmits and zero UDP loss in a 12-second proof. For now, dual WireGuard
should still be presented as advanced traffic-class isolation and policy
control, not as an automatic speed win over a single WG-over-GL tunnel.
After per-service path weights and explicit stable/fast path selection landed,
a clean mixed 15-second proof delivered 520.66 Mbit/s TCP plus 293.51 Mbit/s
UDP with zero TCP retransmits and zero UDP loss. That is a little below the
best earlier clean dual row, but it proves the configurable service split still
works after the scheduler primitive change.
The latest Starlink+2x5G mixed pass also proved why the fast profile should use
the smallest sufficient path set. `service_path_stats` showed the policy was
being executed correctly; fast b+c was too jittery for a 100M UDP target, while
fast b only delivered 154.23 Mbit/s TCP plus 98.09 Mbit/s UDP with no UDP loss.
The benchmark auto-selector therefore keeps the fast service compact unless
the configured headroom actually requires multiple paths.
The next automatic step is now implemented in Python scheduler policy: under
`coordinated_adaptive`, those stable/fast path sets are live hints instead of
hard-coded tunnel wiring. Protected services remain sticky and fail over when a
path is unhealthy; bulk services can grow or shrink their eligible path set from
observed service rate and path health. That is the right default direction for
real traffic mixes where TCP and UDP volumes change over time.
The dual-profile benchmark runner now starts services with scheduler hot
reapply enabled by default. Without that, the VM proof path was only validating
the initial compiled split and could not exercise dynamic service-path movement
during the run.

The fair clean-link comparison is now close rather than one-sided: one
WG-over-GL tunnel with TCP bias delivered 535.71 Mbit/s TCP and 296.04 Mbit/s
UDP at the same 300M UDP target, while dual profile with TCP bias delivered
562.66 Mbit/s TCP and 295.33 Mbit/s UDP. Keep dual-profile work focused on
explicit operator traffic-class separation and future per-service policy knobs,
not on claiming it is always better.

The dual-profile runner now also uses the same named Hyper-V shaping profiles
as the single-WireGuard runner. In the first shaped proof, fiber+5G delivered
600.65 Mbit/s TCP plus 99.30 Mbit/s UDP at a 100M UDP target, which is
essentially level with the single-WG mixed fiber+5G row while keeping the UDP
class on its own WireGuard interface. The benchmark runner defaults to
`scheduler.traffic_bias=udp` for this profile and gives the stable service an
explicit `scheduler_path_policy=single_best_path`. This keeps the fast service
on capacity-oriented path sharing without forcing the TCP-sensitive service to
stripe blindly.
After per-service path weights and explicit path-set controls, the same
fiber+5G mixed shape delivered 605.23 Mbit/s TCP plus 99.63 Mbit/s UDP. The
result confirms the updated path controls did not regress this representative
real-world profile. TCP retransmits remain visible, so the next tuning work is
endpoint/TCP stability, not whether the service split is working.
A p4 iperf `-Z` contrast delivered 589.19 Mbit/s TCP plus 99.73 Mbit/s UDP
with fewer TCP retransmits. That is the cleaner current candidate for
operator-style TCP validation, while p8 remains the faster raw mixed row.
The next pass found that `path-b` alone was a little too compact even though it
had enough nominal capacity for the 100M UDP target. It delivered good
throughput, but the source side still showed send-buffer pressure. The runner
now keeps clean links compact but adds one spare fast path when a single fast
path is below 2x the requested fast-service target. On fiber+5G this changes
the automatic fast service from `b` to `b,c`, keeps UDP lossless, and reduces
TCP retransmits compared with the previous auto-selected `b` row. Explicit
`b,c` reached 601.39 Mbit/s TCP plus 99.39 Mbit/s UDP; the proof of the new
automatic selection reached 591.11 Mbit/s TCP plus 99.38 Mbit/s UDP. Parallel
p4/p6/p10, benchmark-side `iperf3 -Z`, WG MTU 1360, and disabling the stable
flowlet were all rejected for this profile. A core batch-size sweep showed the
same thing: 256 and 128 were only small/noisy changes, while 64 was a clear
regression, so the runner keeps the existing 512 default.

On Starlink+5G and Starlink+2x5G, distinguish component checks from true mixed
traffic. Older p96 dual-profile rows were useful but had `run_mixed: 0`, so TCP
and UDP were measured sequentially. Re-running Starlink+5G with automatic
profile hints reproduced the component result at 153.63 Mbit/s TCP plus
98.71 Mbit/s UDP. When TCP and UDP ran concurrently, the same automatic
scheduler delivered 92.81 Mbit/s TCP plus 97.61 Mbit/s UDP at a 100M UDP
target. Lowering the UDP target to 50M recovered TCP to 126.41 Mbit/s while
keeping UDP lossless, and 75M landed between those rows. Starlink+2x5G showed
the same concurrent-pressure pattern at 87.66 Mbit/s TCP plus 97.62 Mbit/s UDP.
The dual-WireGuard runner now compiles service priority automatically: stable
WireGuard is `high` and fast WireGuard is `bulk`. On Starlink+5G this produced
one improved 100M mixed row at 103.14 Mbit/s TCP plus 97.81 Mbit/s UDP with 0%
UDP loss, but a later confirmation fell back to 90.74 Mbit/s TCP. A stronger
`critical` priority probe was also worse at 96.02 Mbit/s TCP, so `high`/`bulk`
is a sane class-separation default but not a solved automatic performance fix.
This is still not a manual path-selection problem: it is evidence that the
automatic policy needs service-level budget/QoS primitives so fast UDP cannot
starve stable TCP under noisy satellite/mobile profiles.
The first bounded service-drain quantum primitive is implemented and tested,
but it is not enabled by default. Fast-service drain quanta of 128 and 256
packets were both worse than the normal full-batch drain on Starlink+5G; 384
packets was also worse. Use that knob only as an explicit experiment until a
better adaptive byte/time budget controller exists.
Live service-outcome feedback is now wired through the existing service IPC
rather than a side file. The Hyper-V TCP outcome probe now sees retransmit pain
during a run by combining per-socket counters with a benchmark-scoped Linux
`RetransSegs` delta fallback. Python can react by compiling bounded bulk
packet/byte caps while Rust only executes those narrow primitives. The first
Starlink+2x5G mixed proof showed the loop working in diagnostics and service
status, but throughput stayed around 93-95 Mbit/s TCP plus 97 Mbit/s UDP. Even
with an earlier retransmit trigger, the row did not materially improve. A
follow-up attempt to halve the protected-outcome cap was worse and has been
reverted. This is good negative evidence: live outcome feedback is necessary
plumbing, but simple bulk capping is not the final TCP-over-WireGuard-over-
Gatherlink scheduler.
The next useful QoS work is earlier sender-side pacing or better TCP-outcome
prediction, still Python-owned.
The same priority split improved the Starlink+2x5G concurrent row from
87.66 Mbit/s TCP to 100.48 Mbit/s TCP while preserving the 97.62 Mbit/s
lossless fast UDP leg. That is useful, but it remains below the direct
userspace-WireGuard and raw Gatherlink gates.

The external fiber+5G asymmetric comparator is healthier. With automatic
stable/fast path selection it delivered 601.93 Mbit/s TCP plus 99.53 Mbit/s UDP.
That is useful and comfortably above common hosted/prosumer relay bars, but it
still misses the 75% WireGuard path-set gate when judged against the configured
950 Mbit/s expectation. Treat it as a good absolute result, not a solved 90%
target.

On the harsher `external-starlink-queue-dynamics` shape, dual-profile mixed
tests first showed the shared-policy limitation clearly. With global
`traffic_bias` set to `tcp`, TCP stayed near the single-WG result at
72.86 Mbit/s but the fast UDP leg only reached 17.57 Mbit/s. After adding
per-service path eligibility, the fast UDP leg reached 97.59 Mbit/s with 0%
loss while stable TCP reached 53.14 Mbit/s. A later p8 check after per-service
path weights showed 98.90 Mbit/s UDP and 14.90 Mbit/s TCP with the stable path
on `path-a`; moving the stable service to `path-b` was worse at 13.05 Mbit/s
TCP. A matching p8 direct userspace-WireGuard check showed this profile is
itself harsh for TCP: path-a delivered 18.22 Mbit/s alone and 33.14 Mbit/s in
the simultaneous path-set run, while UDP remained clean. That proves the split
is real and controllable, but also shows endpoint/runtime pressure and the
underlying queue profile both matter when both profiles are busy. The 2026-05-25
automatic p96/50M concurrent row reached 40.64 Mbit/s TCP plus 47.66 Mbit/s UDP,
which keeps the profile in diagnostic territory rather than release-claim
territory.

Sequential TCP-then-UDP benchmark rows are component checks. They prove each
traffic class works in isolation, but they do not prove normal mixed use. For
release/performance claims about running TCP and UDP over the same WireGuard
deployment, add a concurrent TCP+UDP benchmark row where both classes are
active at the same time. Use
`tools/hyperv/run_onehop_wireguard_gatherlink_speed.sh --mixed` for that
one-hop VM shape.

For WireGuard MTU:

- start with MTU `1380` on normal 1500-byte underlay paths
- test MTU `1280` on lossy or jittery path sets
- test MTU `1200` on very uneven mobile or satellite-style profiles
- avoid treating MTU `1420` as a blind improvement; current Hyper-V
  real-world facsimiles showed it can hurt TCP-like WireGuard traffic over
  Gatherlink

`PersistentKeepalive` remains a WireGuard liveness/NAT setting. It should not
be treated as a throughput tuning knob.

## What Good Means Here

For the v0.9.x line, "good" means:

- packets are carried correctly through the documented service mapping
- failure and recovery behavior is observable
- the result is stable enough for real personal/lab use
- performance claims are tied to measured runs
- the docs do not imply direct WireGuard parity unless the matching benchmark
  evidence exists

It is fair to call the current result useful and promising. It is not yet fair
to call it solved as a high-speed WireGuard multipath accelerator.

## Active Struggles

The main open performance work is not another packet-header redesign. The
WireGuard-over-Gatherlink packet shape should stay the normal Gatherlink
service payload path.

The likely improvement areas are:

- endpoint socket handoff profiling
- reducing syscall and datagram-per-second pressure
- better sender pacing from path feedback
- safer flowlet timing for tunnel traffic
- deciding when fewer paths are better than more paths
- bounded sender-side in-flight behavior for ordered multipath experiments
- receiver feedback that helps scheduling without inspecting payloads

Any future improvement must keep the protocol boundary intact: Gatherlink
carries authenticated UDP service payloads; WireGuard remains WireGuard.

## Related Docs

- [`docs/user/wireguard.md`](../user/wireguard.md): short user workflow
- [`docs/helpers/wireguard-helper.md`](../helpers/wireguard-helper.md): helper boundary and implementation scope
- [`docs/runtime/scheduler.md`](../runtime/scheduler.md): scheduler semantics
- [`docs/benchmarks/hyperv-performance-log.md`](hyperv-performance-log.md): measured VM benchmark rows
- [`docs/architecture/performance-philosophy.md`](../architecture/performance-philosophy.md): performance guardrails
