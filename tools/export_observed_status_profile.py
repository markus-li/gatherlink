#!/usr/bin/env python3
"""Export Gatherlink service status counters into a path observation profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gatherlink.benchmarks.profile_export import export_profile, load_observations
from gatherlink.benchmarks.status_profile_export import status_observations


def main(argv: list[str] | None = None) -> int:
    """Run the status-to-profile export CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", type=Path, help="Gatherlink service status JSON captured after a traffic run")
    parser.add_argument("--duration", type=float, required=True, help="Traffic duration in seconds")
    parser.add_argument("--profile-name", default="observed-gatherlink-profile", help="Generated profile name")
    parser.add_argument("--pressure-mbit", type=float, help="Offered traffic pressure to record in the profile")
    parser.add_argument("--payload-size", type=int, help="Generated profile UDP payload size")
    parser.add_argument("--observations-out", type=Path, help="Optional raw observation JSON output")
    parser.add_argument("--out", type=Path, help="Write profile JSON here instead of stdout")
    args = parser.parse_args(argv)

    if args.duration <= 0:
        raise SystemExit("--duration must be greater than zero")

    status = json.loads(args.status.read_text(encoding="utf-8"))
    observations = status_observations(
        status,
        duration_seconds=args.duration,
        profile_name=args.profile_name,
        pressure_mbit=args.pressure_mbit,
    )
    if args.observations_out:
        args.observations_out.parent.mkdir(parents=True, exist_ok=True)
        args.observations_out.write_text(json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    name, loaded, pressure = load_observations_from_dict(observations)
    profile = export_profile(name, loaded, pressure_mbit=pressure, payload_size=args.payload_size)
    text = json.dumps(profile.export_dict(), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def load_observations_from_dict(raw: dict[str, Any]):
    """Load observations through the public file loader to keep one conversion path."""
    import tempfile

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json") as handle:
        json.dump(raw, handle)
        handle.flush()
        return load_observations(Path(handle.name))


if __name__ == "__main__":
    raise SystemExit(main())
