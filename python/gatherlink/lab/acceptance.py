"""
Acceptance report models shared by VM and lab harnesses.

The acceptance layer records operator proof, not dataplane behavior. Keeping the
schema in Python gives Bash/SSH harnesses a boring way to produce machine-readable
reports without teaching runtime code anything about VMs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from gatherlink.shared.models import GatherlinkBaseModel

AcceptanceStatus = Literal["pass", "fail", "skipped", "not_configured", "deferred"]


class AcceptanceCheck(GatherlinkBaseModel):
    """One normalized acceptance check result."""

    code: str
    status: AcceptanceStatus
    message: str
    node: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AcceptanceArtifact(GatherlinkBaseModel):
    """One file or directory produced by an acceptance run."""

    kind: str
    path: str
    description: str


class AcceptanceReport(GatherlinkBaseModel):
    """Machine-readable acceptance report for repeatable v0.9.1 gates."""

    schema_version: int = 1
    mode: str
    inventory: str
    output: str
    generated_utc: str = Field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    checks: list[AcceptanceCheck] = Field(default_factory=list)
    artifacts: list[AcceptanceArtifact] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether all required checks passed or were intentionally non-blocking."""
        return not any(check.status == "fail" for check in self.checks)

    def write_json(self, path: Path) -> None:
        """Write the report in deterministic JSON form."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
