#!/usr/bin/env python3
"""Export observed path samples into a Gatherlink benchmark profile draft."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gatherlink.benchmarks.profile_export import export_profile, load_observations


def main(argv: list[str] | None = None) -> int:
    """Run the profile export CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path, help="JSON file containing profile_name and samples")
    parser.add_argument("--out", type=Path, help="Write profile JSON here instead of stdout")
    parser.add_argument("--payload-size", type=int, help="Override generated UDP payload size")
    args = parser.parse_args(argv)

    name, observations, pressure_mbit = load_observations(args.observations)
    profile = export_profile(name, observations, pressure_mbit=pressure_mbit, payload_size=args.payload_size)
    text = json.dumps(profile.export_dict(), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
