"""Bundled Liberation fonts (SIL OFL 1.1), so output is the same on every machine."""

from pathlib import Path
from typing import Literal

FONT_DIR = Path(__file__).resolve().parent

_FAMILIES = {"sans": "LiberationSans", "serif": "LiberationSerif", "mono": "LiberationMono"}


def font_path(font: Literal["sans", "serif", "mono"] = "sans", bold: bool = False) -> Path:
    """Path to the bundled TTF for a zone's font and weight."""
    try:
        family = _FAMILIES[font]
    except KeyError:
        raise ValueError(f"Unknown font '{font}'; choose sans, serif or mono") from None
    return FONT_DIR / f"{family}-{'Bold' if bold else 'Regular'}.ttf"


def all_font_paths() -> list[Path]:
    return [font_path(f, b) for f in _FAMILIES for b in (False, True)]
