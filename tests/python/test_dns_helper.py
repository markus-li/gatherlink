from __future__ import annotations

import socket
import threading

import dns.flags
import dns.message
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import pytest
from gatherlink.cli.helpers import _parse_host_port
from gatherlink.cli.main import app
from gatherlink.diagnostics import DiagnosticsBus
from gatherlink.helpers.dns import DnsHelperResolver, DnsResolverPolicy, DnsUpstream
from gatherlink.helpers.dns.domain_sets import normalize_qname
from gatherlink.helpers.dns.resolver import _doh_endpoint, query_tunnel_upstream
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


def test_dns_helper_race_first_valid_uses_first_accepted_upstream() -> None:
    calls = []

    def fake_upstream(query, upstream):
        calls.append(upstream.name)
        response = dns.message.make_response(query)
        question = query.question[0]
        if upstream.name == "bad":
            response.set_rcode(dns.rcode.SERVFAIL)
            raise OSError("bad upstream")
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.60"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            strategy="race_first_valid",
            upstreams=[
                DnsUpstream(name="bad", address="192.0.2.53"),
                DnsUpstream(name="good", address="192.0.2.54"),
            ],
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("race.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)

    assert response.answer[0][0].address == "192.0.2.60"
    assert result.diagnostic.upstream == "direct:good@192.0.2.54:53"
    assert set(calls) == {"bad", "good"}


def test_dns_helper_race_continues_after_dnssec_rejection() -> None:
    def fake_upstream(query, upstream):
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.70"))
        if upstream.name == "validated":
            response.flags |= dns.flags.AD
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            strategy="race_first_valid",
            dnssec_mode="require_ad",
            upstreams=[
                DnsUpstream(name="unsigned", address="192.0.2.53"),
                DnsUpstream(name="validated", address="192.0.2.54"),
            ],
        ),
        upstream_resolver=fake_upstream,
    )
    query = dns.message.make_query("secure-race.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())

    assert result.diagnostic.upstream == "direct:validated@192.0.2.54:53"
    assert result.diagnostic.dnssec.status == "validated_by_upstream"


def test_dns_helper_tunnel_upstream_uses_gatherlink_service_endpoint() -> None:
    calls = []

    def fake_tunnel(query, upstream):
        calls.append(upstream)
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.50"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[
                DnsUpstream(
                    name="peer-dns",
                    kind="tunnel",
                    address="127.0.0.1",
                    port=55153,
                    timeout_seconds=0.5,
                )
            ]
        ),
        tunnel_upstream_resolver=fake_tunnel,
    )
    query = dns.message.make_query("tunnel.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)

    assert response.rcode() == dns.rcode.NOERROR
    assert response.answer[0][0].address == "192.0.2.50"
    assert result.diagnostic.upstream == "tunnel:peer-dns@127.0.0.1:55153"
    assert calls[0].timeout_seconds == 0.5


def test_dns_helper_tunnel_upstream_sends_dns_datagram_to_service_endpoint() -> None:
    ready = threading.Event()

    def serve_once(sock: socket.socket) -> None:
        ready.set()
        query_wire, address = sock.recvfrom(4096)
        query = dns.message.from_wire(query_wire)
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.51"))
        sock.sendto(response.to_wire(), address)
        sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=serve_once, args=(sock,))
    thread.start()
    ready.wait(timeout=1)

    query = dns.message.make_query("udp-tunnel.example.", dns.rdatatype.A)
    response = query_tunnel_upstream(
        query,
        DnsUpstream(name="peer-dns", kind="tunnel", address="127.0.0.1", port=port, timeout_seconds=1),
    )
    thread.join(timeout=1)

    assert response.answer[0][0].address == "192.0.2.51"


def test_dns_helper_doh_upstream_uses_dnspython_https_transport() -> None:
    calls = []

    def fake_doh(query, upstream):
        calls.append(upstream)
        response = dns.message.make_response(query)
        question = query.question[0]
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "192.0.2.60"))
        return response

    resolver = DnsHelperResolver(
        policy=DnsResolverPolicy(
            upstreams=[
                DnsUpstream(
                    name="cloudflare",
                    kind="doh",
                    address="https://cloudflare-dns.com/dns-query",
                    port=443,
                )
            ]
        ),
        doh_upstream_resolver=fake_doh,
    )
    query = dns.message.make_query("doh.example.", dns.rdatatype.A)

    result = resolver.resolve_wire(query.to_wire())
    response = dns.message.from_wire(result.response_wire)

    assert response.rcode() == dns.rcode.NOERROR
    assert response.answer[0][0].address == "192.0.2.60"
    assert result.diagnostic.upstream == "doh:cloudflare@https://cloudflare-dns.com/dns-query:443"
    assert calls[0].kind == "doh"


def test_dns_helper_doh_endpoint_parses_https_urls_and_rejects_plain_http() -> None:
    endpoint = _doh_endpoint(DnsUpstream(name="cloudflare", kind="doh", address="https://cloudflare-dns.com/dns-query"))

    assert endpoint.host == "cloudflare-dns.com"
    assert endpoint.port == 443
    assert endpoint.path == "/dns-query"

    host_endpoint = _doh_endpoint(DnsUpstream(name="google", kind="doh", address="dns.google"))
    assert host_endpoint.host == "dns.google"
    assert host_endpoint.port == 443
    assert host_endpoint.path == "/dns-query"

    with pytest.raises(ValueError, match="https URL"):
        _doh_endpoint(DnsUpstream(name="bad", kind="doh", address="http://resolver.example/dns-query"))


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


def test_dns_helper_cli_parses_tunnel_upstream(monkeypatch) -> None:
    captured = {}

    class FakeServer:
        def __init__(self, _listen, resolver):
            captured["resolver"] = resolver

        def serve_forever(self):
            return None

    monkeypatch.setattr("gatherlink.cli.helpers.DnsUdpServer", FakeServer)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "dns-serve",
            "--listen",
            "127.0.0.1:5354",
            "--tunnel-upstream",
            "peer-dns=127.0.0.1:55153,timeout=0.5",
        ],
    )

    upstream = captured["resolver"].policy.upstreams[0]
    assert result.exit_code == 0
    assert upstream.kind == "tunnel"
    assert upstream.name == "peer-dns"
    assert upstream.address == "127.0.0.1"
    assert upstream.port == 55153
    assert upstream.timeout_seconds == 0.5


def test_dns_helper_cli_parses_doh_upstream_url_without_udp_port(monkeypatch) -> None:
    captured = {}

    class FakeServer:
        def __init__(self, _listen, resolver):
            captured["resolver"] = resolver

        def serve_forever(self):
            return None

    monkeypatch.setattr("gatherlink.cli.helpers.DnsUdpServer", FakeServer)

    result = CliRunner().invoke(
        app,
        [
            "helpers",
            "dns-serve",
            "--listen",
            "127.0.0.1:5354",
            "--doh-upstream",
            "cloudflare=https://cloudflare-dns.com/dns-query,timeout=0.75",
        ],
    )

    upstream = captured["resolver"].policy.upstreams[0]
    assert result.exit_code == 0
    assert upstream.kind == "doh"
    assert upstream.name == "cloudflare"
    assert upstream.address == "https://cloudflare-dns.com/dns-query"
    assert upstream.port == 443
    assert upstream.timeout_seconds == 0.75
