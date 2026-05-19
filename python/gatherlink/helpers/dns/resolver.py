"""
Path-aware DNS helper resolver with cache, DNSSEC, and route policy support.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import dns.exception
import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.helpers.dns.cache import DnsCacheKey, DnsResponseCache
from gatherlink.helpers.dns.dnssec import DnssecDiagnostic, evaluate_dnssec
from gatherlink.helpers.dns.domain_sets import normalize_qname
from gatherlink.helpers.dns.policies import DnsResolverPolicy, DnsUpstream
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


UpstreamResolver = Callable[[dns.message.Message, DnsUpstream], dns.message.Message]


@dataclass(frozen=True)
class DnsResolutionDiagnostic:
    """Operator-visible facts about one DNS helper lookup."""

    cache: str
    upstream: str | None
    qname: str
    qtype: str
    dnssec: DnssecDiagnostic
    error: str | None = None


@dataclass(frozen=True)
class DnsResolutionResult:
    """DNS response wire bytes plus diagnostics for monitor/logging."""

    response_wire: bytes
    diagnostic: DnsResolutionDiagnostic


@dataclass
class DnsHelperResolver:
    """Local DNS resolver endpoint backed by policy-selected upstreams."""

    policy: DnsResolverPolicy = field(default_factory=DnsResolverPolicy)
    cache: DnsResponseCache | None = None
    upstream_resolver: UpstreamResolver | None = None
    tunnel_upstream_resolver: UpstreamResolver | None = None
    doh_upstream_resolver: UpstreamResolver | None = None
    diagnostics_bus: DiagnosticsBus | None = None

    def __post_init__(self) -> None:
        """Install default cache and resolver dependencies."""
        if self.cache is None:
            self.cache = DnsResponseCache(serve_stale_seconds=self.policy.serve_stale_seconds)
        if self.upstream_resolver is None:
            self.upstream_resolver = query_direct_upstream
        if self.tunnel_upstream_resolver is None:
            self.tunnel_upstream_resolver = query_tunnel_upstream
        if self.doh_upstream_resolver is None:
            self.doh_upstream_resolver = query_doh_upstream

    def resolve_wire(self, query_wire: bytes) -> DnsResolutionResult:
        """Resolve a raw DNS query packet using cache, policy, and upstream diagnostics."""
        query = dns.message.from_wire(query_wire)
        if not query.question:
            return self._error_response(query, dns.rcode.FORMERR, "query did not include a question")

        question = query.question[0]
        qname = normalize_qname(question.name)
        key = DnsCacheKey(qname=qname, qtype=question.rdtype, qclass=question.rdclass)
        cached = self.cache.get(key) if self.cache else None
        if cached is not None:
            entry, stale = cached
            response = dns.message.from_wire(entry.response_wire)
            response.id = query.id
            return DnsResolutionResult(
                response_wire=response.to_wire(),
                diagnostic=DnsResolutionDiagnostic(
                    cache="stale" if stale else "hit",
                    upstream=entry.upstream,
                    qname=qname,
                    qtype=dns.rdatatype.to_text(question.rdtype),
                    dnssec=DnssecDiagnostic(status=entry.validation_status, message="cached response"),
                ),
            )

        for upstream in self.policy.ordered_upstreams():
            try:
                response = self._query_upstream(query, upstream)
            except (OSError, dns.exception.DNSException, NotImplementedError) as exc:
                logger.debug("DNS helper upstream failed", extra={"upstream": upstream.authority(), "error": str(exc)})
                self._publish_event(
                    code="dns.upstream_failed",
                    severity="warning",
                    message="DNS helper upstream failed",
                    qname=qname,
                    qtype=dns.rdatatype.to_text(question.rdtype),
                    upstream=upstream.authority(),
                    error=str(exc),
                )
                continue

            dnssec = evaluate_dnssec(response, self.policy.dnssec_mode)
            if not dnssec.accepted:
                self._publish_event(
                    code="dns.dnssec_bogus",
                    severity="warning",
                    message="DNS helper rejected upstream response by DNSSEC policy",
                    qname=qname,
                    qtype=dns.rdatatype.to_text(question.rdtype),
                    upstream=upstream.authority(),
                    dnssec_status=dnssec.status,
                    reason=dnssec.message,
                )
                return self._error_response(query, dns.rcode.SERVFAIL, dnssec.message, qname=qname, dnssec=dnssec)
            response.id = query.id
            if self.cache is not None:
                self.cache.put(key, response, upstream=upstream.authority(), validation_status=dnssec.status)
            return DnsResolutionResult(
                response_wire=response.to_wire(),
                diagnostic=DnsResolutionDiagnostic(
                    cache="miss",
                    upstream=upstream.authority(),
                    qname=qname,
                    qtype=dns.rdatatype.to_text(question.rdtype),
                    dnssec=dnssec,
                ),
            )

        return self._error_response(query, dns.rcode.SERVFAIL, "all configured DNS upstreams failed", qname=qname)

    def _publish_event(self, *, code: str, message: str, severity: str = "info", **details: object) -> None:
        """Publish structured DNS helper facts without blocking resolution."""
        if self.diagnostics_bus is None:
            return
        self.diagnostics_bus.publish(
            DiagnosticEvent.helper_event(
                code=code,
                helper="dns",
                severity=severity,
                message=message,
                details=details,
            )
        )

    def _query_upstream(self, query: dns.message.Message, upstream: DnsUpstream) -> dns.message.Message:
        if upstream.kind == "direct":
            return self.upstream_resolver(query, upstream)
        if upstream.kind == "tunnel":
            return self.tunnel_upstream_resolver(query, upstream)
        if upstream.kind == "doh":
            return self.doh_upstream_resolver(query, upstream)
        raise NotImplementedError(f"DNS upstream kind is not implemented: {upstream.kind}")

    def _error_response(
        self,
        query: dns.message.Message,
        rcode: dns.rcode.Rcode,
        error: str,
        *,
        qname: str | None = None,
        dnssec: DnssecDiagnostic | None = None,
    ) -> DnsResolutionResult:
        response = dns.message.make_response(query)
        response.set_rcode(rcode)
        question = query.question[0] if query.question else None
        return DnsResolutionResult(
            response_wire=response.to_wire(),
            diagnostic=DnsResolutionDiagnostic(
                cache="error",
                upstream=None,
                qname=qname or (normalize_qname(question.name) if question else "-"),
                qtype=dns.rdatatype.to_text(question.rdtype) if question else "-",
                dnssec=dnssec or DnssecDiagnostic(status="disabled", message="no accepted DNS response"),
                error=error,
            ),
        )


class DnsUdpServer:
    """Tiny UDP DNS listener for helper supervisors and local manual runs."""

    def __init__(self, listen: tuple[str, int], resolver: DnsHelperResolver) -> None:
        self.listen = listen
        self.resolver = resolver

    def serve_forever(self) -> None:
        """Serve DNS datagrams until interrupted by the supervisor."""
        with socket.socket(socket.AF_INET6 if ":" in self.listen[0] else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(self.listen)
            while True:
                query_wire, address = sock.recvfrom(4096)
                result = self.resolver.resolve_wire(query_wire)
                sock.sendto(result.response_wire, address)


def query_direct_upstream(query: dns.message.Message, upstream: DnsUpstream) -> dns.message.Message:
    """Send one DNS query to a direct UDP upstream, retrying over TCP if needed."""
    response = dns.query.udp(
        query,
        upstream.address,
        port=upstream.port,
        timeout=upstream.timeout_seconds,
    )
    if response.flags & dns.flags.TC:
        return dns.query.tcp(
            query,
            upstream.address,
            port=upstream.port,
            timeout=upstream.timeout_seconds,
        )
    return response


def query_tunnel_upstream(query: dns.message.Message, upstream: DnsUpstream) -> dns.message.Message:
    """
    Send one DNS query to a Gatherlink-carried UDP upstream endpoint.

    The endpoint is normally a local Gatherlink UDP service listen address.
    Gatherlink then carries the DNS datagram to the companion endpoint on the
    peer. DNS helper policy stays in Python; Rust only sees ordinary service
    datagrams and remains unaware of DNS semantics.
    """
    return dns.query.udp(
        query,
        upstream.address,
        port=upstream.port,
        timeout=upstream.timeout_seconds,
    )


def query_doh_upstream(query: dns.message.Message, upstream: DnsUpstream) -> dns.message.Message:
    """Send one DNS query to a DNS-over-HTTPS upstream using dnspython."""
    endpoint = _doh_endpoint(upstream)
    return dns.query.https(
        query,
        endpoint.host,
        port=endpoint.port,
        path=endpoint.path,
        timeout=upstream.timeout_seconds,
        post=True,
    )


@dataclass(frozen=True)
class _DohEndpoint:
    """Parsed DoH endpoint facts for dnspython."""

    host: str
    port: int
    path: str


def _doh_endpoint(upstream: DnsUpstream) -> _DohEndpoint:
    """Return host, port, and path for a configured DoH upstream."""
    if upstream.address.startswith(("https://", "http://")):
        parsed = urlparse(upstream.address)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("DoH upstream address must be an https URL")
        # TODO(dns-doh): Keep the default HTTPS port behavior explicit here so
        # operators can still override unusual DoH deployments without making
        # the normal case accidentally use DNS-over-UDP port 53.
        port = parsed.port or (upstream.port if upstream.port != 53 else 443)
        return _DohEndpoint(
            host=parsed.hostname,
            port=port,
            path=parsed.path or "/dns-query",
        )
    return _DohEndpoint(
        host=upstream.address,
        port=upstream.port if upstream.port != 53 else 443,
        path="/dns-query",
    )
