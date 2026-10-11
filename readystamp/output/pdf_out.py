"""PDF output: one stamped PDF per document, named <BEGBATES>.pdf."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pymupdf

from readystamp.model import Document, Production, Settings
from readystamp.stamping.pdf_stamp import SourceCache, stamp_page


def write_document(production: Production, doc: Document, settings: Settings, sources: SourceCache,
                   target: Path, run_date: dt.date | None = None,
                   on_page: Callable[[], None] | None = None) -> None:
    """Stamp every page of `doc` into a new PDF saved at `target` (normally a .partial name)."""
    with pymupdf.open() as out:
        for page in doc.pages:
            stamp_page(out, sources, production, doc, page, settings, run_date)
            if on_page:
                on_page()
        out.set_metadata({"producer": "ReadyStamp Lite", "creator": "ReadyStamp Lite"})
        out.subset_fonts()
        out.save(target, garbage=3, deflate=True)
