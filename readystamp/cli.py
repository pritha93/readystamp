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
def run(
    input: list[Path] = typer.Option(..., "--input", "-i",
                                     help="An .opt file, or loose files and folders (repeat for several)"),
    output: Path = typer.Option(..., "--output", "-o", help="Empty or new output folder"),
    preset: Path | None = typer.Option(None, "--preset", "-p", help="Settings preset (JSON)"),
    format: str | None = typer.Option(None, "--format", "-f", help="pdf or tiff (overrides the preset)"),
    dat: Path | None = typer.Option(None, "--dat", help="Concordance DAT to join (OPT input only)"),
    dat_key: str | None = typer.Option(None, "--dat-key", help="DAT column holding the begin Bates"),
    volume: str | None = typer.Option(None, "--volume", help="Volume name (overrides the preset)"),
    skip_errors: bool = typer.Option(False, "--skip-errors",
                                     help="Leave out documents that have load-report errors"),
) -> None:
    """Load, check, stamp and write a production without the web interface."""
    from readystamp.ingest.dat import DatError
    from readystamp.ingest.loose import load_loose
    from readystamp.ingest.opt import LoadError, load_opt
    from readystamp.model import Settings
    from readystamp.output.export import RunBlocked, export
    from readystamp.presets import PresetError, load_preset

    def fail(lines: list[str], code: int = 2):
        for line in lines:
            typer.echo(line, err=True)
        raise typer.Exit(code=code)

    try:
        settings = load_preset(preset) if preset else Settings()
    except PresetError as exc:
        fail([f"Preset problem: {p}" for p in exc.problems])
    if format is not None:
        if format.lower() not in ("pdf", "tiff"):
            fail([f"--format must be pdf or tiff, not {format!r}."])
        settings.output.format = format.lower()
    if volume is not None:
        settings.output.volume_name = volume
    settings.output.output_dir = output

    try:
        if len(input) == 1 and input[0].suffix.lower() == ".opt":
            production = load_opt(input[0], dat, dat_key)
        elif dat is not None:
            fail(["--dat can only be used with a single .opt input."])
        else:
            production = load_loose(input, volume_name=settings.output.volume_name)
    except (LoadError, DatError) as exc:
        fail([str(exc)])

    report = production.load_report
    if skip_errors and report.has_errors:
        bad = {i.doc_seq for i in report.by_severity("error") if i.doc_seq is not None}
        names = [d.source_name for d in production.documents if d.seq in bad]
        production.remove_documents(bad)
        for name in names:
            typer.echo(f"Left out {name} because of load errors.")
    for issue in report.issues:
        typer.echo(f"[{issue.severity}] {issue.message}")
    t = production.totals()
    typer.echo(f"Loaded {t['documents']} document(s), {t['pages']} page(s).")

    try:
        result = export(production, settings,
                        progress=lambda done, total, seq: typer.echo(f"\r{done}/{total} pages", nl=False))
    except RunBlocked as exc:
        fail(["Cannot run:"] + [f"  - {p}" for p in exc.problems])
    typer.echo("")
    typer.echo(f"Wrote {len(result.documents)} document(s), {result.pages_written} page(s) to {result.volume_dir}.")
    for name, path in result.load_files.items():
        typer.echo(f"  {name}: {path}")
    if result.failures:
        fail(["Failed documents:"] + [f"  - {m}" for _, m in result.failures], code=1)


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
