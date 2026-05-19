"""
Operator-safe audit of persisted Gatherlink control-plane state.

The audit layer checks local state shape, permissions, and document validity
without printing secret values. Persisted files remain subordinate to explicit
config and signed/authenticated control documents; this module only answers
"what is present and does it look safe to use?" for operators and tests.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from gatherlink.persistence.sealed import SealedSecretEnvelope
from gatherlink.persistence.store import PRIVATE_FILE_ALLOWED_MODE, GatherlinkStatePaths, redact_secrets
from gatherlink.secrets.bundles import SignedDocument
from gatherlink.secrets.identity import IdentityPublicRecord, IdentityRecord

AuditSeverity = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class StateAuditFinding:
    """One redacted state-audit finding."""

    severity: AuditSeverity
    code: str
    path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def export_dict(self) -> dict[str, Any]:
        """Return a JSON-safe finding without secret values."""
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "details": redact_secrets(self.details),
        }


@dataclass(frozen=True)
class StateAuditReport:
    """Redacted audit report for one Gatherlink state directory."""

    state_dir: Path
    findings: list[StateAuditFinding]

    @property
    def ok(self) -> bool:
        """Return whether the state has no error findings."""
        return all(finding.severity != "error" for finding in self.findings)

    def export_dict(self) -> dict[str, Any]:
        """Return a stable JSON-friendly report."""
        return {
            "ok": self.ok,
            "state_dir": str(self.state_dir),
            "summary": _summarize_findings(self.findings),
            "findings": [finding.export_dict() for finding in self.findings],
        }


def audit_persistent_state(paths: GatherlinkStatePaths, *, strict_hints: bool = False) -> StateAuditReport:
    """
    Audit known state locations without exposing secret values.

    Authority files such as private identities, trust roots, signed bundles,
    and sealed secrets produce errors when invalid. Non-authoritative hints and
    endpoint caches produce warnings by default because runtime must be able to
    ignore corrupt hints and continue from explicit config.
    """
    findings: list[StateAuditFinding] = []
    _audit_private_identities(paths, findings)
    _audit_trust_roots(paths, findings)
    _audit_signed_bundles(paths, findings)
    _audit_sealed_secrets(paths, findings)
    _audit_json_hints(paths, findings, strict_hints=strict_hints)
    _audit_endpoint_cache(paths, findings, strict_hints=strict_hints)
    if not findings:
        findings.append(
            StateAuditFinding(
                severity="ok",
                code="state.empty",
                path=str(paths.state_dir),
                message="no persisted Gatherlink state artifacts found",
            )
        )
    return StateAuditReport(state_dir=paths.state_dir, findings=findings)


def _audit_private_identities(paths: GatherlinkStatePaths, findings: list[StateAuditFinding]) -> None:
    """Audit owner-only private identity records."""
    for path in sorted((paths.state_dir / "identities").glob("*.identity.json")):
        permission = _permission_finding(path, expected_private=True)
        if permission is not None:
            findings.append(permission)
            continue
        try:
            record = IdentityRecord.load(path)
            record.to_identity()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(_error("state.identity.invalid", path, "private identity is invalid", exc))
            continue
        findings.append(
            StateAuditFinding(
                severity="ok",
                code="state.identity.ok",
                path=str(path),
                message="private identity is owner-only and internally consistent",
                details={"node_id": record.node_id},
            )
        )


def _audit_trust_roots(paths: GatherlinkStatePaths, findings: list[StateAuditFinding]) -> None:
    """Audit public trust-root records."""
    for path in sorted((paths.state_dir / "trust-roots").glob("*.public.json")):
        try:
            record = IdentityPublicRecord.load(path)
            record.public_bytes()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(_error("state.trust_root.invalid", path, "trust root is invalid", exc))
            continue
        findings.append(
            StateAuditFinding(
                severity="ok",
                code="state.trust_root.ok",
                path=str(path),
                message="trust root public identity is valid",
                details={"node_id": record.node_id},
            )
        )


def _audit_signed_bundles(paths: GatherlinkStatePaths, findings: list[StateAuditFinding]) -> None:
    """Audit signed control-plane bundles."""
    for path in sorted((paths.state_dir / "bundles").glob("*.signed.json")):
        try:
            document = SignedDocument.load(path)
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            findings.append(_error("state.bundle.invalid", path, "signed bundle is invalid", exc))
            continue
        findings.append(
            StateAuditFinding(
                severity="ok",
                code="state.bundle.ok",
                path=str(path),
                message="signed bundle verifies",
                details={"domain": document.domain, "body_keys": sorted(document.body.keys())},
            )
        )


def _audit_sealed_secrets(paths: GatherlinkStatePaths, findings: list[StateAuditFinding]) -> None:
    """Audit sealed-secret envelopes without opening them."""
    for path in sorted((paths.state_dir / "secrets").glob("*.sealed.json")):
        permission = _permission_finding(path, expected_private=True)
        if permission is not None:
            findings.append(permission)
            continue
        try:
            envelope = SealedSecretEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            findings.append(_error("state.sealed_secret.invalid", path, "sealed secret envelope is invalid", exc))
            continue
        findings.append(
            StateAuditFinding(
                severity="ok",
                code="state.sealed_secret.ok",
                path=str(path),
                message="sealed secret envelope is owner-only and readable",
                details=envelope.public_summary(),
            )
        )


def _audit_json_hints(paths: GatherlinkStatePaths, findings: list[StateAuditFinding], *, strict_hints: bool) -> None:
    """Audit non-authoritative runtime hints."""
    for path in sorted((paths.state_dir / "hints").glob("*.json")):
        _audit_non_authoritative_json(path, findings, code_prefix="state.hint", strict=strict_hints)


def _audit_endpoint_cache(
    paths: GatherlinkStatePaths, findings: list[StateAuditFinding], *, strict_hints: bool
) -> None:
    """Audit the non-authoritative endpoint cache when present."""
    path = paths.bootstrap_cache_path()
    if path.exists():
        _audit_non_authoritative_json(path, findings, code_prefix="state.endpoint_cache", strict=strict_hints)


def _audit_non_authoritative_json(
    path: Path,
    findings: list[StateAuditFinding],
    *,
    code_prefix: str,
    strict: bool,
) -> None:
    """Audit JSON hints that can be ignored at runtime if corrupt."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            StateAuditFinding(
                severity="error" if strict else "warning",
                code=f"{code_prefix}.invalid",
                path=str(path),
                message="non-authoritative state JSON is invalid",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return
    findings.append(
        StateAuditFinding(
            severity="ok",
            code=f"{code_prefix}.ok",
            path=str(path),
            message="non-authoritative JSON state is readable",
        )
    )


def _permission_finding(path: Path, *, expected_private: bool) -> StateAuditFinding | None:
    """Return an error when a private artifact is group/world-readable."""
    if not expected_private or os.name != "posix":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~PRIVATE_FILE_ALLOWED_MODE:
        return StateAuditFinding(
            severity="error",
            code="state.permission.too_broad",
            path=str(path),
            message="private state artifact must be owner-only (0600)",
            details={"mode": oct(mode), "expected": "0o600"},
        )
    return None


def _error(code: str, path: Path, message: str, exc: Exception) -> StateAuditFinding:
    """Build a redacted error finding from an exception."""
    return StateAuditFinding(
        severity="error",
        code=code,
        path=str(path),
        message=message,
        details={"error_type": type(exc).__name__, "error": str(exc)},
    )


def _summarize_findings(findings: list[StateAuditFinding]) -> dict[str, int]:
    """Count findings by severity."""
    return {
        "ok": sum(1 for finding in findings if finding.severity == "ok"),
        "warning": sum(1 for finding in findings if finding.severity == "warning"),
        "error": sum(1 for finding in findings if finding.severity == "error"),
    }
