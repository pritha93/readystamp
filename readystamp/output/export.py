"""Run a production to an output folder: checks first, then stamped files, then load files.

Every file is written under a temporary `.partial` name. A document's files are
renamed to their final names only once the whole document has succeeded, so a
crash, cancel or failed page never leaves a half-written file under a final name.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from readystamp import numbering
from readystamp.model import Production, Settings, unsafe_chars
from readystamp.output import loadfiles, pdf_out, tiff_out
from readystamp.output.loadfiles import OutputDocument, OutputPage
from readystamp.stamping.layout import validate_zones
from readystamp.stamping.pdf_stamp import SourceCache

PARTIAL = ".partial"

# Rough output sizes used for the free-space check.
_BW_PAGE_BYTES = 60_000
_COLOR_PAGE_BYTES = 600_000


@dataclass
class Preflight:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ExportResult:
    volume_dir: Path
    documents: list[OutputDocument] = field(default_factory=list)
    failures: list[tuple[int, str]] = field(default_factory=list)      # (doc seq, message)
    load_files: dict[str, Path] = field(default_factory=dict)
    cancelled: bool = False
    output_files: list[Path] = field(default_factory=list)

    @property
    def pages_written(self) -> int:
        return sum(len(d.pages) for d in self.documents)


class RunBlocked(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def input_folders(production: Production) -> set[Path]:
    """Folders the input lives in; the output must not be inside any of them."""
    folders = set()
    for page in production.pages():
        folders.add(page.source_path.resolve().parent)
    if production.source_kind == "opt":
        # The volume folder holding IMAGES/ and DATA/.
        for f in list(folders):
            images = next((a for a in [f, *f.parents] if a.name.lower() == "images"), None)
            if images is not None:
                folders.add(images.parent)
    return folders


def estimate_bytes(production: Production, settings: Settings) -> int:
    if settings.output.format == "pdf":
        sources = {p.source_path for p in production.pages()}
        return int(sum(s.stat().st_size for s in sources if s.exists()) * 1.1) + 20_000 * production.page_count
    return sum(_COLOR_PAGE_BYTES if p.is_color and settings.output.color_mode != "force_bw" else _BW_PAGE_BYTES
               for p in production.pages())


def preflight(production: Production, settings: Settings, run_date: dt.date | None = None) -> Preflight:
    """Everything that must be true before a run starts. Assigns Bates numbers when it can."""
    result = Preflight()
    errors = result.errors
    if production.load_report.has_errors:
        n = len(production.load_report.by_severity("error"))
        errors.append(f"The load report has {n} error(s). Remove the affected documents or fix the source "
                      "and reload before running.")
    if not production.documents:
        errors.append("There are no documents to produce.")

    plan = numbering.plan(production, settings.numbering, run_date)
    errors.extend(plan.problems)
    if plan.ok:
        numbering.apply(production, settings.numbering, run_date)
        unsafe = [p.new_bates for p in production.pages() if unsafe_chars(p.new_bates)]
        if unsafe:
            errors.append(f"Bates numbers must be safe in filenames: {', '.join(unsafe[:5])}.")

    for zone, problems in validate_zones(settings, production).items():
        errors.extend(f"Zone {zone.value}: {p}" for p in problems)

    try:
        loadfiles.select_dat_columns(production, settings)
    except ValueError as exc:
        errors.append(str(exc))

    out = settings.output.output_dir
    if out is None:
        errors.append("Choose an output folder.")
    else:
        out = Path(out).expanduser().resolve()
        if out.exists() and not out.is_dir():
            errors.append(f"The output folder {out} is a file, not a folder.")
        elif out.exists() and any(out.iterdir()):
            errors.append(f"The output folder {out} is not empty. Choose an empty or new folder.")
        for folder in input_folders(production):
            if out == folder or out.is_relative_to(folder):
                errors.append(f"The output folder must not be inside the input folder {folder}.")
                break
        result.estimated_bytes = estimate_bytes(production, settings)
        probe = next((p for p in [out, *out.parents] if p.exists()), None)
        if probe is not None:
            free = shutil.disk_usage(probe).free
            if free < 1.5 * result.estimated_bytes:
                result.warnings.append(
                    f"Only {free / 1e6:,.0f} MB is free, and the output may need about "
                    f"{result.estimated_bytes / 1e6:,.0f} MB.")
    return result


def _commit(partials: list[tuple[Path, Path]]) -> None:
    for tmp, final in partials:
        os.replace(tmp, final)


def _discard(partials: list[tuple[Path, Path]]) -> None:
    for tmp, _ in partials:
        tmp.unlink(missing_ok=True)


def _frame_counts(production: Production) -> dict[Path, int]:
    counts: dict[Path, int] = {}
    for p in production.pages():
        if p.is_pdf or p.source_path in counts:
            continue
        try:
            with Image.open(p.source_path) as im:
                counts[p.source_path] = getattr(im, "n_frames", 1)
        except OSError:
            counts[p.source_path] = 1
    return counts


class _Cancelled(Exception):
    def __init__(self, document_complete: bool):
        self.document_complete = document_complete


def _write_pdf_doc(production, doc, settings, sources, vol_dir, od, partials, run_date, tick) -> None:
    pdf_dir = vol_dir / "PDF"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    final = pdf_dir / f"{doc.beg_bates}.pdf"
    tmp = final.with_name(final.name + PARTIAL)
    partials[:] = [(tmp, final)]
    pdf_out.write_document(production, doc, settings, sources, tmp, run_date, on_page=tick)
    od.pdf_rel_path = f"PDF\\{final.name}"
    od.pages = [OutputPage(p, od.pdf_rel_path) for p in doc.pages]


def _write_tiff_doc(production, doc, settings, sources, vol_dir, od, partials, run_date, page_number, tick) -> None:
    for i, page in enumerate(doc.pages, start=1):
        img, dpi, kind = tiff_out.render_page(production, doc, page, settings, sources, run_date)
        folder = tiff_out.folder_name(page_number + i, settings.output.images_per_folder)
        final = vol_dir / "IMAGES" / folder / f"{page.new_bates}{tiff_out.extension(kind, settings)}"
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + PARTIAL)
        partials.append((tmp, final))
        tiff_out.save_page(img, dpi, kind, settings, tmp)
        od.pages.append(OutputPage(page, f"IMAGES\\{folder}\\{final.name}"))
        tick()


def export(production: Production, settings: Settings, run_date: dt.date | None = None,
           progress: Callable[[int, int, int], None] | None = None,
           should_cancel: Callable[[], bool] | None = None) -> ExportResult:
    """Write stamped output and load files. Raises RunBlocked if preflight finds errors.

    progress(pages_done, pages_total, current_doc_seq) is called after every page.
    should_cancel() is checked after every page; a cancelled run stops there, keeps every
    completed document, and removes only the cancelled document's temporary files.
    """
    run_date = run_date or dt.date.today()
    check = preflight(production, settings, run_date)
    if not check.ok:
        raise RunBlocked(check.errors)

    out_root = Path(settings.output.output_dir).expanduser().resolve()
    volume = settings.output.volume_name
    vol_dir = out_root / volume
    data_dir = vol_dir / "DATA"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult(volume_dir=vol_dir)
    total = production.page_count
    done = 0
    page_number = 0               # across successful documents, for folder splitting
    fmt = settings.output.format

    with SourceCache() as sources:
        for doc in production.documents:
            partials: list[tuple[Path, Path]] = []
            od = OutputDocument(doc)
            doc_start = done
            pages_in_doc = 0

            def tick():
                nonlocal done, pages_in_doc
                done += 1
                pages_in_doc += 1
                if progress:
                    progress(done, total, doc.seq)
                if should_cancel and should_cancel():
                    raise _Cancelled(pages_in_doc == doc.page_count)

            cancelled = False
            try:
                try:
                    if fmt == "pdf":
                        _write_pdf_doc(production, doc, settings, sources, vol_dir, od, partials, run_date, tick)
                    else:
                        _write_tiff_doc(production, doc, settings, sources, vol_dir, od, partials, run_date,
                                        page_number, tick)
                except _Cancelled as stop:
                    cancelled = True
                    if not stop.document_complete:
                        _discard(partials)
                        result.cancelled = True
                        break
                    if fmt == "pdf":       # the PDF is saved after its last page; finish it
                        _write_pdf_doc(production, doc, settings, sources, vol_dir, od, partials, run_date, None)
            except Exception as exc:  # one failing page fails its document; the run continues
                _discard(partials)
                result.failures.append((doc.seq, f"Document {doc.seq} ({doc.source_name}) failed: {exc}"))
                done = doc_start + doc.page_count
                continue
            _commit(partials)
            if fmt == "tiff":
                page_number += len(od.pages)
            result.documents.append(od)
            result.output_files.extend(final for _, final in partials)
            if cancelled:
                result.cancelled = True
                break

    files = {}
    if fmt == "tiff":
        files["opt"] = data_dir / f"{volume}.opt"
    files["dat"] = data_dir / f"{volume}.dat"
    files["xref"] = data_dir / f"{volume}_xref.csv"
    partials = [(p.with_name(p.name + PARTIAL), p) for p in files.values()]
    tmp = dict(zip(files, (t for t, _ in partials)))
    if "opt" in tmp:
        loadfiles.write_opt(tmp["opt"], result.documents, volume)
    loadfiles.write_dat(tmp["dat"], production, settings, result.documents)
    loadfiles.write_xref(tmp["xref"], production, result.documents, _frame_counts(production))
    _commit(partials)
    result.load_files = files
    return result
