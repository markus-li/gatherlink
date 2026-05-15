# Testing Strategy

## Purpose

Gatherlink needs repeatable tests for network behavior, not only unit tests.

## Test layers

Use pure Python unit tests, Rust unit tests, protocol encode/decode tests,
integration tests with network namespaces, netem loss/jitter/reorder tests,
bootstrap/DNS tests, and long-running soak tests.

## Rust unit tests

Cover frame encode/decode, replay windows, dedupe, reorder buffer, MTU
eligibility, weighted round-robin distribution, queue overflow, and invalid
packet silent rejection.

## Python unit tests

Cover config validation, config expansion, path state transitions, carrier
discovery decisions, peer failover/failback, DNS policy matching, domain-set
matching, bootstrap candidate ordering, time quality scoring, and hook debounce.

## Integration tests

Use Linux namespaces and veth pairs. Scenarios should include two WAN paths,
fixed round-robin, one path down, path warmup, raw UDP blocked with WSS active,
MTU mismatch, receiver metrics loss, peer failover, DNS helper racing, bootstrap
via cache/direct DNS/DoH, and same-subnet distinct gateway validation.

## Netem scenarios

Use tc/netem to simulate latency, jitter, packet loss, burst loss, reorder, rate
limiting, blackhole, recovery, and flapping.

## Deterministic tests

Where possible, scheduling tests should use deterministic fake clocks and fake
metrics. Avoid tests that depend on internet availability.

## Public demo tests

The demo should prove UDP payload enters a local virtual port, packets split over
paths, the remote emits original UDP payload, tcpdump shows both paths, path
failure does not kill service, and diagnostics explain decisions.
