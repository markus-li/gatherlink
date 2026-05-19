#!/usr/bin/env python3
"""Validate that a diagnostics JSONL file is non-empty and parseable."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    """Return zero only when every non-empty line is valid JSON."""
    if len(sys.argv) != 2:
        print("usage: validate_jsonl.py PATH", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists() or path.stat().st_size == 0:
        print(f"diagnostics JSONL is missing or empty: {path}", file=sys.stderr)
        return 1
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"{path}:{line_number}: invalid JSONL: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
