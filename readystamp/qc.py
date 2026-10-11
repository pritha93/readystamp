"""Post-run quality checks. Everything is re-read from disk, never taken from memory."""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

from readystamp.ingest.dat import read_dat
from readystamp.ingest.opt import decode_text, parse_opt
from readystamp.ingest.loose import IMAGE_EXTS
from readystamp.model import LoadReport
from readystamp.output.loadfiles import XREF_HEADER

_MAX_DETAILS = 20


@dataclass
class QCExpectations:
    """What the run intended to produce, used to judge what is on disk."""
    format: str                      # 'pdf' or 'tiff'
    source_kind: str                 # 'loose' or 'opt'
    numbering_mode: str              # 'keep' or 'renumber'
    volume_name: str
    input_documents: int
    failed_documents: int
    expected_bates: list[str]        # every page of every document the run wrote, in order
    cancelled: bool = False


@dataclass
class QCCheck:
    name: str
    passed: bool
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class QCReport:
    checks: list[QCCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> list[dict]:
        return [{"check": c.name, "passed": c.passed, "summary": c.summary, "details": c.details}
                for c in self.checks]


def _add(report: QCReport, name: str, problems: list[str], ok_summary: str) -> None:
    passed = not problems
    summary = ok_summary if passed else f"{len(problems)} problem(s)"
    report.checks.append(QCCheck(name, passed, summary, problems[:_MAX_DETAILS] +
                                 ([f"… and {len(problems) - _MAX_DETAILS} more"] if len(problems) > _MAX_DETAILS else [])))


def _win_to_path(volume_dir: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    return volume_dir.joinpath(*[p for p in rel.split("/") if p])


@dataclass
class _Doc:
    pages: list[str]                 # Bates per page
    files: list[str]                 # relative path per page


def _read_opt_docs(path: Path) -> tuple[list[_Doc], list[str]]:
    report = LoadReport()
    records = parse_opt(decode_text(path.read_bytes()), report)
    problems = [i.message for i in report.issues]
    docs: list[_Doc] = []
    for rec in records:
        if rec.doc_break or not docs:
            docs.append(_Doc([], []))
        docs[-1].pages.append(rec.key)
        docs[-1].files.append(rec.path)
    return docs, problems


def run_qc(volume_dir: Path, exp: QCExpectations) -> QCReport:
    volume_dir = Path(volume_dir)
    data = volume_dir / "DATA"
    report = QCReport()
    tiff = exp.format == "tiff"

    # --- read the load files ---------------------------------------------------------------
    read_problems: list[str] = []
    opt_docs: list[_Doc] = []
    opt_path = data / f"{exp.volume_name}.opt"
    if tiff:
        if opt_path.is_file():
            opt_docs, p = _read_opt_docs(opt_path)
            read_problems += [f"OPT: {m}" for m in p]
        else:
            read_problems.append(f"{opt_path.name} is missing.")
    dat_header, dat_rows = [], []
    dat_path = data / f"{exp.volume_name}.dat"
    if dat_path.is_file():
        dat_header, dat_rows, _, p = read_dat(dat_path)
        read_problems += [f"DAT: {m}" for _, m in p]
    else:
        read_problems.append(f"{dat_path.name} is missing.")
    xref_rows: list[dict] = []
    xref_path = data / f"{exp.volume_name}_xref.csv"
    if xref_path.is_file():
        rows = list(csv.reader(io.StringIO(xref_path.read_bytes().decode("utf-8-sig"))))
        if not rows or rows[0] != XREF_HEADER:
            read_problems.append(f"{xref_path.name} does not have the expected header.")
        else:
            xref_rows = [dict(zip(rows[0], r)) for r in rows[1:]]
    else:
        read_problems.append(f"{xref_path.name} is missing.")
    col = {h.upper(): i for i, h in enumerate(dat_header)}

    def dat_value(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    # In PDF mode, documents and pages come from the PDFs the DAT points to.
    pdf_docs: list[_Doc] = []
    pdf_open_problems: list[str] = []
    if not tiff:
        for row in dat_rows:
            rel = dat_value(row, "FILEPATH")
            path = _win_to_path(volume_dir, rel)
            count = 0
            if path.is_file():
                try:
                    with pymupdf.open(path) as d:
                        count = d.page_count
                except Exception as exc:
                    pdf_open_problems.append(f"{rel} cannot be opened: {exc}")
            beg, end = dat_value(row, "BEGBATES"), dat_value(row, "ENDBATES")   # pages: just the range ends
            pdf_docs.append(_Doc([beg, end], [rel] * count))
    docs = opt_docs if tiff else pdf_docs

    # --- 1. document count --------------------------------------------------------------------
    expected_docs = exp.input_documents - exp.failed_documents
    problems = list(read_problems)
    if not exp.cancelled and len(docs) != expected_docs:
        problems.append(f"{len(docs)} document(s) in the output; expected {expected_docs} "
                        f"({exp.input_documents} input minus {exp.failed_documents} failed).")
    if len(dat_rows) != len(docs):
        problems.append(f"The DAT has {len(dat_rows)} row(s) but the output has {len(docs)} document(s).")
    _add(report, "Document count", problems, f"{len(docs)} document(s)")

    # --- 2. page count --------------------------------------------------------------------------
    problems = []
    out_pages = sum(len(d.files) for d in docs)
    dat_pages = 0
    for row in dat_rows:
        try:
            dat_pages += int(dat_value(row, "PAGECOUNT"))
        except ValueError:
            problems.append(f"DAT row {dat_value(row, 'BEGBATES')} has no valid PAGECOUNT.")
    if out_pages != dat_pages:
        problems.append(f"The output has {out_pages} page(s) but the DAT page counts add up to {dat_pages}.")
    if out_pages != len(exp.expected_bates):
        problems.append(f"The output has {out_pages} page(s); the run wrote {len(exp.expected_bates)}.")
    _add(report, "Page count", problems, f"{out_pages} page(s)")

    # --- 3. Bates continuity --------------------------------------------------------------------------
    problems = []
    on_disk = [d for doc in opt_docs for d in doc.pages] if tiff else [r["NewBates"] for r in xref_rows]
    seen = set()
    for b in on_disk:
        if b in seen:
            problems.append(f"Bates number {b} appears more than once.")
        seen.add(b)
    if on_disk != exp.expected_bates:
        missing = [b for b in exp.expected_bates if b not in seen]
        extra = [b for b in on_disk if b not in set(exp.expected_bates)]
        problems += [f"Bates number {b} is missing from the output." for b in missing]
        problems += [f"Bates number {b} was not expected." for b in extra]
        if not missing and not extra and not any("more than once" in p for p in problems):
            problems.append("Bates numbers are out of order.")
    elif exp.expected_bates:
        if on_disk[0] != exp.expected_bates[0] or on_disk[-1] != exp.expected_bates[-1]:
            problems.append("The first or last Bates number is not the expected one.")
    summary = f"{on_disk[0]} – {on_disk[-1]}" if on_disk else "no pages"
    if exp.failed_documents and not problems:
        summary += f" (numbers of {exp.failed_documents} failed document(s) are absent)"
    _add(report, "Bates continuity", problems, summary)

    # --- 4. load file <-> files --------------------------------------------------------------------
    problems = []
    if tiff:
        listed = set()
        for doc in opt_docs:
            for rel in doc.files:
                path = _win_to_path(volume_dir, rel)
                listed.add(path.resolve())
                if not path.is_file():
                    problems.append(f"The OPT points to {rel}, which does not exist.")
        images = volume_dir / "IMAGES"
        if images.is_dir():
            for f in sorted(images.rglob("*")):
                if f.is_file() and f.resolve() not in listed:
                    problems.append(f"{f.relative_to(volume_dir)} is in the output but not in the OPT.")
        name = "OPT ↔ files"
    else:
        listed = set()
        for row in dat_rows:
            rel = dat_value(row, "FILEPATH")
            path = _win_to_path(volume_dir, rel)
            listed.add(path.resolve())
            if not path.is_file():
                problems.append(f"The DAT points to {rel}, which does not exist.")
        pdf_dir = volume_dir / "PDF"
        if pdf_dir.is_dir():
            for f in sorted(pdf_dir.rglob("*")):
                if f.is_file() and f.resolve() not in listed:
                    problems.append(f"{f.relative_to(volume_dir)} is in the output but not in the DAT.")
        name = "DAT ↔ files"
    _add(report, name, problems, "every file is listed and present")

    # --- 5. DAT <-> OPT ---------------------------------------------------------------------------------
    problems = []
    if tiff:
        by_beg = {dat_value(r, "BEGBATES"): r for r in dat_rows}
        if len(by_beg) != len(dat_rows):
            problems.append("The DAT has more than one row with the same BEGBATES.")
        for doc in opt_docs:
            row = by_beg.get(doc.pages[0])
            if row is None:
                problems.append(f"Document {doc.pages[0]} has no DAT row.")
            elif dat_value(row, "ENDBATES") != doc.pages[-1]:
                problems.append(f"Document {doc.pages[0]}: DAT ENDBATES is {dat_value(row, 'ENDBATES')}, "
                                f"the OPT's last page is {doc.pages[-1]}.")
        opt_begs = {d.pages[0] for d in opt_docs}
        problems += [f"DAT row {b} matches no OPT document." for b in by_beg if b not in opt_begs]
        name = "DAT ↔ OPT"
    else:
        xref_by_file: dict[str, list[str]] = {}
        for r in xref_rows:
            xref_by_file.setdefault(r["OutputFile"], []).append(r["NewBates"])
        for row, doc in zip(dat_rows, pdf_docs):
            rel = dat_value(row, "FILEPATH")
            pages = xref_by_file.get(rel, [])
            if len(doc.files) != len(pages):
                problems.append(f"{rel} has {len(doc.files)} page(s); the cross-reference lists {len(pages)}.")
            if pages and (pages[0] != dat_value(row, "BEGBATES") or pages[-1] != dat_value(row, "ENDBATES")):
                problems.append(f"{rel}: DAT BEGBATES/ENDBATES do not match its first and last page.")
        name = "DAT ↔ PDFs"
    _add(report, name, problems, "every document matches")

    # --- 6. files open -------------------------------------------------------------------------------------
    problems = list(pdf_open_problems)
    if tiff:
        images = volume_dir / "IMAGES"
        for f in sorted(images.rglob("*")) if images.is_dir() else []:
            if not f.is_file():
                continue
            rel = f.relative_to(volume_dir)
            if f.suffix.lower() not in IMAGE_EXTS:
                problems.append(f"{rel} is not an image file.")
                continue
            try:
                with Image.open(f) as im:
                    im.load()
                    if f.suffix.lower() == ".tif" and im.mode == "1" and im.info.get("compression") != "group4":
                        problems.append(f"{rel} is black-and-white but not Group IV compressed.")
                    if f.suffix.lower() == ".tif" and im.mode not in ("1", "RGB"):
                        problems.append(f"{rel} has unexpected image mode {im.mode}.")
            except Exception as exc:
                problems.append(f"{rel} cannot be opened: {exc}")
    _add(report, "Files open", problems, "every output file opens")

    # --- 7. cross-reference ----------------------------------------------------------------------------------
    problems = []
    if len(xref_rows) != out_pages:
        problems.append(f"The cross-reference has {len(xref_rows)} row(s) for {out_pages} output page(s).")
    for n, r in enumerate(xref_rows, start=2):
        if not r.get("NewBates"):
            problems.append(f"Cross-reference line {n} has no new Bates number.")
        if exp.source_kind == "opt" and not r.get("OriginalBates"):
            problems.append(f"Cross-reference line {n} has no original Bates number.")
    if tiff and [r["NewBates"] for r in xref_rows] != [b for d in opt_docs for b in d.pages]:
        problems.append("The cross-reference's new Bates numbers do not match the OPT.")
    _add(report, "Cross-reference", problems, f"{len(xref_rows)} row(s)")
    return report


def render_html(report: QCReport, title: str, extra: dict[str, str] | None = None) -> str:
    rows = []
    for c in report.checks:
        details = "".join(f"<li>{html.escape(d)}</li>" for d in c.details)
        rows.append(
            f"<tr class='{'pass' if c.passed else 'fail'}'><td>{html.escape(c.name)}</td>"
            f"<td>{'Pass' if c.passed else 'Fail'}</td><td>{html.escape(c.summary)}"
            f"{f'<ul>{details}</ul>' if details else ''}</td></tr>")
    meta = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in (extra or {}).items())
    verdict = "All checks passed" if report.passed else "Some checks failed"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fff}}
table{{border-collapse:collapse;margin:1rem 0;min-width:40rem}}
td,th{{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;vertical-align:top}}
tr.pass td:nth-child(2){{color:#11692b;font-weight:600}}
tr.fail td:nth-child(2){{color:#b00020;font-weight:600}}
.verdict{{font-size:1.2rem;font-weight:600}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p class="verdict">{verdict}</p>
<table>{meta}</table>
<table><tr><th>Check</th><th>Result</th><th>Details</th></tr>{''.join(rows)}</table>
</body></html>
"""
