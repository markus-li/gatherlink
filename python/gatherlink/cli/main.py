"""
Gatherlink command line entrypoint.
"""

from __future__ import annotations

import typer

from gatherlink.cli import config

app = typer.Typer(help="Gatherlink carrier-aware multipath UDP transport.")
app.add_typer(config.app, name="config")


def main() -> None:
    """Run the Gatherlink CLI."""
    app()


if __name__ == "__main__":
    main()
