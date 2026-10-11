"""Preview a stamped page as PNG, through the same code the final output uses."""

from __future__ import annotations

import datetime as dt
import io

import pymupdf
from PIL import Image

from readystamp.model import Production, Settings
from readystamp.stamping import image_stamp, pdf_stamp


class PreviewError(ValueError):
    pass


def render_preview(production: Production, settings: Settings, doc_seq: int, page_no: int, dpi: int = 100,
                   run_date: dt.date | None = None) -> bytes:
    """PNG of page `page_no` (1-based) of document `doc_seq`, stamped for the chosen output format."""
    docs = [d for d in production.documents if d.seq == doc_seq]
    if not docs:
        raise PreviewError(f"There is no document {doc_seq}.")
    doc = docs[0]
    if not 1 <= page_no <= doc.page_count:
        raise PreviewError(f"Document {doc_seq} has {doc.page_count} page(s); there is no page {page_no}.")
    page = doc.pages[page_no - 1]
    with pdf_stamp.SourceCache() as sources:
        if settings.output.format == "pdf":
            with pymupdf.open() as out:
                stamped = pdf_stamp.stamp_page(out, sources, production, doc, page, settings, run_date)
                return pdf_stamp.render_png(stamped, dpi)
        img, src_dpi, kind = image_stamp.stamp_for_tiff(production, doc, page, settings, sources, run_date)
    if src_dpi != dpi:
        size = (max(1, round(img.width * dpi / src_dpi)), max(1, round(img.height * dpi / src_dpi)))
        img = img.convert("L" if kind == "bw" else "RGB").resize(size, Image.Resampling.BOX)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
