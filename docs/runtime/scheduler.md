# Scheduler

Gatherlink scheduling is split across Python and Rust.

Python owns scheduler policy, scoring, path state interpretation, weights,
operator-facing explanations, and future adaptive behavior. Rust owns only the
compiled hot-path scheduler state it needs to choose a path cheaply while
encoding packets.

## Runtime Modes

The default scheduler mode is still `round_robin`, but the runtime contract now
has named modes that let Python choose a policy and keep Rust on primitive
execution:

- `round_robin`: visit compiled paths in order, applying weights and MTU/state eligibility
- `weighted_round_robin`: explicit alias for the weighted path sequence Rust already executes
- `srtt`: Python policy alias for lowest-smoothed-latency selection, inspired by MPTCP's default RTT-first behavior
- `lowest_latency`: pick the eligible path with the lowest compiled latency estimate
- `loss_aware`: pick the eligible path with the lowest compiled loss estimate
- `capacity_aware`: Python compiles TX capacity estimates into path weights so
  Rust splits traffic proportionally while still applying MTU/state eligibility
- `least_queue`: queue-depth-driven selection using Python-compiled queue depth, byte depth, and oldest-age primitives
- `earliest_completion_first`: MPTCP ECF-inspired policy using latency plus estimated transmit time
- `blocking_estimation`: BLEST-inspired policy that avoids paths likely to create reorder blocking
- `ordered_multipath`: MPTCP-inspired service-flow scheduler that uses the
  global service sequence, compiled path latency/capacity/queue facts, and a
  tiny Rust virtual send timeline to choose the path with the earliest safe
  predicted arrival
- `ordered_multipath_capacity_aware`: same ordered Rust primitive, but Python
  compiles capacity-derived weights and pressure feedback so it can be tested
  separately from the original ordered baseline
- `single_best_path`: Python chooses one currently best active path by
  capacity, latency, loss, and configured order, then marks other paths as
  drain/probe paths before Rust sees the compiled weighted primitives; this is
  useful for TCP-heavy opaque tunnels where intentional striping hurts more
  than it helps
- `arrival_guarded_capacity`: Python compiles capacity-derived weights, then
  demotes paths whose predicted arrival would fall outside the receiver reorder
  budget; Rust still executes ordinary weighted scheduling primitives
- `balanced`: hybrid ranked policy that combines capacity, latency, loss, and queue facts
- `adaptive`: Python scoring compiles live telemetry into weights, states, and primitive limits; Rust executes it like weighted round-robin
- `coordinated_adaptive`: Python meta-policy that chooses a concrete policy
  such as `capacity_aware`, `single_best_path`, `arrival_guarded_capacity`,
  `latency_guarded_capacity`, `ordered_multipath_capacity_aware`, or
  `flowlet_adaptive` from current telemetry and then compiles that normal
  policy for Rust

Scheduler mode is directional. It describes this node's local TX decisions, not
the whole connection. Two peers may legitimately use different scheduler modes
at the same time: for example, the client may use `flowlet_adaptive` for
WireGuard request traffic while the sink uses `capacity_aware` or
`coordinated_adaptive` for reply traffic. Gatherlink therefore tracks both the
local TX scheduler and the peer-advertised TX scheduler in control metadata, but
the peer value is diagnostic/context only. It must not override local policy.

## Per-Service Path Primitives

Node scheduler mode is not always enough. Some helpers expose more than one
Gatherlink service on purpose. The dual-WireGuard helper is the clearest
example: the stable/TCP profile should often stay on one conservative path,
while the fast/UDP profile should use an aggregation-friendly policy. Python
therefore compiles small per-service path primitives before Rust sees runtime
state:

- `traffic_class`: Python-owned service meaning such as `tcp_ordered`,
  `udp_bulk`, `latency_sensitive`, `control`, or `unknown`. Helpers and config
  may set this before encryption; Rust never infers it from payloads.
- `scheduler_path_policy="inherit"`: use the node-wide compiled scheduler
- `scheduler_path_policy="single_best_path"`: Rust picks the best eligible path
  from Python-compiled capacity, latency, loss, and path order
- `scheduler_path_policy="weighted_round_robin"`: Rust uses an independent
  weighted cursor for that service
- `scheduler_allowed_paths=[...]`: user config names allowed paths; expansion
  converts them to compact path ids as `scheduler_allowed_path_ids`
- `scheduler_path_weights={...}`: user/config tooling names per-service path
  weights; expansion converts them to compact `(path_id, weight)` pairs as
  `scheduler_path_weights`

The allowed-path list and path-weight map are execution primitives, not Rust
policy. They let Python split traffic classes cleanly, such as stable service
on `path-a` and fast service on `path-b,path-c`, without moving helper meaning
or traffic classification into the dataplane. If a service supplies path
weights, Rust uses those weights only for that service's independent weighted
cursor; unlisted paths are not selected by that service unless Python also
compiles a different primitive later.

Under `coordinated_adaptive`, explicit service path sets are starting hints
rather than a promise to keep using the same path forever. Python can recompile
the service primitives when status shows a path is down, lossy, queued, or no
longer sized for the observed service rate. Protected services are identified
from priority or `traffic_class`: `tcp_ordered`, `latency_sensitive`, and
`control` services stay sticky on one healthy path and fail over only when the
current eligible path is unhealthy. `udp_bulk` services are allowed to expand to
aggregation-friendly path sets even when their human priority remains `normal`.
Python-side service outcome
feedback, for example TCP retransmits rising in a WireGuard helper benchmark,
is recorded in the allocator decision, but it does not by itself steal another
service's path while cheap path counters remain healthy. Bulk/fast services can
expand to the smallest healthy spare path set that covers their observed
transmit rate with modest headroom, then shrink again when demand no longer
justifies the extra paths. Fixed-with-failover or manually pinned splits remain
useful advanced operator controls, but the general target is that
`coordinated_adaptive` makes the good automatic choice from live counters and
bounded outcome facts.

`coordinated_adaptive` also reads a compact Python-owned service-traffic
summary. When `scheduler.traffic_bias` is `auto`, a config containing only
`tcp_ordered`, `latency_sensitive`, or `control` services starts from the
TCP-safe `single_best_path` posture. A config containing only `udp_bulk`
services starts from a capacity-friendly posture. Mixed protected/bulk configs
use an aggregation-capable node baseline while the per-service path allocator
keeps protected services sticky and lets bulk services expand onto spare
capacity. Service outcome facts, such as protected-service degradation, are
inputs to this Python policy and to service budgeting; Rust still receives only
path eligibility, weights, polling budgets, byte caps, and other primitive
execution values.

The intended Python planner shape is two-stage:

1. Compile each service as if it were the only service, using service intent
   such as TCP-stable, UDP-fast, control, helper, or bulk.
2. Reconcile those service plans against aggregate path pressure and total
   traffic so the final primitive set does not overfill one path or starve a
   higher-value service.

Rust receives only the reconciled primitive values.

The service-path allocator also tracks same-pass bulk reservations. If several
bulk services are active, the first bulk plan claims part of the currently
available path capacity before the next bulk service is planned. This keeps two
UDP-fast services from both assuming the same spare path is empty while still
compiling only ordinary allowed-path and weight primitives for Rust.

The coordinator also classifies each path into a weak responsiveness class for
diagnostics and future policy tuning:

- `wired_stable`
- `cellular_like`
- `starlink_like`
- `volatile_wireless`
- `unknown`

These classes are hints from Python-visible latency, jitter, loss, queue, and
capacity facts. They are not Rust policy, not device detection, and not a
replacement for explicit operator config. Fixed config remains the startup
truth; responsiveness classes help explain why `coordinated_adaptive` held a
safe policy, tested a more aggressive policy, or rejected a switch.

The live coordinator keeps a bounded per-path history of these classes and
exports `path_profiles` in `scheduler.decision` diagnostics with the current
class, stable class, confidence ppm, and number of windows. This history is a
Python-only guardrail: it helps avoid reacting to one noisy sample, but it does
not give Rust access-type policy and it does not override explicit operator
capacity or scheduler settings by itself.

## Congestion Fairness

`scheduler.congestion_policy` lets Python compile self-limiting behavior from
path pressure without adding Rust policy:

- `off`: do not infer pacing; useful for benchmarks that need a raw primitive
  baseline
- `conservative`: reduce sender budget earlier on shared links
- `adaptive`: normal default; back off only when queue/loss/send-failure
  pressure is visible
- `volatile`: stronger backoff for wireless or bufferbloaty links

The compiled primitive is currently `pacing_budget_bps`. If the value is zero,
Rust bypasses pacing entirely. If config already set a per-path
`pacing_budget_bps`, that explicit operator value wins over inferred fairness.
The policy reads normalized path pressure such as loss ppm, oldest queue age,
queue depth, send failures, and local drops. It does not add reliability,
retransmission, or payload semantics.

Live reapply uses a Python-owned congestion fairness controller to avoid
flapping. New pressure can apply quickly, but recovery requires repeated cleaner
windows before Python removes an inferred pacing budget. This keeps a shared or
bufferbloaty path from oscillating between unrestricted and restricted sender
budgets because of one lucky sample. Rust is not aware of the hysteresis model;
it still receives either `pacing_budget_bps = 0` for bypass or a compiled byte
rate primitive.

## Service Outcome Feedback

Service-level QoS now has an optional Python-only outcome signal. Helpers,
benchmark tooling, or future operator integrations may report that a protected
service outcome degraded, such as TCP retransmits rising for a stable
WireGuard profile or application delivery falling below a test gate. That
signal is deliberately semantic and therefore stays out of Rust.

The current service-budget controller uses the signal conservatively:

- degraded `high` or `critical` service outcomes block more aggressive bulk
  byte-budget tightening
- if a bulk service had already been tightened, repeated degraded protected
  outcomes loosen the cap back toward the known-good baseline
- degraded `bulk` service outcomes do not block protection of higher-priority
  services
- when no outcome signal exists, the controller keeps using service counters
  and the sticky hysteresis rules documented in the v0.9.2 roadmap

This is not a Rust scheduler mode. Rust still only receives packet and byte
budget primitives. Any future retransmit, UDP-loss, helper-health, or
application-gate interpretation belongs in Python and should be converted into
one of these outcome facts before compiling Rust primitives.

The Hyper-V dual-WireGuard benchmark runner already emits this DTO shape in its
report JSON after each run. That is post-run evidence, so it documents and
validates the policy input but does not steer the completed run. A future live
helper feed should use the same DTO shape while traffic is active, then the
controller can react without changing Rust.

The Hyper-V dual-WireGuard benchmark runner follows that shape for its manual
split profile: the stable/TCP service defaults to the highest hinted-capacity
active path, while the fast/UDP service defaults to the smallest remaining path
set that can cover the configured UDP target with headroom. It also exposes
explicit `--stable-paths`, `--fast-paths`, `--fast-path-headroom`,
`--stable-path-policy`, and `--fast-path-policy` overrides so tests can prove
that service-level choices are real runtime primitives rather than lab-only
assumptions.

`round_robin` remains intentionally simple:

- paths are visited in the order Python compiled into runtime config
- each datagram advances the next-path cursor
- Rust still applies MTU eligibility before emitting a frame
- path `enabled`, `state`, `weight`, MTU, and primitive limits are compiled by Python
- `active` MTU-eligible paths are preferred for whole packets
- if no path can carry the datagram whole, Rust may use a non-busy path for
  fragmentation
- `busy`, `drain`, and `disabled` are inputs from Python, not Rust policy
- `weight` repeats a path in the simple round-robin sequence

This gives the dataplane real scheduling modes without moving adaptive policy
into Rust.

`ordered_multipath` is the first policy intended for one ordered UDP service
flow, such as WireGuard-over-Gatherlink. It is inspired by Multipath TCP's split
between connection-level data sequencing and per-subflow delivery. Gatherlink
already has a global per-session/service `sequence` in the encrypted logical
frame, so this mode does not add packet-header bytes. Python selects the policy
and compiles the path facts; Rust only keeps per-path virtual availability and
the last predicted service-flow arrival so it can avoid sending the next
sequence number on a path that is likely to arrive far before the previous one.
That makes it different from simple ECF: ECF ranks a packet, while
`ordered_multipath` remembers enough cheap execution state to make consecutive
sequence choices coherent. Predicted early arrival receives a large
head-of-line penalty only when it falls outside the compiled receiver reorder
window. Inside that window, Rust does not add a soft timing penalty, because
using the configured reorder budget is cheaper than sleeping in the packet path.
Separately, Python may compile an explicit `pacing_budget_bps`; when that value
is zero Rust bypasses pacing entirely, and when it is non-zero Rust applies only
a tiny bounded sleep while maintaining the virtual path timeline.

The current implementation also accepts Python-compiled in-flight credits:

- `max_in_flight_packets`
- `max_in_flight_bytes`
- `reorder_hold_us`
- `pacing_budget_bps`

Python may derive those values from configured capacity, measured latency,
receiver reorder pressure, local drops, receiver-advertised directional
capacity, and path-delivery telemetry. Ordered credits use the narrower known
TX/RX capacity so a sender does not build work faster than the receiver says
the path can absorb. Receiver pressure is both absolute and ratio-based:
lifetime counters matter, but a small recent sample with a high gap/drop ratio
still tightens the compiled credit. Ordered policy also treats receiver
reorder-buffer depth and oldest buffered age as pressure, and uses p95 latency
when available so the virtual arrival timeline is not built from an optimistic
mean alone.

Rust only maintains the tiny virtual path timeline and applies the compiled
limits while choosing a path. If one eligible path is already over its
Python-compiled packet or byte credit and another path is still under credit,
Rust chooses from the under-credit set. If every path is over credit, Rust falls
back to the least-bad ordered choice rather than deadlocking UDP forwarding.
Rust also reports the current in-flight packet count, in-flight bytes, and
zero-payload predicted delivery delay for each path. Those are execution facts,
not policy. Python uses them with latency and queue facts to estimate earliest
useful delivery for the next payload.
When live latency is unknown, Python compiles an explicit ordered latency
fallback from the reorder hold so Rust's virtual timeline and Python's
in-flight credit model stay on the same timing basis. This is intentionally
MPTCP-inspired but not TCP: Gatherlink does not retransmit ordinary UDP payloads
and does not promise reliable delivery for user traffic.

`ordered_multipath_capacity_aware` deliberately keeps a separate Python policy
name while compiling to the same Rust ordered primitive. That lets benchmark
tables compare the original ordered virtual-timeline behavior with a
capacity-weighted version using the same pressure feedback as
`capacity_aware`.

`arrival_guarded_capacity` is the Python-only middle ground between plain
capacity weighting and full ordered multipath. Python estimates each path's
arrival time from compiled latency, capacity, and queue facts. If a slower path
would probably land outside the receiver reorder budget, Python marks that path
as `drain` with a tiny probe weight before Rust receives runtime state. When no
path looks unsafe, it compiles exactly like `capacity_aware`, so the hot Rust
path stays on the existing weighted scheduler and there is no new dataplane
knob to bypass.

## Feedback-Driven UDP Scheduling

The active v0.9.3 direction is to make path selection increasingly
feedback-driven while preserving UDP semantics and the Python/Rust boundary.

External scheduler references point to the same shape. MPTCP separates
connection-level ordering from per-subflow delivery, keeps receiver feedback
and send buffers at the connection level, and treats retransmission/reinjection
as policy around that model rather than blind striping. Linux MPTCP exposes a
path manager plus a packet scheduler: schedulers can aggregate capacity, prefer
low latency, fail over, or reinject on other paths. WireGuard's public
performance notes emphasize the opposite pressure on the hot path: lock-free
queues, batching/offload-like behavior such as GRO, CPU locality, and avoiding
unnecessary queue churn. Gatherlink therefore should not make Rust "smarter" in
the semantic sense; it should give Rust better compiled primitives and cheaper
packet execution while Python owns the reasoning.

Python should compute:

- per-path health: `alive`, `degraded`, or `down`
- path credit/window values from bandwidth, latency, jitter, drops, queue age,
  reorder depth, and confidence
- hot-reapply cadence, hysteresis, and provenance rules so flapping telemetry
  cannot destabilize runtime path state
- path pacing budgets
- service-specific scheduler choice such as `flowlet_adaptive`,
  `ordered_multipath`, `arrival_guarded_capacity`,
  `latency_guarded_capacity`, or a simpler fallback
- path suppression and recovery ramp-up
- all operator-facing explanations

Rust should execute:

- compiled byte/packet credits
- compiled pacing budgets
- compiled path state and weights
- cheap queue, drop, reorder, and delivery counters
- the ordered multipath virtual timeline
- batching, fragmentation, dedupe, and socket I/O

Rust must not derive business meaning from service payloads, decide helper
behavior, infer user intent, or add hidden reliability. If Gatherlink needs
reliable delivery for its own metadata, that must be a reserved/internal service
with explicit ack/retry semantics handled by Python control logic. It must not
change ordinary UDP payload behavior.

Remote receiver metrics do help the local scheduler, but only in the direction
that the local node is sending. A local TX scheduler should merge its own
sender-side pressure with the peer's receiver-side pressure for the same logical
path. The merge must be pessimistic: local counters must not erase peer-reported
loss, gaps, queue pressure, or reorder pressure just because the local socket
was quiet during the same status window. The opposite direction has its own
scheduler loop on the peer. That matters for WireGuard-over-Gatherlink because
the "ACK packets" are encrypted UDP payloads from WireGuard's point of view;
Gatherlink should not inspect them as TCP ACKs. It can only schedule the
reverse UDP datagrams using the reverse service flow's own local TX facts and
remote RX feedback.

Peer-advertised service scheduler primitives are a narrower exception: they are
shared so the receiver can classify expected fanout duplicates and count failed
expected copies correctly. Those primitives are not the peer's path scheduler
mode and should not be used as evidence that both directions are running the
same policy.

The remote-status reserved service is the first concrete internal-metadata
acknowledgement surface. Python sends short-lived read-only requests with
request ids, tracks pending ids, treats matching responses as acknowledgements,
and counts timed-out requests locally. That accounting helps operators
understand remote monitor freshness without teaching the normal UDP transport
to retransmit application payloads.

### Planned Policy Modes

`flowlet_adaptive` should be the first WireGuard-over-Gatherlink friendly
default candidate. It keeps one service/source flow on a path while packets are
close together, then moves only after an idle gap, bounded hold time, or
sustained path pressure. This avoids blind packet striping for order-sensitive
encapsulated UDP.

`latency_guarded_capacity` should prefer capacity only among paths inside a
Python-selected latency and jitter envelope. Python drains clear outliers, then
compiles the remaining capacity estimates into ordinary weighted execution. It
exists for cases where the fastest path by bandwidth would create enough reorder
pressure to reduce useful throughput.

`arrival_guarded_capacity` is stricter and more queue-aware than the simple
latency guard. It checks predicted arrival against the compiled reorder budget,
so a high-capacity path can still be used if it is slow but safely inside the
receiver window, while a slightly slower queued path can be demoted when it is
likely to create head-of-line blocking. `coordinated_adaptive` may choose this
mode when capacity hints are present and latency spread combines with queue
pressure.

Capacity-oriented policies do not treat configured speed as permanently true:
Python prefers live capacity estimates over startup hints when those facts are
available. Basic `capacity_aware` keeps capacity share stable even when WAN
facsimiles intentionally include loss, because drop counters alone do not prove
the configured capacity ratio is wrong. Pressure facts still feed ordered
credits, adaptive policies, health diagnostics, and future policies that are
explicitly designed to react to overload.

Python also computes an explicit capacity-confidence fact for diagnostics and
coordinator decisions. That value explains whether the current capacity estimate
looks trustworthy without secretly changing the `capacity_aware` split. This is
important in lab and real WAN tests: sustained qdisc drops can mean "the test is
above link capacity" rather than "the configured path ratio is wrong."

`ordered_multipath` remains the more ambitious single-flow striping mode.
`ordered_multipath_capacity_aware` exists for direct A/B testing without
changing that baseline. Neither should become a default for order-sensitive
services until VM evidence shows that live credits and pacing can exceed the
clean one-path ceiling without causing excessive reorder buffering.

`coordinated_adaptive` is the first Python-level scheduler coordinator. It is
not a Rust mode. It classifies telemetry into a concrete policy and then uses
the same compiler path as any explicitly configured scheduler. Its fallback is
derived from configured and observed path facts: skewed path speeds prefer
`capacity_aware`, visible latency spread with queue pressure may prefer
`arrival_guarded_capacity`, other visible latency spread prefers
`latency_guarded_capacity`, and otherwise it uses the conservative adaptive
primitive.

`scheduler.traffic_bias` is an optional Python-only hint for this coordinator.
The default is `auto`. `tcp` tells Python the service profile is expected to
carry order-sensitive opaque UDP, such as WireGuard with TCP inside it. In that
mode the coordinator avoids promoting the more experimental ordered multipath
policy on reorder pressure alone; it prefers latency/capacity guards first and
starts from `single_best_path` and continues to use that candidate for stable
TCP-heavy tunnel conditions where the latest VM evidence shows one clean path
can outperform blind striping. It uses flowlet behavior only when jitter plus
latency spread make stickiness the safer choice. `udp` biases toward capacity
aggregation for ordinary UDP-like traffic. This hint never reaches Rust as
meaning: Rust still receives only the compiled primitive mode, path state,
weights, and limits.

Services may also carry a narrow `scheduler_path_policy` primitive. This is not
a second policy engine. Python uses it when service intent is already explicit,
for example dual WireGuard where the stable/default service should stay on the
single best path while the fast service inherits an aggregation-friendly
node-wide policy. Rust only executes `inherit`, `single_best_path`, or
`weighted_round_robin` against the already compiled path table; it does not
inspect payloads or decide whether a service is TCP-like or UDP-like.

The current coordinator records compact decision signals such as
`capacity_hints`, `capacity_confident`, `latency_spread`, `jitter_pressure`,
`reorder_pressure`, `queue_pressure`, and `loss_pressure`. Those signals are
bounded control-plane facts, not per-packet logs. Jitter pressure can steer the
coordinator toward `flowlet_adaptive`; latency spread plus queue pressure can
steer it toward `arrival_guarded_capacity`; ordered multipath uses jitter as
part of its Python-compiled reorder budget.

Flapping protection is packet-volume based first, with a short wall-clock guard. A
scheduler switch is justified by traffic evidence: a quiet service should not
change mode only because time passed, while a busy service can accumulate enough
evidence quickly. The secondary time guard is intentionally single-digit seconds
so one large burst cannot force multiple same-moment switches. Diagnostics
expose the latest decision and a small recent history; they must never become
per-packet logs.

### Telemetry Needed

Scheduler telemetry should reuse structured status and diagnostics rather than
creating a lab-only vocabulary:

- local TX/RX packet and byte counters per path
- summarized Rust-edge TX/RX timing facts suitable for scheduler use, without
  per-packet Python callbacks; the current runtime shape exposes last TX/RX
  timestamps and last TX/RX inter-packet gaps as aggregate service/path status
  facts
- queue depth in packets and bytes
- oldest queued packet age
- send failures and local drops
- receiver missing sequence facts and reorder-buffer depth
- current, mean, and jitter/variance latency estimates
- stale-control metadata age
- ordinary path telemetry for relayed paths; relay runners expose diagnostics
  and counters, but endpoint schedulers should not treat a relay as a special
  policy dimension unless future evidence proves that abstraction is needed
- MTU and carrier max-datagram facts

Python should treat telemetry as suspect until it has confidence. Missing,
stale, negative, impossible, or flapping metrics must fall back to deterministic
safe behavior and emit diagnostics instead of destabilizing the dataplane.
Control metadata is also bounded by the smallest active path MTU. When a busy
service produces more real-data timing samples than fit, Python advertises a
small fitted batch and omits the rest rather than asking Rust to emit an
oversized control frame.

### Clock-Safe One-Way Latency

One-way latency is scheduler-critical and must not be treated as precise just
because a number exists. The current traffic-derived fallback of `reply-rtt-half`
is safe as a coarse bootstrap value, but it is not accurate enough to drive
ordered multipath, latency guards, or ECF/BLEST-style decisions by itself.

Python should maintain a peer clock-quality model before compiling directional
latency into Rust primitives:

- use NTP-style four-timestamp exchanges on Gatherlink control metadata to
  estimate offset, RTT, clock jitter, and dispersion/error
- keep per-path samples instead of one global offset-only view, because
  asymmetric paths can make one-way delay estimates look plausible while still
  being wrong
- mark every latency sample with source and confidence, for example
  `reply-rtt-half`, `clock-synced-one-way`, or `rejected`
- reject or down-rank one-way samples that violate RTT sanity: negative delay,
  one direction greater than RTT plus clock-error budget, the sum of directional
  estimates far outside current RTT, or abrupt offset changes without enough
  fresh samples
- prefer minimum-delay/low-jitter offset samples for clock discipline, then
  use rolling median/p95/jitter windows for scheduler latency rather than a raw
  mean alone
- expose clock quality to diagnostics and service monitor so operators can see
  when latency-sensitive schedulers are using coarse RTT-derived data

Current v0.9.2 code implements the first production control-plane slice of this
model: non-sink nodes send four-timestamp sync requests on the exact path being
measured, sinks answer each request on that same path, impossible exchanges are
rejected, abrupt offset outliers are ignored after a baseline exists, and
offsets are summarized with a robust median. Accepted samples derive directional
TX/RX one-way estimates from the selected peer clock offset instead of blindly
splitting RTT in half. Directional latency samples are still rejected when their
sum is impossible against current RTT plus the clock-error budget, and every
sample remains tagged with source/confidence/rejection facts for scheduler and
monitor use. `reply-rtt-half` remains visible as a coarse traffic fallback
rather than being treated as precise one-way timing.

This follows the same shape as NTP and OWAMP/IPPM practice: offset, round-trip
delay, jitter, and error/confidence are separate facts, and one-way-delay
metrics are only meaningful when clock synchronization quality is known. Python
owns that interpretation. Rust should receive only the compact `latency_us`,
`reorder_hold_us`, credit, and pacing primitives that Python decides are safe.

Service monitor output should be derived from the same facts. Per-path views
should show rate, credit/window, queue pressure, latency, jitter, reorder depth,
drops, and whether the current limit appears to be scheduler, transport, or
application pressure when the data is available.

Service redundancy is a Python-owned policy that compiles to a tiny Rust fanout
primitive. Rust receives `fanout` and `fanout_below_bytes`: `fanout=1` means one
scheduled path, `fanout=0` means every eligible path, and values above one mean
that many eligible paths. `fanout_below_bytes=0` makes fanout apply to every
payload; otherwise larger payloads fall back to one scheduled path. Python can
therefore expose modes like `duplicate`, `duplicate_small`, or control metadata
all-path delivery without Rust learning those policy names.

## Primitive Contract

Python may use user config, lab qdisc counters, peer control metadata, service
priority, path history, and future helper facts to decide what each path should
do. Rust receives only cheap execution primitives:

- `tx_capacity_bps` and `rx_capacity_bps` for directional bandwidth estimates
- `latency_us` for the compact latency value used by packet-time selection
- `tx_p95_us`/`rx_p95_us` and directional jitter remain Python telemetry facts
  for scheduler selection and diagnostics; Rust only receives the compact
  latency primitive that Python chooses from those facts
- `loss_ppm` for smoothed loss in parts per million
- `reorder_hold_us` for path-specific reorder timing selected by Python
- `max_in_flight_packets` and `max_in_flight_bytes` for bounded pressure limits
- `pacing_budget_bps` for Python-selected sender pacing, where zero means bypassed
- `queue_depth_packets`, `queue_depth_bytes`, and `queue_oldest_age_us` for queue-aware path selection
- `enabled`, `state`, `weight`, and `mtu` for direct path eligibility

Rust reports these additional raw facts back through status/control metadata:

- scheduler in-flight packets and bytes
- scheduler predicted delivery delay
- receiver reorder-buffer packets and oldest buffered age
- service/path TX and RX counters, gaps, duplicates, failures, and drops
- control-metaband TX/RX bytes, frames, and inter-frame gaps per path
- socket receive/send buffer capacity and path drain quantum

Python owns any meaning attached to those facts: earliest-delivery estimates,
head-of-line blocking pressure, ACK/control-return quality, service contention,
or buffer-pressure diagnostics.

The important boundary is that Python explains and changes these values. Rust
only follows them and reports counters back.

Any future Rust tuning knob must be disabled or zero-cost when Python does not
compile it. The existing high-speed path remains the baseline: new primitives
must not add extra packet-path work when their compiled value is unset or zero.

Live reapply smooths noisy scalar telemetry in Python before compiling these
primitives. Capacity, latency, loss, and jitter move through a small exponential
average, while immediate pressure facts such as queue depth, drops, send
failures, receive gaps, and stale-control age remain unsmoothed so fail-closed
or suppression decisions are not hidden by averaging.

Path capacity auto-detection is part of the Python path telemetry layer, not
Rust policy. At startup, each path is seeded from persisted non-authoritative
capacity cache data when present, otherwise from configured scheduler capacity
hints, otherwise from the conservative default. During runtime, Python observes
Rust path counters for both local TX and RX directions and updates the estimate
only after sustained evidence: increases require sustained higher throughput,
while decreases require sustained lower throughput plus drops. The detected
values are advertised through control metadata, shown in service monitor output,
cached for the next run, and fed into the live scheduler reapply loop. Rust only
receives the resulting compiled capacity primitive.

Python may choose a per-path capacity responsiveness policy. Stable wired paths
should keep conservative sustained-evidence updates so transient application
traffic does not rewrite a good configured capacity. Volatile paths such as
Starlink, LTE, or 5G may opt into faster capacity decrease detection and faster
queue-pressure reaction, while still requiring confidence before increasing the
estimate again. The policy is a scheduler/control-plane decision, not a Rust
mode.

The intended knobs are small and path-local:

- `conservative`: slow increases and decreases; default for stable configured
  wired paths
- `adaptive`: normal live detection using sustained evidence and pressure facts
- `volatile`: faster decreases, faster pacing reduction, and cautious recovery
  ramp for Starlink/mobile-style paths

`volatile` must not mean trusting every short spike. It should react quickly to
queue growth, drops, latency-under-load, or clear capacity collapse, then probe
back gradually. Diagnostics should show when a path used fast decrease,
queue-pressure pacing, or recovery ramp so operators can tell the difference
between real Starlink/mobile volatility and a bad benchmark shape.

Future v0.9.3 and post-v0.9.3 work may let Python infer a per-path network
profile to select the responsiveness policy. This inference must be
confidence-scored and slow enough to avoid chasing short-lived noise. Fixed
operator config wins; lab profile metadata comes next; helper-provided hints
such as Starlink stats, modem/router state, radio technology, or signal quality
can strengthen the inference; longer window traffic behavior can classify
unknown paths; weak network identity hints such as ASN or reverse DNS are
advisory only.

The inference result should be names like `wired_stable`, `cellular_like`,
`starlink_like`, `satellite_like`, `volatile_wireless`, or `unknown`. It should
tune responsiveness, pacing margin, queue weighting, and recovery ramp. It must
not disable other automation, override fixed config, or add packet-format
meaning. Short benchmark profiles may provide their intended profile directly
so the test does not have to run long enough for behavioral inference to become
confident.

MTU policy follows the same boundary. Python observes interface MTU, carrier max
datagram size, and explicit too-large or fragmentation-failed counters, then
may compile a lower path MTU. Generic loss or congestion is not enough by
itself to downgrade MTU because that would confuse queue pressure with packet
shape. Rust only fragments or drops according to the MTU Python supplied.

## Service Priority

Service priority belongs to configured Gatherlink services. It must not be
derived from packet inspection.

The current config accepts priority labels (`bulk`, `normal`, `high`,
`critical`) and Python compiles them into stable numeric runtime values. The
Python runner also turns those labels into a bounded service poll order: every
listening service appears at least once, while higher-priority services get a
small number of extra slots. Rust drains the supplied order; it does not infer
priority from packet contents or payload-derived classification.

Mixed-service budget control also stays Python-owned. Helpers, benchmark
monitors, and operator tooling can push live service-outcome facts through the
existing process service IPC command, for example with `gatherlink services
outcome`. Those facts may say things such as "the protected WireGuard TCP
service is degraded" or "the fast UDP service is losing packets". Python turns
those facts into a `ServiceOutcomeSnapshot`, emits low-noise
`scheduler.decision` diagnostics when they change, and passes the snapshot into
the service-budget controller. Rust never learns TCP, UDP, retransmit, or
helper-specific meaning; it only executes the compiled packet/byte drain caps
Python chooses.

The IPC payload is intentionally small. Accepted shapes are:

```json
{"outcomes": [{"service": "wireguard-stable", "degraded": true, "reason": "tcp retransmits increased"}]}
```

or the compact mapping form:

```json
{"wireguard-stable": "tcp retransmits increased", "wireguard-fast": false}
```

Malformed payloads are rejected by the service IPC handler and cannot block the
dataplane/control loop.

When a protected `high` or `critical` service reports a degraded outcome while
bulk traffic is active, Python may compile both a bounded packet drain for the
protected service and a bounded bulk byte budget. This protects TCP-like
services from large self-bursts while still allowing bulk traffic to make
progress. That is still policy, not dataplane semantics: Rust only receives the
resulting packet and byte drain caps. The v0.9.2 Hyper-V probe for
TCP-over-WireGuard-over-Gatherlink uses Linux TCP retransmit counters as
benchmark-side evidence and sends only the compact service outcome payload to
the runner.

The runner status now exposes a `service_budget` block for operator visibility.
It includes whether the budget is active, the reason, packet/byte overrides,
and compact service rate samples. This status is an explanation surface for
Python policy; it is not another Rust control surface.

## Future Work

Receiver-metric-driven adaptation should be compiled by Python into explicit
runtime state before Rust executes it.

Rust should not parse user config, discover links, score carriers, explain path
choices, or own failover policy. It should execute the scheduler state, count
what happened, and report enough structured diagnostics for Python to explain
the behavior.

## Queues and UDP

Gatherlink should keep a small bounded local scheduler queue. The queue exists
to smooth short scheduler decisions and preserve FIFO ordering while the runtime
chooses among currently eligible paths. It is not a reliability mechanism for
UDP.

Normal UDP payloads remain best-effort:

- if a path is temporarily busy and another eligible path has capacity, the
  queued packet may be redistributed FIFO-style to that path
- if the queue is full or the packet age exceeds policy, Gatherlink drops the
  packet, increments explicit Gatherlink drop counters, and emits diagnostics
- Gatherlink does not retransmit ordinary UDP packets. Any ack/retry behavior
  belongs to an explicit reserved/internal service, not to the normal UDP
  transport.

This keeps real-time UDP honest. Loss recovery belongs to the application or
upper protocol that chose UDP. Gatherlink's job is to make drops, queue depth,
queue age, path latency, and receiver missing-packet facts visible enough for
Python policy to adjust path weights or disable bad paths.


## Path Health Scoring

Python owns path-health meaning. It may score scheduler telemetry into
`alive`, `degraded`, or `down` labels using capacity, latency, loss, queue
pressure, send failures, receive gaps, local drops, reorder pressure, and stale
control metadata. Rust receives only compact execution primitives such as path
state, weight, credit, pacing, and queue limits.

These health labels are operator explanations and scheduler policy input. They
are not packet-header fields and must not add reliability semantics to ordinary
UDP payloads.
