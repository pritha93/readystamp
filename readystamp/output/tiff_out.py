"""TIFF output: one file per page, Group IV for black-and-white, JPEG or LZW TIFF for colour."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PIL import Image

from readystamp.model import Document, Page, Production, Settings
from readystamp.stamping.image_stamp import stamp_for_tiff
from readystamp.stamping.pdf_stamp import SourceCache


def folder_name(page_number: int, per_folder: int) -> str:
    """IMG001 for pages 1..per_folder, IMG002 for the next, and so on (page_number is 1-based)."""
    return f"IMG{(page_number - 1) // per_folder + 1:03d}"


def extension(kind: str, settings: Settings) -> str:
    return ".jpg" if kind == "color" and settings.output.color_mode == "jpeg" else ".tif"


def save_page(img: Image.Image, dpi: int, kind: str, settings: Settings, target: Path) -> None:
    """Save a stamped page in its final format. `target` may have any (e.g. .partial) suffix."""
    if kind == "bw":
        img.convert("1").save(target, format="TIFF", compression="group4", dpi=(dpi, dpi))
    elif settings.output.color_mode == "jpeg":
        img.convert("RGB").save(target, format="JPEG", quality=settings.output.jpeg_quality, dpi=(dpi, dpi))
    else:
        img.convert("RGB").save(target, format="TIFF", compression="tiff_lzw", dpi=(dpi, dpi))


def render_page(production: Production, doc: Document, page: Page, settings: Settings,
                sources: SourceCache, run_date: dt.date | None = None) -> tuple[Image.Image, int, str]:
    return stamp_for_tiff(production, doc, page, settings, sources, run_date)
