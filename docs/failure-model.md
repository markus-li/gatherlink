# Failure Model

## Purpose

Transport systems become usable when failures are predictable, observable, and
recoverable.

## Failure categories

Gatherlink should distinguish down, degraded, invalid, disabled, flapping,
recovering, overloaded, and unknown states.

## Path failure

A path may fail because an interface is down, an IP was removed, a gateway is
unreachable, a route is ambiguous, a carrier is blocked, a peer is unreachable,
an authenticated probe fails, MTU probing fails, queue age exceeds policy, or
receiver metrics show degradation.

Behavior: remove from scheduler, keep watching for recovery, emit event, run
hooks if configured, and do not crash the service.

## Carrier failure

A carrier profile may fail independently of the physical link. Example: raw UDP
blocked while WSS works. Mark the logical path down/degraded, retry according to
carrier discovery policy, and keep other logical paths on the same physical link
alive.

## Overload

Overload must not cause unbounded memory growth. Use bounded queues for paths,
diagnostics, hooks, DNS work, bootstrap attempts, carrier discovery, replay
windows, reorder buffers, metrics history, and logs.

## Stream-like carrier stall

WSS/TCP/QUIC paths may stall due to head-of-line blocking or remote backpressure.
Never block the global dataplane; queue per path only and expose queue age/depth.

## Peer failure

Do not switch peer because one carrier failed. Switch peer when the peer as a
whole is unreachable or materially worse. Failover can be aggressive; failback
should be conservative.

## Metrics failure

If receiver metrics stop arriving, fall back to local metrics, reduce confidence,
do not assume the path is good, and emit diagnostics.

## DNS/helper/time/hook failures

Optional helpers must fail independently. Core transport should keep running
where possible and expose clear status.

## Safe mode

A future safe mode should disable helpers, disable overlay routing, disable
adaptive scheduling, use conservative MTU, use the single best verified path,
and keep diagnostics available.
