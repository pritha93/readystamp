"""Opticon OPT ingest: parse the load file, resolve image paths, build a Production.

Readers are lenient: comma-split lines with 3 to 7 fields, either path
separator, absolute Windows paths, and case-insensitive file matching.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from readystamp.ingest import dat as dat_mod
from readystamp.ingest.loose import IMAGE_EXTS, PDF_EXTS, image_pages, pdf_pages
from readystamp.ingest.validate import report_issue
from readystamp.model import Document, LoadReport, Page, Production


class LoadError(ValueError):
    """The load could not start at all (file missing, unreadable, bad options). User-readable."""


# ---------------------------------------------------------------------------
# Text decoding (shared with DAT)
# ---------------------------------------------------------------------------

_BOMS = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
]


def decode_text(raw: bytes, allow_utf16: bool = True) -> str:
    """BOM if present, else UTF-8, falling back to Windows-1252."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom) and (allow_utf16 or encoding == "utf-8-sig"):
            return raw.decode(encoding)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class OptRecord:
    line: int                 # 1-based physical line number
    key: str
    volume: str
    path: str
    doc_break: bool
    page_count: str           # raw field; may be blank


def parse_opt(text: str, report: LoadReport) -> list[OptRecord]:
    records = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        fields = [f.strip() for f in raw.split(",")]
        if len(fields) < 3:
            report_issue(report, "opt_too_few_fields",
                         f"OPT line {n} has {len(fields)} field(s); at least 3 are needed "
                         "(image key, volume, image path).", line=n)
            continue
        fields += [""] * (7 - len(fields))
        records.append(OptRecord(n, fields[0], fields[1], fields[2], fields[3].upper() == "Y", fields[6]))
    return records


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_WIN_ABS = re.compile(r"^[A-Za-z]:/")


class PathResolver:
    """Finds image files on disk for OPT paths, tolerating Windows paths and case differences."""

    def __init__(self, opt_dir: Path):
        self.bases = [opt_dir, opt_dir.parent] if opt_dir.parent != opt_dir else [opt_dir]
        self._listing: dict[Path, dict[str, str]] = {}

    def _children(self, folder: Path) -> dict[str, str]:
        if folder not in self._listing:
            try:
                self._listing[folder] = {name.lower(): name for name in os.listdir(folder)}
            except OSError:
                self._listing[folder] = {}
        return self._listing[folder]

    def _find(self, base: Path, parts: list[str]) -> Path | None:
        exact = base.joinpath(*parts)
        if exact.is_file():
            return exact
        current = base
        for part in parts:
            if part in ("", "."):
                continue
            if part == "..":
                current = current.parent
                continue
            match = self._children(current).get(part.lower())
            if match is None:
                return None
            current = current / match
        return current if current.is_file() else None

    def resolve(self, raw: str) -> Path | None:
        norm = raw.replace("\\", "/")
        is_unc = norm.startswith("//")
        is_win_abs = bool(_WIN_ABS.match(norm)) or is_unc
        if is_win_abs or norm.startswith("/"):
            direct = Path(raw if os.sep == "\\" else norm)
            if direct.is_file():
                return direct
            if not is_win_abs:
                return None
            parts = [p for p in PurePosixPath(norm).parts if p not in ("/", "//")]
            lowered = [p.lower() for p in parts]
            if "images" not in lowered:
                return None
            tail = parts[lowered.index("images"):]
            for base in self.bases:
                found = self._find(base, tail)
                if found:
                    return found
            return None
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        for base in self.bases:
            found = self._find(base, parts)
            if found:
                return found
        return None


# ---------------------------------------------------------------------------
# Building the production
# ---------------------------------------------------------------------------

_KEY_SPLIT = re.compile(r"^(.*?)(\d+)$")


def _check_gaps(records: list[OptRecord], report: LoadReport) -> None:
    prev = None
    for rec in records:
        m = _KEY_SPLIT.match(rec.key)
        if not m:
            prev = None
            continue
        prefix, number = m.group(1), int(m.group(2))
        if prev and prev[0] == prefix and number > prev[1] + 1:
            missing = number - prev[1] - 1
            report_issue(report, "bates_gap",
                         f"Image keys jump from {prev[2]} to {rec.key} on line {rec.line} "
                         f"({missing} number{'s' if missing > 1 else ''} missing).", line=rec.line)
        prev = (prefix, number, rec.key)


class _FrameSource:
    """Hands out successive pages/frames of each file, for OPT lines that share a file."""

    def __init__(self):
        self._pages: dict[Path, list[Page] | Exception] = {}
        self._next: dict[Path, int] = {}

    def take(self, path: Path) -> Page | str:
        """The next page of `path`, or a user-readable reason it is unavailable."""
        if path not in self._pages:
            try:
                self._pages[path] = pdf_pages(path) if path.suffix.lower() in PDF_EXTS else image_pages(path)
            except PermissionError:
                self._pages[path] = PermissionError("the PDF is password-protected or encrypted")
            except ValueError:
                self._pages[path] = ValueError("the file cannot be opened or decoded")
        pages = self._pages[path]
        if isinstance(pages, Exception):
            return str(pages)
        index = self._next.get(path, 0)
        self._next[path] = index + 1
        if index >= len(pages):
            return f"it is used for page {index + 1} but has only {len(pages)} page(s)"
        return pages[index].model_copy()


def load_opt(opt_path: Path, dat_path: Path | None = None, dat_key: str | None = None,
             dat_delimiter: str | None = None, dat_qualifier: str | None = None) -> Production:
    """Load an OPT production (optionally with its DAT) and run every load-report check."""
    opt_path = Path(opt_path)
    if not opt_path.is_file():
        raise LoadError(f"The OPT file {opt_path} could not be found.")
    try:
        text = decode_text(opt_path.read_bytes())
    except OSError as exc:
        raise LoadError(f"The OPT file {opt_path.name} could not be read: {exc.strerror or exc}.") from exc

    report = LoadReport()
    records = parse_opt(text, report)
    if not records:
        raise LoadError(f"{opt_path.name} contains no image lines.")
    if not records[0].doc_break:
        report_issue(report, "opt_first_line_not_break",
                     f"The first OPT line (line {records[0].line}) does not start a document "
                     "(its document-break field is not Y).", line=records[0].line)

    resolver = PathResolver(opt_path.parent)
    frames = _FrameSource()
    seen_keys: dict[str, int] = {}
    groups: list[list[OptRecord]] = []
    for rec in records:
        if rec.doc_break or not groups:
            groups.append([])
        groups[-1].append(rec)
        if rec.key in seen_keys:
            report_issue(report, "duplicate_key",
                         f"Image key {rec.key} on line {rec.line} was already used on line {seen_keys[rec.key]}.",
                         doc_seq=len(groups), line=rec.line)
        else:
            seen_keys[rec.key] = rec.line

    referenced: set[Path] = set()
    documents = []
    for seq, group in enumerate(groups, start=1):
        first = group[0]
        if first.page_count:
            try:
                declared = int(first.page_count)
            except ValueError:
                declared = None
            if declared != len(group):
                report_issue(report, "page_count_mismatch",
                             f"Line {first.line} says document {first.key} has {first.page_count} page(s), "
                             f"but {len(group)} line(s) belong to it.", doc_seq=seq, line=first.line)
        pages = []
        for rec in group:
            path = resolver.resolve(rec.path)
            if path is None:
                report_issue(report, "missing_file",
                             f"Line {rec.line} ({rec.key}) points to {rec.path}, which does not exist.",
                             doc_seq=seq, line=rec.line, path=rec.path)
                continue
            referenced.add(path.resolve())
            page = frames.take(path)
            if isinstance(page, str):
                check = "pdf_encrypted" if "password" in page else "image_unreadable"
                report_issue(report, check, f"Line {rec.line} ({rec.key}): {path.name} cannot be used: {page}.",
                             doc_seq=seq, line=rec.line, path=str(path))
                continue
            page.index_in_doc = len(pages) + 1
            page.original_bates = rec.key
            page.opt_path = rec.path
            pages.append(page)
        documents.append(Document(seq=seq, pages=pages, source_name=first.key))

    _check_gaps(records, report)
    _check_unreferenced(referenced, report)

    production = Production(source_kind="opt", volume_name=records[0].volume or opt_path.stem,
                            documents=documents, load_report=report)
    if dat_path is not None:
        dat_mod.attach_dat(production, Path(dat_path), key_column=dat_key,
                           delimiter=dat_delimiter, qualifier=dat_qualifier)
    return production


def _check_unreferenced(referenced: set[Path], report: LoadReport) -> None:
    """Image files in the production's image folders that no OPT line uses.

    The folders scanned are each referenced file's IMAGES folder (recursively) if it has
    one, otherwise just the folder the file is in.
    """
    roots: set[Path] = set()
    for p in referenced:
        images = next((a for a in p.parents if a.name.lower() == "images"), None)
        roots.add(images or p.parent)
    seen: set[Path] = set()
    for root in sorted(roots):
        files = root.rglob("*") if root.name.lower() == "images" else root.iterdir()
        for f in sorted(files):
            key = f.resolve()
            if key in seen or not f.is_file() or f.suffix.lower() not in IMAGE_EXTS or key in referenced:
                continue
            seen.add(key)
            report_issue(report, "unreferenced_image",
                         f"{f.name} is in the production folder but no OPT line refers to it.", path=str(f))
