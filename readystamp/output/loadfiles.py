"""Writers for the OPT, DAT and cross-reference CSV. Strict about what they produce."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from readystamp.model import Document, Page, Production, Settings

DAT_DELIM = "\x14"     # ¶
DAT_QUOTE = "\xfe"     # þ
DAT_NEWLINE = "\xae"   # ®


@dataclass
class OutputPage:
    page: Page
    rel_path: str          # relative to the volume folder, backslashes, e.g. IMAGES\IMG001\ABC000001.tif


@dataclass
class OutputDocument:
    doc: Document
    pages: list[OutputPage] = field(default_factory=list)
    pdf_rel_path: str | None = None   # PDF mode: PDF\ABC000001.pdf


# --- OPT ------------------------------------------------------------------------------

def opt_text(docs: list[OutputDocument], volume: str) -> str:
    lines = []
    for od in docs:
        for i, op in enumerate(od.pages):
            first = i == 0
            lines.append(",".join([
                op.page.new_bates, volume, ".\\" + op.rel_path,
                "Y" if first else "", "", "", str(len(od.pages)) if first else "",
            ]))
    return "".join(line + "\r\n" for line in lines)


def write_opt(path: Path, docs: list[OutputDocument], volume: str) -> None:
    path.write_bytes(opt_text(docs, volume).encode("utf-8"))


# --- DAT ------------------------------------------------------------------------------

def dat_columns(production: Production, settings: Settings) -> list[tuple[str, str]]:
    """(output column name, source) pairs in order. Source is a built-in name or 'src:<DAT column>'."""
    cols = [("BEGBATES", "BEGBATES"), ("ENDBATES", "ENDBATES"), ("PAGECOUNT", "PAGECOUNT")]
    if production.source_kind == "opt" and settings.numbering.mode == "renumber":
        cols += [("ORIG_BEGBATES", "ORIG_BEGBATES"), ("ORIG_ENDBATES", "ORIG_ENDBATES")]
    if production.source_kind == "loose":
        cols.append(("SOURCE_FILE", "SOURCE_FILE"))
    if settings.output.format == "pdf":
        cols.append(("FILEPATH", "FILEPATH"))
    taken = {name.upper() for name, _ in cols}
    for name in production.dat_columns:
        out = f"SRC_{name}" if name.upper() in taken else name
        taken.add(out.upper())
        cols.append((out, f"src:{name}"))
    return cols


def select_dat_columns(production: Production, settings: Settings) -> list[tuple[str, str]]:
    """The default columns, or the ones `settings.output.dat_fields` names, in that order."""
    cols = dat_columns(production, settings)
    wanted = settings.output.dat_fields
    if wanted is None:
        return cols
    by_name = {name.upper(): (name, src) for name, src in cols}
    unknown = [w for w in wanted if w.upper() not in by_name]
    if unknown:
        raise ValueError(f"Unknown DAT field(s): {', '.join(unknown)}. Available: {', '.join(n for n, _ in cols)}.")
    return [by_name[w.upper()] for w in wanted]


def _dat_value(od: OutputDocument, source: str) -> str:
    doc = od.doc
    match source:
        case "BEGBATES":
            return doc.beg_bates or ""
        case "ENDBATES":
            return doc.end_bates or ""
        case "PAGECOUNT":
            return str(doc.page_count)
        case "ORIG_BEGBATES":
            return doc.pages[0].original_bates or ""
        case "ORIG_ENDBATES":
            return doc.pages[-1].original_bates or ""
        case "SOURCE_FILE":
            return doc.source_name
        case "FILEPATH":
            return od.pdf_rel_path or ""
    return doc.fields.get(source[len("src:"):], "")


def _qualify(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", DAT_NEWLINE)
    value = value.replace(DAT_QUOTE, "").replace(DAT_DELIM, " ")
    return DAT_QUOTE + value + DAT_QUOTE


def dat_text(production: Production, settings: Settings, docs: list[OutputDocument]) -> str:
    cols = select_dat_columns(production, settings)
    rows = [[name for name, _ in cols]] + [[_dat_value(od, src) for _, src in cols] for od in docs]
    return "".join(DAT_DELIM.join(_qualify(v) for v in row) + "\r\n" for row in rows)


def write_dat(path: Path, production: Production, settings: Settings, docs: list[OutputDocument]) -> None:
    path.write_bytes(dat_text(production, settings, docs).encode("utf-8-sig"))


# --- cross-reference ------------------------------------------------------------------------

XREF_HEADER = ["DocNum", "PageInDoc", "OriginalBates", "NewBates", "SourceFile", "SourcePage", "OutputFile"]


def source_ref(page: Page) -> str:
    if page.opt_path is not None:
        ref = page.opt_path.replace("/", "\\")
        return ref[2:] if ref.startswith(".\\") else ref
    return str(page.source_path)


def xref_text(production: Production, docs: list[OutputDocument], frame_counts: dict[Path, int]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(XREF_HEADER)
    for num, od in enumerate(docs, start=1):
        for op in od.pages:
            p = op.page
            # Page numbers are 1-based for PDFs and multi-page TIFFs; 0 for single-page images.
            multi = p.is_pdf or frame_counts.get(p.source_path, 1) > 1
            writer.writerow([num, p.index_in_doc, p.original_bates or "", p.new_bates,
                             source_ref(p), p.source_page + 1 if multi else 0, op.rel_path])
    return buf.getvalue()


def write_xref(path: Path, production: Production, docs: list[OutputDocument], frame_counts: dict[Path, int]) -> None:
    path.write_bytes(xref_text(production, docs, frame_counts).encode("utf-8-sig"))
