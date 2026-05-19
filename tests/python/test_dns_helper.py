from __future__ import annotations

import dns.flags
import dns.message
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.rrset
from gatherlink.cli.helpers import _parse_host_port
from gatherlink.cli.main import app
from gatherlink.diagnostics import DiagnosticsBus
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUpstream
from gatherlink.helpers.dns.domain_sets import normalize_qname
from typer.testing import CliRunner


def test_normalize_qname_handles_idna_and_absolute_form() -> None:
    unicode_name = normalize_qname("bücher.example")
    punycode_name = normalize_qname("xn--bcher-kva.example.")

    assert unicode_name == punycode_name
    assert unicode_name.endswith(".")


def test_dns_helper_cli_endpoint_parser_supports_ipv6() -> None:
    assert _parse_host_port("[2001:db8::1]:5353") == ("2001:db8::1", 5353)
    assert _parse_host_port("127.0.0.1:5353") == ("127.0.0.1", 5353)


def test_dns_helper_resolves_and_caches_response() -> None:
    calls = []

    def fake_upstream(query, upstream):
        calls.append(upstream.authority())
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.10"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(upstreams=[DnsUpstream(name="test", address="192.0.2.53")]),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("example.test.", dns.rdatatype.A)

    first = resolver.resolve_wire(query.to_wire())
    second = resolver.resolve_wire(query.to_wire())

    first_response = dns.message.from_wire(first.response_wire)
    second_response = dns.message.from_wire(second.response_wire)
    assert first.diagnostic.cache == "miss"
    assert second.diagnostic.cache == "hit"
    assert len(calls) == 1
    assert first_response.answer[0][0].address == "192.0.2.10"
    assert second_response.answer[0][0].address == "192.0.2.10"


def test_dns_helper_can_serve_stale_answers_after_ttl(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr("gatherlink.helpers.dns.cache.time.monotonic", lambda: now)

    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 1, "IN", "A", "192.0.2.20"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[DnsUpstream(name="test", address="192.0.2.53")],
            serve_stale_seconds=60,
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("stale.example.", dns.rdatatype.A)

    resolver.resolve_wire(query.to_wire())
    now = 102.0
    stale = resolver.resolve_wire(query.to_wire())

    assert stale.diagnostic.cache == "stale"


def test_dnssec_require_ad_refuses_unvalidated_response() -> None:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.30"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[DnsUpstream(name="test", address="192.0.2.53")],
            dnssec_mode="require_ad",
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("secure.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)

    assert response.rcode() == dns.rcode.SERVFAIL
    assert result.diagnostic.dnssec.status == "failed"
    assert result.diagnostic.error == "DNSSEC AD bit required but not present"


def test_dns_helper_emits_diagnostics_for_dnssec_rejection() -> None:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.30"))
        return response

    bus = DiagnosticsBus()
    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[DnsUpstream(name="test", address="192.0.2.53")],
            dnssec_mode="require_ad",
        ),
        upstream_resolver=fake_upstream,
        diagnostics_bus=bus,
    )
    query = dns.message.make_query("secure.example.", dns.rdatatype.A)

    resolver.resolve_wire(query.to_wire())

    assert bus.queued_events == 1
    event = bus._events[0]
    assert event.code == "dns.dnssec_bogus"
    assert event.helper == "dns"
    assert event.details["qname"] == "secure.example."


def test_dns_helper_emits_diagnostics_for_upstream_failure() -> None:
    def fake_upstream(_query, _upstream):
        raise OSError("network unreachable")

    bus = DiagnosticsBus()
    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(upstreams=[DnsUpstream(name="test", address="192.0.2.53")]),
        upstream_resolver=fake_upstream,
        diagnostics_bus=bus,
    )
    query = dns.message.make_query("broken.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())

    assert dns.message.from_wire(result.response_wire).rcode() == dns.rcode.SERVFAIL
    assert bus.queued_events == 1
    event = bus._events[0]
    assert event.code == "dns.upstream_failed"
    assert event.details["upstream"] == "direct:test@192.0.2.53:53"


def test_dnssec_require_ad_accepts_authenticated_response() -> None:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        response.flags |= dns.flags.AD
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.40"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[DnsUpstream(name="test", address="192.0.2.53")],
            dnssec_mode="require_ad",
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("secure.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)

    assert response.rcode() == dns.rcode.NOERROR
    assert result.diagnostic.dnssec.status == "validated_by_upstream"


def test_dns_helper_cli_wires_jsonl_diagnostics(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeServer:
        def __init__(self, listen, resolver):
            captured["listen"] = listen
            captured["resolver"] = resolver

        def serve_forever(self):
            captured["resolver"]._publish_event(
                code="dns.upstream_failed",
                severity="warning",
                message="DNS helper upstream failed",
                qname="example.test.",
            )

    monkeypatch.setattr("gatherlink.cli.helpers.DnsUdpServer", FakeServer)
    output = tmp_path / "dns.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "dns-serve",
            "--listen",
            "127.0.0.1:5354",
            "--diagnostics-jsonl",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert captured["listen"] == ("127.0.0.1", 5354)
    assert '"code":"dns.upstream_failed"' in output.read_text(encoding="utf-8")
