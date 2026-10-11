"""Stamp a raster page (TIFF/JPG/PNG, or a PDF page rendered to pixels).

Used for TIFF output and for its preview. Layout comes from layout.py, so the
text lands where the PDF backend would put it.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFont

from readystamp.model import Document, OutputSettings, Page, Production, Settings
from readystamp.stamping.layout import Block, content_rect, page_blocks
from readystamp.stamping.pdf_stamp import SourceCache

RasterKind = Literal["bw", "color"]
COLOR_SPREAD = 24          # a pixel is coloured if its channels differ by more than this
_MASK_MARGIN = 4           # px around each block's mask, so antialiasing is not clipped


@lru_cache(maxsize=64)
def _font(path: Path, size_px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size_px, layout_engine=ImageFont.Layout.BASIC)


def load_raster(page: Page, pdf_dpi: int, sources: SourceCache) -> tuple[Image.Image, int]:
    """The page as pixels, as displayed, and its DPI. Images keep their native resolution."""
    if page.is_pdf:
        pix = sources.get(page.source_path)[page.source_page].get_pixmap(dpi=pdf_dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), pdf_dpi
    with Image.open(page.source_path) as im:
        im.seek(page.source_page)
        im.load()
        return im.copy(), page.dpi or 300


def is_color_image(img: Image.Image) -> bool:
    """True if any pixel has a channel spread greater than COLOR_SPREAD."""
    if img.mode in ("1", "L", "LA", "I", "I;16", "F"):
        return False
    r, g, b = img.convert("RGB").split()
    for a, c in ((r, g), (g, b), (r, b)):
        if ImageChops.difference(a, c).getextrema()[1] > COLOR_SPREAD:
            return True
    return False


def raster_kind(img: Image.Image, page: Page, output: OutputSettings) -> RasterKind:
    """Black-and-white or colour output. Images go by mode; rendered PDF pages by their pixels."""
    if output.color_mode == "force_bw":
        return "bw"
    colored = is_color_image(img) if page.is_pdf else page.is_color
    return "color" if colored else "bw"


def _block_mask(block: Block, k: float) -> Image.Image:
    m = _MASK_MARGIN
    mask = Image.new("L", (int(block.width * k) + 2 * m + 1, int(block.height * k) + 2 * m + 1), 0)
    draw = ImageDraw.Draw(mask)
    font = _font(block.font_file, block.size_pt * k)
    for line in block.lines:
        draw.text((line.x * k + m, line.y * k + m), line.text, fill=255, font=font, anchor="ls")
    if block.rotation:
        mask = mask.rotate(block.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    return mask


def draw_blocks(canvas: Image.Image, blocks: list[Block], dpi: int, force_black: bool) -> None:
    k = dpi / 72
    draw = ImageDraw.Draw(canvas)
    white = 255 if canvas.mode == "L" else (255, 255, 255)
    for block in blocks:
        if block.knockout:
            draw.polygon([(x * k, y * k) for x, y in block.corners(block.knockout_pad)], fill=white)
        if force_black:
            ink = 0 if canvas.mode == "L" else (0, 0, 0)
        else:
            ink = tuple(round(c * 255) for c in block.color)
            if canvas.mode == "L":
                ink = round(0.299 * ink[0] + 0.587 * ink[1] + 0.114 * ink[2])
        mask = _block_mask(block, k)
        # The mask's centre is the block's centre (its margins are symmetric).
        x = round(block.center[0] * k - (mask.width - 1) / 2)
        y = round(block.center[1] * k - (mask.height - 1) / 2)
        canvas.paste(ink, (x, y), mask)


def stamp_raster(img: Image.Image, dpi: int, kind: RasterKind, blocks: list[Block], settings: Settings,
                 page_size_pt: tuple[float, float]) -> Image.Image:
    """Return the stamped page: mode '1' for black-and-white, 'RGB' for colour."""
    work_mode = "L" if kind == "bw" else "RGB"
    base = img.convert(work_mode)
    if settings.margin.mode == "add_band":
        k = dpi / 72
        canvas = Image.new(work_mode, base.size, 255 if work_mode == "L" else (255, 255, 255))
        x0, y0, x1, y1 = content_rect(settings, page_size_pt)
        box = (round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k))
        canvas.paste(base.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS), box[:2])
    else:
        canvas = base
    draw_blocks(canvas, blocks, dpi, force_black=(kind == "bw"))
    if kind == "bw":
        threshold = settings.output.bw_threshold
        return canvas.point(lambda v: 255 if v >= threshold else 0).convert("1")
    return canvas


def stamp_for_tiff(production: Production, doc: Document, page: Page, settings: Settings,
                   sources: SourceCache, run_date: dt.date | None = None) -> tuple[Image.Image, int, RasterKind]:
    """The page exactly as TIFF output writes it: (image, dpi, kind)."""
    img, dpi = load_raster(page, settings.output.tiff_dpi, sources)
    kind = raster_kind(img, page, settings.output)
    blocks = page_blocks(production, doc, page, settings, run_date)
    return stamp_raster(img, dpi, kind, blocks, settings, page.display_size_pt), dpi, kind
