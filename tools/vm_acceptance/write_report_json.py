#!/usr/bin/env python3
"""Write a machine-readable VM acceptance report from shell harness facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gatherlink.lab.acceptance import AcceptanceArtifact, AcceptanceCheck, AcceptanceReport


def _load_checks(path: Path) -> list[AcceptanceCheck]:
    checks: list[AcceptanceCheck] = []
    if not path.exists():
        return checks
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            checks.append(AcceptanceCheck(**json.loads(line)))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSONL check: {exc}") from exc
    return checks


def main() -> None:
    """Parse command-line arguments and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--checks-jsonl", required=True)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--artifact", action="append", default=[], help="kind:path:description")
    args = parser.parse_args()

    artifacts = []
    for raw in args.artifact:
        parts = raw.split(":", 2)
        if len(parts) != 3:
            raise SystemExit(f"artifact must be kind:path:description, got: {raw}")
        artifacts.append(AcceptanceArtifact(kind=parts[0], path=parts[1], description=parts[2]))

    report = AcceptanceReport(
        mode=args.mode,
        inventory=args.inventory,
        output=args.out,
        checks=_load_checks(Path(args.checks_jsonl)),
        artifacts=artifacts,
    )
    report.write_json(Path(args.report_json))


if __name__ == "__main__":
    main()
