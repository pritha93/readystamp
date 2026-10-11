"""Zone geometry, shared by the PDF and raster stamping backends.

Everything here is in points, in the page's *displayed* coordinate system
(origin top-left, y down, after any PDF /Rotate). A Block is one zone's text
laid out in its own local box; `Block.to_page` maps local points onto the page.

Text is positioned by its ink (the visible glyph extents), not by font
ascender/descender boxes, so the visible text sits exactly `edge_offset`
from the page edges.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from readystamp import tokens
from readystamp.fonts import font_path
from readystamp.model import Document, Page, Production, Settings, ZoneId, ZoneSettings

LINE_HEIGHT = 1.2          # x font size
KNOCKOUT_PAD = 0.2         # x font size, around the ink box
_REF_SIZE = 1000           # measuring size, in pixels = points at 72 dpi

_ROWS = {"T": "top", "M": "middle", "B": "bottom"}
_COLS = {"L": "left", "C": "center", "R": "right"}


@lru_cache(maxsize=16)
def _ref_font(path: Path) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), _REF_SIZE, layout_engine=ImageFont.Layout.BASIC)


def ink_bbox(text: str, path: Path, size_pt: float) -> tuple[float, float, float, float]:
    """Ink extents of one line relative to its pen origin on the baseline: (x0, top, x1, bottom), y down."""
    if not text.strip():
        return 0.0, 0.0, 0.0, 0.0
    x0, y0, x1, y1 = _ref_font(path).getbbox(text, anchor="ls")
    k = size_pt / _REF_SIZE
    return x0 * k, y0 * k, x1 * k, y1 * k


@dataclass(frozen=True)
class Line:
    text: str
    x: float            # pen origin (baseline-left) in block-local coordinates
    y: float


@dataclass(frozen=True)
class Block:
    zone: ZoneId
    lines: tuple[Line, ...]
    font_file: Path
    size_pt: float
    color: tuple[float, float, float]   # 0..1
    rotation: int                        # degrees counter-clockwise as displayed
    knockout: bool
    width: float                         # local ink box: (0, 0) to (width, height)
    height: float
    center: tuple[float, float]          # where the local box centre lands on the page

    def to_page(self, x: float, y: float) -> tuple[float, float]:
        """Map a block-local point to displayed page coordinates."""
        a = math.radians(self.rotation)
        dx, dy = x - self.width / 2, y - self.height / 2
        # counter-clockwise on screen, with y pointing down
        return (self.center[0] + dx * math.cos(a) + dy * math.sin(a),
                self.center[1] - dx * math.sin(a) + dy * math.cos(a))

    def corners(self, pad: float = 0.0) -> list[tuple[float, float]]:
        """Page coordinates of the (padded) local box: top-left, top-right, bottom-right, bottom-left."""
        w, h = self.width, self.height
        return [self.to_page(x, y) for x, y in ((-pad, -pad), (w + pad, -pad), (w + pad, h + pad), (-pad, h + pad))]

    def bounds(self, pad: float = 0.0) -> tuple[float, float, float, float]:
        xs, ys = zip(*self.corners(pad))
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def knockout_pad(self) -> float:
        return KNOCKOUT_PAD * self.size_pt


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def applies(rule: str, page: Page, doc: Document) -> bool:
    match rule:
        case "all":
            return True
        case "first":
            return page.index_in_doc == 1
        case "last":
            return page.index_in_doc == doc.page_count
        case "odd":
            return page.index_in_doc % 2 == 1
        case "even":
            return page.index_in_doc % 2 == 0
        case "portrait":
            return not page.is_landscape
        case "landscape":
            return page.is_landscape
    raise ValueError(f"Unknown page rule {rule!r}")


def band_layout(settings: Settings, page_size: tuple[float, float]) -> tuple[float, float, float] | None:
    """In add_band mode: (scale, top_band, bottom_band) in points; None in overlay mode.

    A band is reserved at the top (bottom) wherever any zone in that row is enabled.
    Content is scaled uniformly to fit between the bands and centred horizontally.
    """
    if settings.margin.mode != "add_band":
        return None
    band = settings.margin.band_in * 72
    top = band if any(settings.zones[z].enabled for z in (ZoneId.TL, ZoneId.TC, ZoneId.TR)) else 0.0
    bottom = band if any(settings.zones[z].enabled for z in (ZoneId.BL, ZoneId.BC, ZoneId.BR)) else 0.0
    w, h = page_size
    scale = (h - top - bottom) / h
    return scale, top, bottom


def content_rect(settings: Settings, page_size: tuple[float, float]) -> tuple[float, float, float, float]:
    """Where the original page content is drawn, in displayed page points."""
    w, h = page_size
    band = band_layout(settings, page_size)
    if band is None:
        return 0.0, 0.0, w, h
    scale, top, _ = band
    cw, ch = w * scale, h * scale
    x0 = (w - cw) / 2
    return x0, top, x0 + cw, top + ch


def layout_zone(zone: ZoneId, zs: ZoneSettings, text: str, page_size: tuple[float, float],
                edge_offset_pt: float) -> Block | None:
    """Lay out one zone's resolved text on a page of the given displayed size."""
    raw_lines = text.split("\n")
    if not any(l.strip() for l in raw_lines):
        return None
    path = font_path(zs.font, zs.bold)
    size = zs.size_pt
    lh = LINE_HEIGHT * size
    boxes = [ink_bbox(l, path, size) for l in raw_lines]
    width = max(b[2] - b[0] for b in boxes)
    first_top = -boxes[0][1]                    # ink above the first baseline
    last_bottom = boxes[-1][3]                  # ink below the last baseline
    height = first_top + (len(raw_lines) - 1) * lh + last_bottom
    col = _COLS[zone.value[1]]
    lines = []
    for i, (l, (x0, _, x1, _)) in enumerate(zip(raw_lines, boxes)):
        if not l.strip():
            continue
        ink_w = x1 - x0
        if col == "left":
            x = -x0
        elif col == "right":
            x = width - x1
        else:
            x = (width - ink_w) / 2 - x0
        lines.append(Line(l, x, first_top + i * lh))

    rotation = zs.rotation
    a = math.radians(rotation)
    fw = abs(math.cos(a)) * width + abs(math.sin(a)) * height     # footprint on the page
    fh = abs(math.sin(a)) * width + abs(math.cos(a)) * height
    pw, ph = page_size
    off = edge_offset_pt
    cx = {"left": off + fw / 2, "center": pw / 2, "right": pw - off - fw / 2}[col]
    row = _ROWS[zone.value[0]]
    cy = {"top": off + fh / 2, "middle": ph / 2, "bottom": ph - off - fh / 2}[row]
    return Block(zone=zone, lines=tuple(lines), font_file=path, size_pt=size, color=hex_to_rgb(zs.color),
                 rotation=rotation, knockout=zs.knockout, width=width, height=height, center=(cx, cy))


def page_blocks(production: Production, doc: Document, page: Page, settings: Settings,
                run_date: dt.date | None = None) -> list[Block]:
    """All stamp blocks for one page, in zone order TL..BR."""
    size = page.display_size_pt
    ctx = tokens.Context(production, doc, page, run_date=run_date)
    blocks = []
    for zone in ZoneId:
        zs = settings.zones[zone]
        if not zs.enabled or not zs.template or not applies(zs.page_rule, page, doc):
            continue
        block = layout_zone(zone, zs, tokens.render(zs.template, ctx), size, settings.margin.edge_offset_in * 72)
        if block is not None:
            blocks.append(block)
    return blocks


def validate_zones(settings: Settings, production: Production) -> dict[ZoneId, list[str]]:
    """Template problems for each enabled zone, to show under that zone."""
    problems = {}
    for zone in ZoneId:
        zs = settings.zones[zone]
        if zs.enabled:
            found = tokens.validate(zs.template, production)
            if found:
                problems[zone] = found
    return problems
