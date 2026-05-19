# Time Sync

Gatherlink has two separate time concerns:

- system wall time, which belongs to the host OS and its normal NTP service
- Gatherlink internal time, which is peer-relative metadata used by Python
  policy for telemetry windows, replay windows, and later crypto/session logic

Gatherlink does not step system time directly and external time discovery does
not travel through the Gatherlink connection. A sink-side process first tries to
read a direct NTP sample over UDP/123 and uses that sample as the wall-clock
source of truth for advertised Gatherlink time. The default direct NTP order is
Cloudflare, Google Public NTP, then the NTP pool. Cloudflare is preferred first
because it also supports Network Time Security for future authenticated time
work. Google is a good fallback but uses leap smear, so production policy should
avoid averaging it with non-smearing sources during leap-second windows.

If direct NTP is unavailable, the sink may fall back to HTTPS `Date` header
sampling. HTTPS is usually the most network-safe fallback because it rides over
ordinary outbound TCP/443, but it is coarse, proxy-influenced, and is reported
as `https-date`, not as NTP synchronization. If no external sample is available,
the sink falls back to the local OS wall clock and reports whether the host
appears disciplined by the platform's normal NTP service, such as
`systemd-timesyncd`, `chrony`, or `ntpd`.

In the lab, the process that is acting as the sink is treated as the
authoritative Gatherlink time source. This stays process-scoped because a node
may be a sink for one peer/session and a source for another. The sink process
periodically sends sparse control metaband messages on every active path using
all-path control duplication. Each message contains:

- sink wall-clock Unix microseconds
- sink process-internal monotonic microseconds
- observed sink-side NTP state: `synchronized`, `unsynchronized`, or `unknown`

The direct NTP query is intentionally structured data, not a formatted status
string. Python records the server, measured offset, round-trip time, stratum,
NTP-derived Unix microseconds, and local monotonic receive time. Monitor code is
responsible for choosing compact text at display time.

The forwarder records the latest sink time, when it was received locally, and
the sink's sent timestamp. The service monitor displays local system time,
Gatherlink time derived from the latest sink sample, and sink-side NTP state in
separate columns so operator-facing time status does not get buried in address
or control-context text.

The non-sink side keeps a peer-relative internal clock estimate based on the
sink. The exchange is intentionally analogous to NTP's four-timestamp model,
but it uses Gatherlink process monotonic clocks instead of changing system time:
the requester sends origin time, the sink stamps receive/transmit time, and the
requester computes offset and RTT in Python. Until richer bidirectional latency
confidence exists, sink wall-clock samples are applied with half of the current
rolling RTT as the one-way correction. That is deliberately a policy-layer
estimate: once both directions have enough samples, Python can replace it with a
confidence-weighted one-way latency model without changing the Rust packet path.

Rust needs the same effective time facts for later fast-path checks such as
crypto signature windows and replay protection. Python still owns the policy:
it decides the sink-authoritative clock source and pushes the resulting compact
time facts down to Rust; Rust should only execute cheap comparisons in the
packet path.

## Time Helper Priority

The time helper is an active helper priority, but only for the privileged act of
setting system time from Gatherlink-derived time. Core Gatherlink must continue
to maintain internal time quality without requiring system clock privileges.

The time helper may set system time only when explicitly enabled. It must warn
that system time is normally managed by an NTP agent such as chrony,
systemd-timesyncd, ntpd, or an appliance time service. Operators should not use
the Gatherlink time helper if such an agent is active.

First scope:

- explicit opt-in system time correction
- narrow privileged helper boundary
- diagnostics showing source quality and applied correction
- no automatic enablement

Implemented command shape:

- run the privileged helper separately, for example
  `gatherlink-time-helper --socket /run/gatherlink/time-helper.sock`
- inspect the time source Gatherlink would advertise with `gatherlink time status`
- preview a correction with
  `gatherlink time correct <target-unix-us> --socket /run/gatherlink/time-helper.sock`
- apply a bounded correction only with the explicit `--apply` flag

The helper protocol is deliberately small and local: Python sends structured
policy facts as `target_unix_us`, `source`, `quality`, `max_step_us`, and
`apply`. The helper refuses corrections larger than `max_step_us`, previews by
default, and relies on the OS to enforce the privilege required to set
`CLOCK_REALTIME`.

Library posture:

- prefer OS APIs or narrow platform commands over adding a Python dependency
- keep privilege handling explicit and isolated in the helper
- do not hide NTP-agent detection behind a broad framework
