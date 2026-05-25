# External Aggregation Speed Notes

Last checked: 2026-05-24.

This note collects public speed claims and user reports for WAN aggregation
systems so Gatherlink benchmark profiles can compare against something close to
the outside world. Treat vendor claims as useful sizing hints, not truth. Treat
forum and community reports as noisy field evidence unless they include link
shape, endpoint, hardware, and test method.

## Reading Rules

- Vendor maximum throughput is usually a product, license, relay, or hardware
  ceiling, not a field result.
- Starlink and cellular reports must be assumed variable unless the report
  includes time of day, signal, packet loss, RTT, jitter, and ground/relay
  region.
- Single speed-test results are not enough. Prefer repeated TCP and UDP tests,
  direct per-link baselines, bonded tunnel tests, and host CPU observations.
- Packet-level bonding across unequal-latency paths should be expected to lose
  efficiency unless the product has a good scheduler, selective bonding,
  buffering, duplication, or FEC behavior.
- Compare Gatherlink against three gates:
  - raw Gatherlink transport gate: how much of our own raw UDP ceiling remains
  - WireGuard path-set gate: how much WG-over-GL keeps versus direct userspace
    WireGuard over the same path set
  - vendor/product gate: how much of the outside product's reported or likely
    field outcome we can match under similar link shape

## Collected External Signals

| Technology | Source type | Public speed signal | Likely link shape | Confidence | Notes |
| --- | --- | --- | --- | --- | --- |
| Speedify public servers | vendor docs | Public servers usually 200-300 Mbps; dedicated servers up to 1 Gbps; enterprise above 1 Gbps by arrangement. | Mixed consumer links through hosted VPN relay. Link latency not specified. | medium for service ceiling, low for field aggregation | Useful as a practical hosted-relay ceiling. Does not prove Starlink+5G aggregation on a specific path shape. Source: https://support.speedify.com/article/689-max-speed |
| Peplink SpeedFusion | vendor whitepaper | Bonding combines WAN links into one logical VPN; SpeedFusion adds 80 bytes per packet and uses smoothing/FEC options. Peplink recommends WAN links with bandwidth within 50% and latency within 150 ms for best performance. | Enterprise branch/mobile/field WANs. Explicitly warns about latency mismatch and high-latency loss. | high for design constraints, medium for overhead | Very relevant to Gatherlink. It validates our concern that high-latency or mismatched links are the hard case. Source: https://download.peplink.com/resources/whitepaper-speedfusion-and-best-practices-2019.pdf |
| Peplink device throughput | vendor datasheet | SpeedFusion throughput ranges from tens of Mbps on small devices to 2 Gbps on large Balance 2500-class devices; figures depend on Ethernet frame sizes and environment. | Hardware-bound appliance throughput, not link-shape proof. | medium | Useful reminder that many real products are hardware/CPU/license capped before links are. Source: https://www.peplink.com/wp-content/uploads/2020/05/peplink_balance_datasheet.pdf |
| Peplink B One 5G / SpeedFusion Cloud class | third-party review/vendor-derived | Review data places encrypted SpeedFusion on B One 5G-class hardware around a 200 Mbps hard cap, with realistic working throughput around 150 Mbps. | Prosumer/home-office appliance to hosted relay. | medium | Good small-site comparator: Gatherlink should beat this on clean wired profiles and be judged against it on mixed 5G/Starlink profiles. Sources: https://www.waveform.com/a/b/guides/bonding-platform-review and https://www.waveform.com/a/b/guides/multi-wan-guide |
| Peplink 5x Starlink report | community forum | 5 Starlinks at about 250 Mbps each produced about 300 Mbps through one SDX hub path, but about 650 Mbps to cloud FusionHub in AWS/GCP Chile. | Five Starlink links, likely high jitter and shared satellite/ground path effects; relay location and hub path mattered a lot. | medium-low | Very useful field shape: many fast satellite paths do not automatically sum; cloud relay placement can matter more than raw dish count. Source: https://forum.peplink.com/t/bonding-of-5-starlink-with-balance-sdx/56160 |
| Peplink Starlink plus cellular tuning report | community forum | Reported inputs were Starlink at 50-250 Mbps down, 2-15 Mbps up, about 65 ms SpeedFusion RTT, plus cellular at 10-30 Mbps down, 30-50 Mbps up, about 75 ms RTT. Peplink support guidance in the thread says bonding benefit is weak when bandwidths are not within about 40%. | Starlink plus cellular, similar RTT but very different bandwidth and upload/downlink asymmetry. | medium for shape, low for final throughput | This is close to the field shape Gatherlink cares about: useful aggregation may mean preserving Starlink download while using cellular upload/failover, not perfect sum. Source: https://forum.peplink.com/t/speedfusion-dynamic-weighted-balance-starlink-cellular/41070 |
| Peplink Starlink plus low-bandwidth link report | community forum | A user reports about 500 Mbps PepVPN on a 310-5G with gigabit input, but far worse performance when a low-bandwidth Starlink is added to the bond. | Strong wired link plus lower-quality Starlink, likely asymmetric latency/loss/capacity. | low-medium | Matches our current WG-over-GL problem: adding a bad path can reduce ordered-flow performance. Source: https://forum.peplink.com/t/speedfusion-bandwidth-bonding-does-it-actually-work/42453 |
| Speedify independent review, fast relay case | third-party review | One review reported Speedify consistently at least 250 Mbps and often over 300 Mbps on a US connection, while noting limited ping/loading visibility. | Hosted consumer VPN relay; not necessarily bonded mixed links. | medium for relay speed, low for bonding lift | Useful as a public-relay speed bar: clean Gatherlink profiles should beat 300 Mbps if the local hardware and path allow it. Source: https://www.tomsguide.com/reviews/speedify-review |
| Speedify independent review, weak bonding case | third-party review | Another review reported 50-60 Mbps download in UK tests and no concrete performance improvement from bonding Wi-Fi plus tethered mobile. | Consumer Wi-Fi plus mobile through hosted VPN relay. | low-medium | Useful negative case: a branded bonding product can fail to improve throughput in ordinary mixed consumer conditions. Source: https://www.techradar.com/reviews/speedify |
| OpenMPTCProuter review | third-party review | Review claims 200+ Mbps on x86 and 100-150 Mbps on Raspberry Pi, with strong TCP bonding but weak real-time/UDP behavior. | MPTCP to self-hosted VPS. Usually TCP-heavy workloads. | medium | Good comparator for TCP-only aggregation, poor comparator for UDP/WireGuard-like traffic. Source: https://www.waveform.com/a/b/guides/bonding-platform-review |
| OpenMPTCProuter issue | user bug report | Two LTE links averaging about 80 Mbps each were expected to reach about 160 Mbps, but bonded result was about 80 Mbps. | Two LTE links, likely cellular contention and/or config/VPS/kernel limits. | low-medium | Useful negative case: same-medium links can still fail to aggregate when the stack or provider behavior is wrong. Source: https://github.com/Ysurac/openmptcprouter/issues/2495 |
| Bondix licenses | vendor docs | License tiers list throughput ceilings from 20 Mbps to 500 Mbps, plus individual higher-throughput options. | License/hardware ceiling, not field result. | medium for product ceiling, low for field outcome | Useful to compare commercial expectations: 100-500 Mbps is a normal sold range for small/field bonding. Source: https://www.bondix.de/licenses |
| Bondix presets | vendor wiki | Bonding preset optimizes bandwidth; packet duplication decreases bandwidth; satellite preset is mandatory for satellite but explicitly says it does not apply to Starlink. TCP mode can increase throughput at cost of latency. | Heterogeneous WANs with preset-specific behavior. | medium | Confirms that products expose different modes for speed, duplication, satellite, and TCP fallback. Sources: https://wiki.bondix.dev/wiki/Tunnel_Preset and https://wiki.bondix.dev/wiki/Presets |
| Mushroom Truffle | vendor docs/review | Current brochure material lists Truffle at 1 Gbps load-balancing throughput, 450 Mbps standalone bonding, and 250 Mbps peered bonding; third-party review says high-end Truffle EX can route up to 10 Gbps aggregate. | Business fixed-line bonding appliance. | medium for product ceiling, low for field outcome | Good reminder to separate load balancing, standalone bonding, and peered bonding. Gatherlink should compare mostly to the peered number, not headline routing throughput. Sources: https://www.mushroomnetworks.com/brochure-truffle/ and https://www.waveform.com/a/b/guides/bonding-platform-review |
| Viprinet | vendor docs/review | Vendor claims bonding of DSL/cable/mobile/satellite into one line; third-party review lists older hardware in 100-400 Mbps class. | Enterprise hardware hub/router model. | low-medium | Useful as historical enterprise bonding comparator, but public field details are thin. Sources: https://www.viprinet.com/en/technology/how-viprinet-works and https://www.waveform.com/a/b/guides/bonding-platform-review |
| Viprinet appliance capacities | vendor datasheets | Multichannel VPN Router 1610 lists 125 Mbps bonding capacity; product folder examples list 100 Mbps class mobile routers and 400 Mbps class larger routers. | Hardware appliance ceilings across older enterprise product tiers. | medium | Useful historical bar: many dedicated bonding appliances are not automatically gigabit-class once true bonded VPN capacity is counted. Sources: https://www.viprinet.com/sites/default/files/files/viprinet_multichannel_vpn_router_1610_eng.pdf and https://www.viprinet.com/sites/default/files/files/viprinet_product_folder_en_web_1.pdf |

## What The Link Types Probably Mean

### Clean Fiber Or Ethernet-Like Paths

Expected characteristics:

- RTT: 1-20 ms to relay in lab or metro cloud
- jitter: usually under 2 ms
- loss: effectively zero
- capacity: stable and often CPU-bound before link-bound

External expectation:

- Most products should do well.
- Hardware and relay caps dominate.
- If Gatherlink cannot keep a high `GL Gate` here, the issue is our hot path,
  socket pacing, crypto/replay cost, or benchmark setup.

### Fiber Plus 5G

Expected characteristics:

- fiber RTT: 5-20 ms
- 5G RTT: 25-70 ms
- jitter: 5G can swing 5-40 ms
- loss: usually low but bursty under load
- capacity: fiber much larger and more stable than 5G

External expectation:

- SpeedFusion-style products recommend similar bandwidth and latency for best
  bonding, so this is already an uneven-path challenge.
- Selective bonding, TCP bias, or class split matters more than blind striping.
- Gatherlink should compare against passable usefulness, not perfect sum.

### Starlink Plus 5G

Expected characteristics:

- Starlink RTT: often 30-80 ms, with handoff spikes
- 5G RTT: often 25-90 ms depending on radio and backhaul
- jitter: material on both links
- loss: usually bursty, not constant
- capacity: volatile; speed tests can vary sharply by time and relay region

External expectation:

- Field reports show this can work, but not reliably as additive bandwidth.
- High-BDP TCP needs enough streams, larger windows, and careful path choice.
- UDP may look better than TCP if the application can tolerate reordering/loss.

### Multiple Starlinks

Expected characteristics:

- similar nominal medium, but not necessarily independent congestion domains
- shared satellite/ground-region behavior may correlate latency and loss
- relay/cloud region can dominate outcome

External expectation:

- User reports show five dishes can produce only about 300 Mbps in one setup,
  or about 650 Mbps with a better cloud FusionHub path.
- Simulation must include correlated jitter/loss and relay-region effects, not
  just five independent clean 250 Mbps links.

### Two Or More Cellular Links

Expected characteristics:

- RTT: 30-100 ms
- jitter: 10-60 ms under contention
- loss: bursty
- capacity: often time-sliced and tower/backhaul-limited

External expectation:

- Same-medium links can aggregate well when independent and stable, but field
  reports also show complete failure to exceed one link.
- Tests must model both independent carriers and same-tower correlated
  contention.

## Proposed Gatherlink Simulation Profiles

These profiles should be added as benchmark shapes before claiming apples-ish
comparison against SpeedFusion, Speedify, Bondix, OpenMPTCProuter, or similar.

| Profile | Paths | Shape | What it compares to | Required result columns |
| --- | --- | --- | --- | --- |
| `external-clean-dual-gig` | 2 paths | 1000/1000 Mbps, 5/7 ms RTT, under 1 ms jitter, 0% loss | product/hardware ceiling; SpeedFusion enterprise or Bondix high-tier clean lab | raw GL, WG-over-GL, direct userspace WG path-set, GL Gate, WG Gate, CPU |
| `external-fiber-5g-asymmetric` | 2 paths | 800 Mbps at 10 ms plus 150 Mbps at 45 ms, 10-25 ms jitter, 0.1-0.5% burst loss on 5G | Peplink best-practice warning case; our fiber+5G field shape | TCP-only, mixed TCP+UDP, raw UDP, single-best-path contrast, split-WG contrast |
| `external-starlink-5g-high-bdp` | 2-3 paths | 180 Mbps at 55 ms plus 120 Mbps at 45 ms plus optional 15 Mbps tail at 90 ms; jitter 15-60 ms; burst loss | Speedify/SpeedFusion/Starlink+cellular reports | p24/p48/p96 TCP rows, 100M UDP concurrent, dual-WG split, retransmits |
| `external-starlink-queue-dynamics` | 2-3 paths | Start near the Starlink+5G high-BDP shape, then add moving bottlenecks: capacity steps, latency-under-load growth, short handoff spikes, queue-drain recovery, and occasional correlated Starlink/cellular delay bursts | Starlink router/user-terminal/gateway queue behavior inferred from public measurements, without replacing the simpler Starlink+5G baseline | p24/p48/p96 TCP rows, mixed TCP+UDP, latency-under-load, queue age, pacing budget, capacity-estimate responsiveness, recovery time |
| `external-five-starlink-correlated` | 5 paths | 5x 200-250 Mbps nominal, 40-90 ms RTT, correlated 20-80 ms jitter events, shared 0.1-1% burst loss windows | Peplink 5x Starlink 300/650 Mbps report | aggregate TCP and UDP, relay-region variants, correlated-loss counters |
| `external-dual-lte-same-tower` | 2 paths | 80/80 Mbps nominal, 50-90 ms RTT, correlated capacity dips, burst loss under load | OpenMPTCProuter two-LTE negative report | one-link baseline, two-link aggregate, same-tower correlation gate |
| `external-dual-lte-independent` | 2 paths | 50/50 or 80/80 Mbps, different RTT/jitter phases, independent loss | OpenMPTCProuter/SpeedFusion positive cellular bonding expectation | compare same-tower vs independent-carrier lift |
| `external-duplication-mode` | 2-3 paths | lossy cellular/starlink profile, duplicate selected traffic across paths | SpeedFusion WAN Smoothing and Bondix packet duplication | delivered latency, loss, overhead, useful bandwidth, voice/video-like UDP |
| `external-tcp-mode-relay` | 2 paths | UDP throttled or lossy path; TCP relay fallback path | Bondix TCP mode and OpenMPTCProuter TCP-friendly behavior | throughput, added latency, head-of-line blocking, UDP penalty |

## External Target Matrix

These are not product claims for Gatherlink. They are benchmark targets chosen
from the public evidence above so v0.9.2 can tell whether a Gatherlink result
is merely working, genuinely useful, or competitive with the lower credible
field range of other aggregation products. Use the lower credible value for
messy Starlink/cellular reports and the product/relay ceiling only for clean
lab profiles.

| Profile | Product evidence to beat | Pass target | Strong target | Why this is fair |
| --- | --- | --- | --- | --- |
| `external-clean-dual-gig` | Speedify public relay 200-300 Mbps; Peplink B One 5G-class encrypted SpeedFusion about 150-200 Mbps; Mushroom peered bonding 250 Mbps | WG-over-GL TCP above 300 Mbps and raw GL comfortably above that | WG-over-GL TCP above 500 Mbps or at least `target` on `WG Gate` | Clean dual-gig should not be judged against Starlink chaos. It should beat prosumer hosted-relay and peered-appliance ceilings. |
| `external-fiber-5g-asymmetric` | Peplink field guidance says mismatched links do not add cleanly; Speedify/Bondix modes often prioritize stability over perfect sum | WG-over-GL improves or preserves the best stable single-path result while keeping useful failover | WG-over-GL reaches at least 75% of direct WireGuard path-set and shows no worse practical result than single-best-path | Perfect sum is the wrong bar. The real bar is useful extra capacity or resilience without poisoning the good path. |
| `external-starlink-5g-high-bdp` | Starlink+cellular reports show 50-250 Mbps Starlink, 10-30 Mbps cellular down, and variable 65-75 ms tunnel RTT; Speedify public relay ceiling is 200-300 Mbps | WG-over-GL reaches at least 150 Mbps on TCP-heavy rows or beats single-best-path when the cellular path is useful | WG-over-GL reaches 200-300 Mbps on enough streams without severe retransmit/reorder penalties | This should compare to realistic hosted-relay outcomes, not theoretical Starlink+5G sum. |
| `external-starlink-queue-dynamics` | Public Starlink internals are not fully known, but measurements show variable capacity, latency-under-load, handoff-like spikes, and likely active queue management in parts of the path. The current measured userspace-WireGuard TCP path-set baseline is about 232 Mbit/s, below the 275 Mbit/s nominal path-rate sum. | Result stays useful without worse-than-single-best TCP collapse, and diagnostics show why paths were drained, paced, or recovered | Scheduler reacts to queue growth and capacity drops quickly enough to avoid sustained bufferbloat while avoiding flap under brief spikes | This is the "mystery moving bottleneck" profile. It complements, but does not replace, the simpler Starlink+5G baseline. |
| `external-five-starlink-correlated` | Peplink 5x Starlink report saw about 300 Mbps to one hub and about 650 Mbps to a better cloud FusionHub path | Aggregate WG-over-GL or raw GL beats 300 Mbps under correlated jitter/loss | Aggregate result approaches 650 Mbps when the simulated relay/region path is favorable | Five fast satellite links are not five independent clean links. Both the poor and improved field outcomes matter. |
| `external-dual-lte-same-tower` | OpenMPTCProuter user report saw two about-80 Mbps LTE links produce only about 80 Mbps bonded | Result must not be worse than the best single link and should diagnose correlation clearly | Result shows meaningful lift only when the scheduler can prove the links are not collapsing together | Same-tower LTE is a negative-control profile. Not making things worse is already important. |
| `external-dual-lte-independent` | OpenMPTCProuter review claims 200+ Mbps on x86 for TCP-heavy bonding; cellular bonding products can work when links are independent | Result beats the best single link by a visible margin and stays above 75% of direct path-set baseline | Result approaches the combined stable capacity with acceptable jitter/reorder | This is the cellular case where aggregation should actually earn its keep. |
| `external-duplication-mode` | SpeedFusion WAN Smoothing and Bondix duplication trade throughput for reliability | Lower delivered loss/jitter for protected traffic, with overhead shown explicitly | Protected UDP/interactive traffic stays usable under loss where normal striping degrades | Do not score this only on Mbps. The point is survivability and latency consistency. |
| `external-tcp-mode-relay` | Bondix TCP mode and OpenMPTCProuter show TCP-friendly relays can help throughput but add latency/head-of-line risk | TCP throughput improves on hostile UDP paths with the added latency recorded | Improvement is repeatable without hiding large latency or UDP penalties | This is a future-facing comparator for relay mode, not proof that TCP is better for normal Gatherlink packets. |

## Comparison Gates To Add

For external-comparison tables, keep these gates separate:

- `GL Gate`: result versus Gatherlink raw UDP guardrail for that shape.
- `WG Gate`: WG-over-GL TCP result versus direct userspace WireGuard path-set
  TCP for that shape.
- `Vendor Gate`: result versus the external field/product target selected for
  that profile.

When comparing userspace WireGuard backends, keep backend identity explicit.
Rows should state whether the WireGuard side used kernel WireGuard,
`wireguard-go`, GotaTun, or another backend. GotaTun and `wireguard-go` should
be compared first against each other without Gatherlink, then through
WireGuard-over-Gatherlink on the same profile. Do not mix backend changes into
scheduler claims.

Use `fail`, `pass`, `target`, or `n/a` only. Document the threshold once above
the table. Do not repeat fixed percentages in every row.

Initial suggested gates:

- `pass`: at least 75% of the chosen baseline
- `target`: at least 90% of the chosen baseline
- `Vendor Gate` for Starlink/cellular community reports should use the lower
  credible reported field value, not the vendor maximum

## What This Means For Gatherlink

Gatherlink already looks credible for raw UDP and useful WG-over-GL on clean
and fiber+5G-style profiles. The external evidence says this is not surprising:
commercial products also struggle when latency, loss, and capacity diverge.

The main missing comparison work is not a new packet format. It is a benchmark
matrix that includes correlated satellite/cellular behavior, direct
WireGuard-path-set baselines, and an explicit external field target. The most
important next profiles are:

1. `external-five-starlink-correlated`
2. `external-starlink-5g-high-bdp`
3. `external-starlink-queue-dynamics`
4. `external-dual-lte-same-tower`
5. `external-fiber-5g-asymmetric`

If Gatherlink can pass `GL Gate` and get close to the lower credible external
field targets on those profiles, it is honest to describe the result as useful
for real-world small-site aggregation. If it also passes `WG Gate`, it becomes
competitive with direct tunnel aggregation rather than merely useful.

Post-v0.9.2, a connection-profiling helper should be able to collect real path
behavior and export a lab shape that resembles it. Those generated profiles
should sit beside the fixed profiles above, not replace them. Fixed profiles
are for repeatable comparison; generated profiles are for reproducing a user's
actual Starlink, 5G, LTE, Wi-Fi, or mixed-link behavior closely enough to tune
and debug scheduler decisions.
