"""DNS helper package for local resolver endpoints."""

from gatherlink.helpers.dns.cache import DnsCacheEntry, DnsCacheKey, DnsResponseCache
from gatherlink.helpers.dns.dnssec import DnssecDiagnostic, evaluate_dnssec
from gatherlink.helpers.dns.policies import DnsResolverPolicy, DnsUpstream
from gatherlink.helpers.dns.resolver import DnsHelperResolver, DnsResolutionResult, DnsUdpServer

__all__ = [
    "DnsCacheEntry",
    "DnsCacheKey",
    "DnsHelperResolver",
    "DnsResolutionResult",
    "DnsResolverPolicy",
    "DnsResponseCache",
    "DnsUdpServer",
    "DnsUpstream",
    "DnssecDiagnostic",
    "evaluate_dnssec",
]
