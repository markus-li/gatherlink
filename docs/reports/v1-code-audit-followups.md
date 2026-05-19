# V1 Code Audit Follow-ups

These findings are the reusable handoff list for the code-building chat. They
come from the full source/docs boundary audit and should be resolved with code,
tests, and any needed docs updates before calling v1 complete.

TODO comments are allowed in active code when they describe real future work.
When a TODO is resolved, remove it instead of leaving stale implementation
notes behind.

## Must Fix Before V1

No source-level audit follow-ups are currently open in this file.

## Fixed In Current Code

- DNS Gatherlink-tunnel upstream execution is implemented in the Python DNS
  helper. It sends DNS wire queries to an explicitly configured local
  Gatherlink UDP service endpoint; Rust remains unaware of DNS semantics.
- Process-managed DNS helper launches receive the shared `--diagnostics-jsonl`
  flag through the helper supervisor.
- DNS helper focused tests cover direct upstreams, tunnel upstream policy,
  actual UDP datagram exchange with a service endpoint, DoH fail-closed
  behavior, CLI parsing, diagnostics, and supervisor wiring.

## Release-Gate Verification Still Required

### DNS Tunnel Upstream VM Proof

Gatherlink-tunnel DNS upstream execution is implemented in the DNS helper and
covered by focused unit tests. Before tagging v1, prove it in the VM acceptance
environment so the release report shows DNS traffic crossing a Gatherlink UDP
service rather than only a local test double.

References:

- `docs/helpers/dns-helper.md`
- `python/gatherlink/helpers/dns/policies.py`
- `python/gatherlink/helpers/dns/resolver.py`
- `tests/python/test_dns_helper.py`

## Verification Required

Run the normal v1 gates before tagging:

- Rust unit tests
- Python unit tests
- lint checks
- relevant helper-specific tests
- full lab checks for the changed behavior
- VM acceptance checks for DNS tunnel behavior

Do not mark v1 complete until docs and source agree on the implemented behavior
and the release report records the VM DNS tunnel check.
