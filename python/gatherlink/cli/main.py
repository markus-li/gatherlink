"""
Gatherlink command line entrypoint.
"""

from __future__ import annotations

from pathlib import Path

import typer

from gatherlink.cli import config, lab, run, services
from gatherlink.cli.lab import down as lab_down
from gatherlink.cli.lab import status as lab_status
from gatherlink.cli.lab import up as lab_up

app = typer.Typer(help="Gatherlink carrier-aware multipath UDP transport.")
DEFAULT_LAB_CONFIG = Path("configs/lab/local-dual-path.json")
app.add_typer(config.app, name="config")
app.add_typer(lab.app, name="lab")
app.add_typer(run.app, name="run")
app.add_typer(services.app, name="services")


@app.command("up")
def up(path: Path = DEFAULT_LAB_CONFIG) -> None:
    """Start a Gatherlink service scenario."""
    lab_up(path)


@app.command("status")
def status(path: Path = DEFAULT_LAB_CONFIG) -> None:
    """Show Gatherlink service scenario status."""
    lab_status(path)


@app.command("down")
def down(path: Path = DEFAULT_LAB_CONFIG) -> None:
    """Stop a Gatherlink service scenario."""
    lab_down(path)


def main() -> None:
    """Run the Gatherlink CLI."""
    app()


if __name__ == "__main__":
    main()
