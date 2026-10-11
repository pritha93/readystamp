"""Stamp a page into an output PDF, keeping the source PDF's text layer.

Used for PDF output and for its preview. Source PDFs are only ever opened for
reading; stamped pages are built in a separate output document.
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pymupdf
from PIL import Image

from readystamp.model import Document, Page, Production, Settings
from readystamp.stamping.layout import Block, content_rect, page_blocks


class SourceCache:
    """Opens each source PDF once, read-only."""

    def __init__(self):
        self._docs: dict[Path, pymupdf.Document] = {}

    def get(self, path: Path) -> pymupdf.Document:
        if path not in self._docs:
            self._docs[path] = pymupdf.open(path)
        return self._docs[path]

    def close(self) -> None:
        for d in self._docs.values():
            d.close()
        self._docs.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def image_stream(page: Page) -> bytes:
    """Bytes of an image page suitable for embedding: original JPEG bytes, else lossless PNG."""
    path = page.source_path
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return path.read_bytes()
    with Image.open(path) as im:
        im.seek(page.source_page)
        frame = im.copy()
    if frame.mode not in ("1", "L", "RGB", "RGBA"):
        frame = frame.convert("RGB")
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return buf.getvalue()


def _font_name(block: Block) -> str:
    return "RS" + block.font_file.stem.replace("Liberation", "").replace("-", "")


def draw_blocks(page: pymupdf.Page, blocks: list[Block]) -> None:
    """Draw stamp blocks given in displayed coordinates onto a (possibly /Rotate'd) PDF page."""
    derot = page.derotation_matrix
    rot = page.rotation_matrix
    rot_linear = pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
    for block in blocks:
        if block.knockout:
            tl, tr, br, bl = (pymupdf.Point(p) * derot for p in block.corners(block.knockout_pad))
            page.draw_quad(pymupdf.Quad(tl, tr, bl, br), color=None, fill=(1, 1, 1), width=0)
        # morph works in PDF (y-up) space, so a positive angle turns text counter-clockwise as
        # displayed; composing with the page's rotation matrix keeps it upright on /Rotate pages.
        morph = pymupdf.Matrix(block.rotation) * rot_linear
        for line in block.lines:
            point = pymupdf.Point(block.to_page(line.x, line.y)) * derot
            page.insert_text(point, line.text, fontsize=block.size_pt, fontname=_font_name(block),
                             fontfile=str(block.font_file), color=block.color, morph=(point, morph))


def stamp_page(out: pymupdf.Document, sources: SourceCache, production: Production, doc: Document,
               page: Page, settings: Settings, run_date: dt.date | None = None) -> pymupdf.Page:
    """Append `page`, stamped, to `out`, and return the new output page."""
    size = page.display_size_pt
    banded = settings.margin.mode == "add_band"
    if page.is_pdf:
        src = sources.get(page.source_path)
        if banded:
            target = out.new_page(width=size[0], height=size[1])
            # show_pdf_page draws the unrotated page; turn it back to how the source page displays.
            target.show_pdf_page(pymupdf.Rect(content_rect(settings, size)), src, page.source_page,
                                 rotate=-page.rotation)
        else:
            out.insert_pdf(src, from_page=page.source_page, to_page=page.source_page)
            target = out[-1]
    else:
        target = out.new_page(width=size[0], height=size[1])
        target.insert_image(pymupdf.Rect(content_rect(settings, size)), stream=image_stream(page),
                            keep_proportion=False)
    draw_blocks(target, page_blocks(production, doc, page, settings, run_date))
    return target


def render_png(page: pymupdf.Page, dpi: int) -> bytes:
    return page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")
