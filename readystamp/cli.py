"""Command line: `readystamp ui`, `readystamp run`, `readystamp make-fixtures`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import typer

app = typer.Typer(help="ReadyStamp Lite: Bates numbering and endorsement stamping.", no_args_is_help=True)

_FIXTURES_SCRIPT = Path(__file__).resolve().parents[1] / "tests" / "fixtures.py"


@app.command()
def ui() -> None:
    """Start the local web interface (built in Milestone 7)."""
    typer.echo("The web interface is not built yet (Milestone 7).")
    raise typer.Exit(code=1)


@app.command()
def run() -> None:
    """Run a headless stamping job (built in Milestone 5)."""
    typer.echo("Headless runs are not built yet (Milestone 5).")
    raise typer.Exit(code=1)


@app.command("make-fixtures")
def make_fixtures(output_dir: Path = typer.Argument(..., help="Folder to write the test data into")) -> None:
    """Generate all test fixtures (loose files and sample productions)."""
    if not _FIXTURES_SCRIPT.exists():
        typer.echo(f"Cannot find the fixture generator at {_FIXTURES_SCRIPT}. "
                   "Run this from a source checkout of ReadyStamp.")
        raise typer.Exit(code=1)
    spec = importlib.util.spec_from_file_location("readystamp_fixtures", _FIXTURES_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name, path in module.generate_all(output_dir).items():
        typer.echo(f"{name}: {path}")


if __name__ == "__main__":
    app()
