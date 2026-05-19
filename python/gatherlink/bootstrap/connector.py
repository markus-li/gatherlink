"""
Resolve plus authenticated connect validation for candidate peer endpoints.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from datetime import UTC, datetime, timedelta
from secrets import token_bytes

from gatherlink.bootstrap.cache import BootstrapEndpoint
from gatherlink.secrets.bundles import SignedDocument, sign_document
from gatherlink.secrets.identity import IdentityPublicRecord
from gatherlink.security.keys import NodeIdentity
from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)
BOOTSTRAP_CHALLENGE_DOMAIN = "GATHERLINK_BOOTSTRAP_CHALLENGE_V1"
DEFAULT_BOOTSTRAP_CHALLENGE_TTL_SECONDS = 60


class BootstrapProbeResult(GatherlinkBaseModel):
    """Result of validating one bootstrap candidate."""

    endpoint: BootstrapEndpoint
    reachable: bool
    authenticated: bool
    checked_at: datetime
    warning: str | None = None
    proof: dict[str, object] | None = None


class BootstrapChallenge(GatherlinkBaseModel):
    """Signed bootstrap challenge body that proves a peer owns an identity key."""

    schema_version: int = 1
    endpoint: BootstrapEndpoint
    nonce: str
    issued_at: datetime
    expires_at: datetime


def probe_candidate(
    endpoint: BootstrapEndpoint,
    *,
    allow_insecure: bool = False,
    expected_peer: IdentityPublicRecord | None = None,
    proof: SignedDocument | None = None,
    now: datetime | None = None,
) -> BootstrapProbeResult:
    """
    Validate whether a candidate can be used for bootstrap.

    Authenticated bootstrap is intentionally Python-owned and document-based at
    this stage: the candidate is accepted only when a signed challenge proof
    matches the expected peer identity and endpoint. Network challenge exchange
    can plug into the same proof verifier later without changing cache policy.
    """
    checked_at = now or datetime.now(UTC)
    if expected_peer is not None and proof is not None:
        try:
            challenge = verify_bootstrap_challenge_proof(
                proof,
                expected_peer,
                expected_endpoint=endpoint,
                now=checked_at,
            )
        except ValueError as exc:
            return BootstrapProbeResult(
                endpoint=endpoint,
                reachable=False,
                authenticated=False,
                checked_at=checked_at,
                warning=str(exc),
            )
        return BootstrapProbeResult(
            endpoint=endpoint.model_copy(update={"last_verified_at": checked_at}),
            reachable=True,
            authenticated=True,
            checked_at=checked_at,
            proof={
                "domain": proof.domain,
                "issued_at": challenge.issued_at.isoformat(),
                "expires_at": challenge.expires_at.isoformat(),
            },
        )

    if not allow_insecure:
        return BootstrapProbeResult(
            endpoint=endpoint,
            reachable=False,
            authenticated=False,
            checked_at=checked_at,
            warning="authenticated bootstrap requires --peer-identity and --proof",
        )

    logger.warning(
        "using insecure bootstrap candidate %s; this is only acceptable for local labs", endpoint.authority()
    )
    return BootstrapProbeResult(
        endpoint=endpoint,
        reachable=True,
        authenticated=False,
        checked_at=checked_at,
        warning="insecure plaintext bootstrap accepted for local lab use",
    )


def create_bootstrap_challenge(
    endpoint: BootstrapEndpoint,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_BOOTSTRAP_CHALLENGE_TTL_SECONDS,
    nonce: bytes | None = None,
) -> BootstrapChallenge:
    """Create a short-lived challenge for an authenticated bootstrap candidate."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = now or datetime.now(UTC)
    challenge_nonce = nonce or token_bytes(32)
    if len(challenge_nonce) < 16:
        raise ValueError("bootstrap challenge nonce must be at least 16 bytes")
    return BootstrapChallenge(
        endpoint=endpoint,
        nonce=b64encode(challenge_nonce).decode("ascii"),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
    )


def sign_bootstrap_challenge(identity: NodeIdentity, challenge: BootstrapChallenge) -> SignedDocument:
    """Sign a bootstrap challenge with the responding node identity."""
    return sign_document(identity, BOOTSTRAP_CHALLENGE_DOMAIN, challenge.model_dump(mode="json"))


def verify_bootstrap_challenge_proof(
    proof: SignedDocument,
    expected_peer: IdentityPublicRecord,
    *,
    expected_endpoint: BootstrapEndpoint | None = None,
    now: datetime | None = None,
) -> BootstrapChallenge:
    """Verify a signed bootstrap challenge proof against expected peer identity."""
    if proof.domain != BOOTSTRAP_CHALLENGE_DOMAIN:
        raise ValueError("unexpected bootstrap proof domain")
    proof.verify()
    peer_node_id, peer_ed25519_public, _peer_x25519_public = expected_peer.public_bytes()
    if proof.signer_node_id != peer_node_id or proof.signer_public_key != peer_ed25519_public:
        raise ValueError("bootstrap proof signer does not match expected peer identity")
    challenge = BootstrapChallenge.model_validate(proof.body)
    if b64decode(challenge.nonce) == b"":
        raise ValueError("bootstrap challenge nonce is empty")
    checked_at = now or datetime.now(UTC)
    if challenge.expires_at < checked_at:
        raise ValueError("bootstrap challenge proof has expired")
    if expected_endpoint is not None and challenge.endpoint.authority() != expected_endpoint.authority():
        raise ValueError("bootstrap proof endpoint does not match candidate")
    return challenge
