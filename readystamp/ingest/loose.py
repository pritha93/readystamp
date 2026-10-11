"""Loose-file ingest: PDFs and images -> a Production.

Each PDF, multi-page TIFF or single image is one document. Unsupported files
are listed in the load report; files that cannot be read stay in the document
list with zero pages and an error, so the user can see and remove them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from PIL import Image, UnidentifiedImageError

from readystamp.ingest.validate import report_issue
from readystamp.model import Document, LoadReport, Page, Production

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
SUPPORTED_EXTS = PDF_EXTS | IMAGE_EXTS
DEFAULT_DPI = 300

_GREY_MODES = {"1", "L", "LA", "I", "I;16", "F"}


def natural_key(name: str) -> list:
    """Sort key so that 'doc2' sorts before 'doc10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _expand(paths: list[Path], report: LoadReport) -> list[Path]:
    """Files given directly plus every file under given folders. Hidden files are skipped."""
    seen: set[Path] = set()
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            candidates = [c for c in p.rglob("*") if c.is_file()]
        elif p.is_file():
            candidates = [p]
        else:
            report_issue(report, "missing_file", f"{p.name} could not be found.", path=str(p))
            continue
        for c in candidates:
            if any(part.startswith(".") for part in c.relative_to(p.parent if p.is_file() else p).parts):
                continue
            key = c.resolve()
            if key not in seen:
                seen.add(key)
                files.append(c)
    return files


def _image_dpi(info: dict) -> int:
    dpi = info.get("dpi")
    try:
        value = round(float(dpi[0]))
    except (TypeError, ValueError, IndexError):
        return DEFAULT_DPI
    return value if value > 1 else DEFAULT_DPI


def pdf_pages(path: Path) -> list[Page]:
    """Pages of a PDF. Raises PermissionError if encrypted, ValueError if damaged."""
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF raises several types for corrupt files
        raise ValueError(str(exc)) from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise PermissionError("encrypted")
        if doc.page_count == 0:
            raise ValueError("no pages")
        pages = []
        for n, page in enumerate(doc):
            box = page.cropbox
            pages.append(Page(
                index_in_doc=n + 1, source_path=path, source_page=n,
                width_pt=box.width, height_pt=box.height, rotation=page.rotation % 360,
            ))
        return pages


def image_pages(path: Path) -> list[Page]:
    """Pages (frames) of an image file. Raises ValueError if it cannot be decoded."""
    try:
        with Image.open(path) as im:
            pages = []
            for frame in range(getattr(im, "n_frames", 1)):
                im.seek(frame)
                im.load()
                dpi = _image_dpi(im.info)
                w, h = im.size
                pages.append(Page(
                    index_in_doc=frame + 1, source_path=path,
                    source_page=frame if getattr(im, "n_frames", 1) > 1 else 0,
                    width_pt=w / dpi * 72, height_pt=h / dpi * 72, dpi=dpi,
                    is_color=im.mode not in _GREY_MODES,
                ))
            return pages
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ValueError(str(exc)) from exc


def load_loose(paths: list[Path], volume_name: str = "VOL001") -> Production:
    report = LoadReport()
    files = _expand([Path(p) for p in paths], report)
    supported = []
    for f in files:
        if f.suffix.lower() in SUPPORTED_EXTS:
            supported.append(f)
        else:
            report_issue(report, "unsupported_type",
                         f"{f.name} was ignored: {f.suffix or 'files without an extension'} is not a supported type "
                         "(PDF, TIFF, JPG or PNG).", path=str(f))
    supported.sort(key=lambda f: (natural_key(f.name), natural_key(str(f))))

    documents = []
    for seq, f in enumerate(supported, start=1):
        pages: list[Page] = []
        try:
            pages = pdf_pages(f) if f.suffix.lower() in PDF_EXTS else image_pages(f)
        except PermissionError:
            report_issue(report, "pdf_encrypted",
                         f"{f.name} is password-protected or encrypted and cannot be stamped. "
                         "Remove it, or supply an unlocked copy.", doc_seq=seq, path=str(f))
        except ValueError:
            check = "pdf_damaged" if f.suffix.lower() in PDF_EXTS else "image_unreadable"
            what = "PDF is damaged" if check == "pdf_damaged" else "image cannot be opened or decoded"
            report_issue(report, check, f"{f.name}: the {what}. Remove it, or supply a good copy.",
                         doc_seq=seq, path=str(f))
        documents.append(Document(seq=seq, pages=pages, source_name=f.name, source_path=f))

    return Production(source_kind="loose", volume_name=volume_name, documents=documents, load_report=report)
