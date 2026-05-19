"""
helpers.dns.cache module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import dns.message
import dns.rrset

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DNS_TTL_SECONDS = 30
DEFAULT_SERVE_STALE_SECONDS = 300


@dataclass(frozen=True)
class DnsCacheKey:
    """Canonical cache key for one DNS question."""

    qname: str
    qtype: int
    qclass: int


@dataclass
class DnsCacheEntry:
    """Stored DNS response plus expiry metadata."""

    response_wire: bytes
    expires_at: float
    stale_until: float
    upstream: str
    validation_status: str

    def is_fresh(self, now: float) -> bool:
        """Return whether the cached answer is still within normal TTL."""
        return now <= self.expires_at

    def is_servable(self, now: float) -> bool:
        """Return whether this response may be served, including stale mode."""
        return now <= self.stale_until


class DnsResponseCache:
    """Small in-memory DNS response cache with explicit serve-stale behavior."""

    def __init__(self, *, serve_stale_seconds: int = DEFAULT_SERVE_STALE_SECONDS) -> None:
        self._entries: dict[DnsCacheKey, DnsCacheEntry] = {}
        self._serve_stale_seconds = serve_stale_seconds

    def get(self, key: DnsCacheKey, *, now: float | None = None) -> tuple[DnsCacheEntry, bool] | None:
        """Return a cache entry and whether it is stale, removing dead entries."""
        checked_at = now if now is not None else time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            return None
        if not entry.is_servable(checked_at):
            del self._entries[key]
            return None
        return entry, not entry.is_fresh(checked_at)

    def put(
        self,
        key: DnsCacheKey,
        response: dns.message.Message,
        *,
        upstream: str,
        validation_status: str,
        now: float | None = None,
    ) -> DnsCacheEntry:
        """Store a response using the lowest answer TTL or a defensive default."""
        checked_at = now if now is not None else time.monotonic()
        ttl = min((rrset.ttl for rrset in _cacheable_rrsets(response)), default=DEFAULT_DNS_TTL_SECONDS)
        expires_at = checked_at + max(ttl, 0)
        entry = DnsCacheEntry(
            response_wire=response.to_wire(),
            expires_at=expires_at,
            stale_until=expires_at + self._serve_stale_seconds,
            upstream=upstream,
            validation_status=validation_status,
        )
        self._entries[key] = entry
        return entry


def _cacheable_rrsets(response: dns.message.Message) -> list[dns.rrset.RRset]:
    return list(response.answer) + list(response.authority)
