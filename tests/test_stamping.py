"""Milestone 3: stamping engine, page rules, margins and preview."""

import hashlib
import io
import os
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageChops

import fixtures
from helpers import make_production
from readystamp import numbering
from readystamp.ingest.loose import load_loose
from readystamp.model import MarginSettings, NumberingSettings, Settings, ZoneId, ZoneSettings
from readystamp.stamping import image_stamp, layout, pdf_stamp
from readystamp.stamping.preview import PreviewError, render_preview

GOLDEN = Path(__file__).parent / "golden"
DPI = 100
OFFSET_PX = 25           # 0.25 in at 100 DPI
DOCS = {"memo": 1, "mixed": 2, "rotated": 3, "scan": 4, "photo": 5, "diagram": 6}


@pytest.fixture(scope="module")
def prod(fixture_root):
    p = load_loose([fixture_root / "loose"])
    p.remove_documents({7})            # encrypted PDF
    numbering.apply(p, NumberingSettings(prefix="ABC"))
    return p


def only(zone: ZoneId, fmt: str = "pdf", **zone_kw) -> Settings:
    s = Settings.model_validate({"zones": {z.value: {"enabled": False} for z in ZoneId}})
    s.zones[zone] = ZoneSettings(enabled=True, **({"template": "{BATES}"} | zone_kw))
    s.output.format = fmt
    return s


def none_enabled(fmt: str) -> Settings:
    s = Settings.model_validate({"zones": {z.value: {"enabled": False} for z in ZoneId}})
    s.output.format = fmt
    return s


def png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def stamp_pixels(prod, settings, reference, doc, page=1):
    a = png(render_preview(prod, settings, doc, page, DPI))
    b = png(render_preview(prod, reference, doc, page, DPI))
    assert a.size == b.size
    return a, ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 0 else 0)


# --- acceptance: BR placement ------------------------------------------------------

@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
@pytest.mark.parametrize("name", list(DOCS))
def test_bottom_right_only_lands_in_bottom_right_at_edge_offset(prod, fmt, name):
    page_img, diff = stamp_pixels(prod, only(ZoneId.BR, fmt), none_enabled(fmt), DOCS[name])
    bbox = diff.getbbox()
    assert bbox is not None, "nothing was stamped"
    w, h = page_img.size                         # displayed size, so /Rotate 90 is landscape here
    if name == "rotated":
        assert w > h
    x0, y0, x1, y1 = bbox
    assert x0 >= w / 2 and y0 >= h / 2          # inside the bottom-right quarter
    assert abs((w - x1) - OFFSET_PX) <= 2, (w - x1)
    assert abs((h - y1) - OFFSET_PX) <= 2, (h - y1)


# --- every zone sits where it should ---------------------------------------------------

@pytest.mark.parametrize("zone", list(ZoneId))
def test_zone_geometry(zone):
    zs = ZoneSettings(enabled=True, template="ABC000001")
    b = layout.layout_zone(zone, zs, "ABC000001", (612, 792), 18)
    x0, y0, x1, y1 = b.bounds()
    col, row = zone.value[1], zone.value[0]
    if col == "L":
        assert x0 == pytest.approx(18)
    elif col == "R":
        assert x1 == pytest.approx(612 - 18)
    else:
        assert (x0 + x1) / 2 == pytest.approx(306)
    if row == "T":
        assert y0 == pytest.approx(18)
    elif row == "B":
        assert y1 == pytest.approx(792 - 18)
    else:
        assert (y0 + y1) / 2 == pytest.approx(396)


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_side_zones_run_along_the_edge(rotation):
    for zone, edge in ((ZoneId.ML, "left"), (ZoneId.MR, "right")):
        b = layout.layout_zone(zone, ZoneSettings(rotation=rotation), "CONFIDENTIAL", (612, 792), 18)
        x0, y0, x1, y1 = b.bounds()
        assert (y1 - y0) > 3 * (x1 - x0)                   # runs vertically
        assert (x0 if edge == "left" else 612 - x1) == pytest.approx(18)
        assert (y0 + y1) / 2 == pytest.approx(396)


def test_rotation_direction():
    b90 = layout.layout_zone(ZoneId.ML, ZoneSettings(rotation=90), "AB", (612, 792), 18)
    start, end = b90.to_page(0, 0), b90.to_page(b90.width, 0)
    assert end[1] < start[1]                               # 90 reads bottom to top
    b270 = layout.layout_zone(ZoneId.MR, ZoneSettings(rotation=270), "AB", (612, 792), 18)
    start, end = b270.to_page(0, 0), b270.to_page(b270.width, 0)
    assert end[1] > start[1]                               # 270 reads top to bottom


def test_watermark_45_is_centred_and_rises_left_to_right():
    b = layout.layout_zone(ZoneId.MC, ZoneSettings(rotation=45, size_pt=72), "DRAFT", (612, 792), 18)
    assert b.center == (306, 396)
    start, end = b.to_page(0, b.height), b.to_page(b.width, b.height)
    assert end[0] > start[0] and end[1] < start[1]


def test_multiline_stacks_at_1_2_line_height():
    b = layout.layout_zone(ZoneId.TC, ZoneSettings(size_pt=10), "LINE ONE\nLINE TWO\nLINE 3", (612, 792), 18)
    assert [round(l.y - b.lines[0].y, 6) for l in b.lines] == [0, 12, 24]
    centres = [l.x + (layout.ink_bbox(l.text, b.font_file, 10)[0] + layout.ink_bbox(l.text, b.font_file, 10)[2]) / 2
               for l in b.lines]
    assert max(centres) - min(centres) < 0.01              # centre-aligned


def test_blank_template_lines_make_no_block():
    assert layout.layout_zone(ZoneId.BR, ZoneSettings(), "  \n ", (612, 792), 18) is None


# --- page rules ------------------------------------------------------------------------

def test_page_rules():
    p = make_production([3])
    doc = p.documents[0]
    doc.pages[2].width_pt, doc.pages[2].height_pt = 792, 612     # page 3 landscape
    pick = lambda rule: [pg.index_in_doc for pg in doc.pages if layout.applies(rule, pg, doc)]
    assert pick("all") == [1, 2, 3]
    assert pick("first") == [1]
    assert pick("last") == [3]
    assert pick("odd") == [1, 3]
    assert pick("even") == [2]
    assert pick("portrait") == [1, 2]
    assert pick("landscape") == [3]


def test_page_rule_controls_stamping(prod):
    s = only(ZoneId.TR, template="FIRST ONLY", page_rule="first")
    blocks = [layout.page_blocks(prod, prod.documents[0], pg, s) for pg in prod.documents[0].pages]
    assert [len(b) for b in blocks] == [1, 0, 0]


# --- PDF text layer -----------------------------------------------------------------------

def test_stamped_pdf_keeps_text_and_bates_is_extractable(prod):
    s = Settings()
    s.zones[ZoneId.MC] = ZoneSettings(enabled=True, template="DRAFT", rotation=45, size_pt=60)
    memo = prod.documents[0]
    with pymupdf.open() as out, pdf_stamp.SourceCache() as src:
        for pg in memo.pages:
            pdf_stamp.stamp_page(out, src, prod, memo, pg, s)
        data = out.tobytes()
    with pymupdf.open(stream=data) as reread:
        for page, text, pg in zip(reread, fixtures.MEMO_PAGE_TEXT, memo.pages):
            got = page.get_text()
            assert text in got
            assert pg.new_bates in got and "CONFIDENTIAL" in got and "DRAFT" in got


def test_rotated_pdf_keeps_its_rotation_and_text(prod):
    doc = prod.documents[DOCS["rotated"] - 1]
    with pymupdf.open() as out, pdf_stamp.SourceCache() as src:
        page = pdf_stamp.stamp_page(out, src, prod, doc, doc.pages[0], Settings())
        assert page.rotation == 90
        words = page.get_text("words")
        bates = [w for w in words if w[4] == doc.pages[0].new_bates]
        assert bates
        # In displayed coordinates the stamp is at the bottom right of the landscape page.
        r = pymupdf.Rect(bates[0][:4]) * page.rotation_matrix
        assert r.x0 > page.rect.width / 2 and r.y0 > page.rect.height / 2


def test_add_band_pdf_keeps_text(prod):
    s = Settings(margin=MarginSettings(mode="add_band"))
    memo = prod.documents[0]
    with pymupdf.open() as out, pdf_stamp.SourceCache() as src:
        page = pdf_stamp.stamp_page(out, src, prod, memo, memo.pages[0], s)
        assert page.rect == pymupdf.Rect(0, 0, 612, 792)        # page size unchanged
        assert fixtures.MEMO_PAGE_TEXT[0] in page.get_text()


# --- add-band mode ------------------------------------------------------------------------

def test_band_layout():
    s = only(ZoneId.BR)
    scale, top, bottom = layout.band_layout(Settings(margin=MarginSettings(mode="add_band"), zones=s.zones), (612, 792))
    assert (top, bottom) == (0, 36) and scale == pytest.approx(756 / 792)
    assert layout.band_layout(Settings(), (612, 792)) is None


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
@pytest.mark.parametrize("name", ["memo", "rotated", "scan", "photo"])
def test_add_band_stamps_never_touch_content(prod, fmt, name):
    def settings(text):
        s = only(ZoneId.BR, fmt, template=text)
        s.zones[ZoneId.TC] = ZoneSettings(enabled=True, template=text and "TOP LEGEND\n{BATES}", size_pt=9)
        s.zones[ZoneId.BL] = ZoneSettings(enabled=True, template=text and "CONFIDENTIAL")
        s.margin = MarginSettings(mode="add_band")
        return s

    img, diff = stamp_pixels(prod, settings("{BATES}"), settings(""), DOCS[name])
    assert diff.getbbox() is not None
    page = prod.documents[DOCS[name] - 1].pages[0]
    x0, y0, x1, y1 = layout.content_rect(settings(""), page.display_size_pt)
    k = DPI / 72
    content = diff.crop((int(x0 * k), int(y0 * k), int(x1 * k) + 1, int(y1 * k) + 1))
    assert content.getbbox() is None, "a stamp pixel overlaps the scaled page content"


# --- knockout, colour ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def black_page(tmp_path_factory):
    d = tmp_path_factory.mktemp("black")
    Image.new("L", (850, 1100), 0).save(d / "black.png", dpi=(100, 100))
    p = load_loose([d])
    numbering.apply(p, NumberingSettings(prefix="K"))
    return p


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_knockout_paints_white_behind_text(black_page, fmt):
    s = only(ZoneId.BR, fmt, knockout=True, size_pt=20)
    img = png(render_preview(black_page, s, 1, 1, DPI)).convert("L")
    page = black_page.documents[0].pages[0]
    block = layout.page_blocks(black_page, black_page.documents[0], page, s)[0]
    bx0, by0, bx1, by1 = (v * DPI / 72 for v in block.bounds(block.knockout_pad))
    corner = img.getpixel((int(bx0) + 2, int(by0) + 2))
    outside = img.getpixel((int(bx0) - 6, int(by0) - 6))
    assert corner > 200 and outside < 50
    plain = png(render_preview(black_page, only(ZoneId.BR, fmt, size_pt=20), 1, 1, DPI)).convert("L")
    assert plain.getpixel((int(bx0) + 2, int(by0) + 2)) < 50


def _red_pixels(img: Image.Image) -> int:
    r, g, b = img.split()
    red = ImageChops.subtract(r, g).point(lambda v: 255 if v > 100 else 0)
    return red.histogram()[255]


def test_colour_is_kept_on_colour_pages_and_forced_black_on_bw(prod):
    def added_red(settings, doc):
        base = settings.model_copy(deep=True)
        base.zones[ZoneId.MC] = ZoneSettings()
        return _red_pixels(png(render_preview(prod, settings, doc, 1, DPI))) - \
            _red_pixels(png(render_preview(prod, base, doc, 1, DPI)))

    s = only(ZoneId.MC, "tiff", template="RED", color="#FF0000", size_pt=60)
    assert added_red(s, DOCS["photo"]) > 500                # colour page keeps the zone colour
    assert added_red(only(ZoneId.MC, "tiff", template="RED", size_pt=60), DOCS["photo"]) <= 0
    assert added_red(s, DOCS["scan"]) == 0                  # black-and-white page: forced black
    s.output.color_mode = "force_bw"
    assert _red_pixels(png(render_preview(prod, s, DOCS["photo"], 1, DPI))) == 0
    s.output.format = "pdf"
    assert added_red(s, DOCS["memo"]) > 500                 # PDF output keeps colour


def test_tiff_kind_and_mode(prod):
    s = Settings()
    s.output.format = "tiff"
    expected = {"memo": ("bw", "1"), "scan": ("bw", "1"), "diagram": ("bw", "1"), "photo": ("color", "RGB")}
    with pdf_stamp.SourceCache() as src:
        for name, (kind, mode) in expected.items():
            doc = prod.documents[DOCS[name] - 1]
            img, dpi, got = image_stamp.stamp_for_tiff(prod, doc, doc.pages[0], s, src)
            assert (got, img.mode, dpi) == (kind, mode, 300)


def test_colour_detection():
    assert not image_stamp.is_color_image(Image.new("RGB", (10, 10), (100, 110, 120)))
    im = Image.new("RGB", (10, 10), (255, 255, 255))
    im.putpixel((3, 3), (200, 170, 200))
    assert image_stamp.is_color_image(im)
    assert not image_stamp.is_color_image(Image.new("L", (5, 5), 0))


# --- preview -----------------------------------------------------------------------------------

def test_preview_uses_the_output_stamping_function(prod):
    s = Settings()
    memo = prod.documents[0]
    with pymupdf.open() as out, pdf_stamp.SourceCache() as src:
        direct = pdf_stamp.render_png(pdf_stamp.stamp_page(out, src, prod, memo, memo.pages[1], s), DPI)
    assert render_preview(prod, s, 1, 2, DPI) == direct


def test_preview_size_follows_dpi(prod):
    assert png(render_preview(prod, Settings(), 1, 1, 100)).size == (850, 1100)
    s = Settings()
    s.output.format = "tiff"
    assert png(render_preview(prod, s, DOCS["scan"], 2, 100)).size == (850, 1100)


def test_preview_errors(prod):
    with pytest.raises(PreviewError, match="no document 99"):
        render_preview(prod, Settings(), 99, 1)
    with pytest.raises(PreviewError, match="no page 4"):
        render_preview(prod, Settings(), 1, 4)


def test_zone_template_errors_are_reported_per_zone(prod):
    s = Settings()
    s.zones[ZoneId.TL] = ZoneSettings(enabled=True, template="{NOPE}")
    s.zones[ZoneId.TR] = ZoneSettings(enabled=False, template="{NOPE}")
    problems = layout.validate_zones(s, prod)
    assert list(problems) == [ZoneId.TL] and "Unknown token" in problems[ZoneId.TL][0]


def test_stamping_does_not_modify_inputs(prod, fixture_root):
    files = sorted((fixture_root / "loose").iterdir())
    before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    for fmt in ("pdf", "tiff"):
        s = Settings()
        s.output.format = fmt
        for d in prod.documents:
            render_preview(prod, s, d.seq, 1, 50)
    assert before == [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]


# --- golden images -------------------------------------------------------------------------------

def golden_settings(fmt: str, band: bool = False) -> Settings:
    s = Settings()
    s.zones[ZoneId.TC] = ZoneSettings(enabled=True, template="PRODUCED IN LITIGATION\nPage {PAGE} of {PAGES}",
                                      size_pt=9, bold=True, knockout=True)
    s.zones[ZoneId.MC] = ZoneSettings(enabled=True, template="DRAFT", rotation=45, size_pt=72, color="#C00000")
    s.zones[ZoneId.ML] = ZoneSettings(enabled=True, template="{FILENAME}", rotation=90, font="mono", size_pt=8)
    s.zones[ZoneId.MR] = ZoneSettings(enabled=True, template="Doc {DOCNUM}", rotation=270, font="serif")
    s.output.format = fmt
    if band:
        s.margin = MarginSettings(mode="add_band")
    return s


GOLDEN_CASES = {
    "memo_pdf": ("pdf", False, "memo", 1),
    "rotated_pdf": ("pdf", False, "rotated", 1),
    "mixed_landscape_pdf": ("pdf", False, "mixed", 2),
    "photo_pdf": ("pdf", False, "photo", 1),
    "memo_band_pdf": ("pdf", True, "memo", 1),
    "memo_tiff": ("tiff", False, "memo", 1),
    "scan_tiff": ("tiff", False, "scan", 2),
    "photo_tiff": ("tiff", False, "photo", 1),
    "rotated_band_tiff": ("tiff", True, "rotated", 1),
}


@pytest.mark.parametrize("case", list(GOLDEN_CASES))
def test_golden_images(prod, case):
    fmt, band, name, page = GOLDEN_CASES[case]
    import datetime as dt
    got = png(render_preview(prod, golden_settings(fmt, band), DOCS[name], page, DPI, run_date=dt.date(2026, 1, 1)))
    path = GOLDEN / f"{case}.png"
    if os.environ.get("UPDATE_GOLDEN"):
        got.save(path)
    assert path.exists(), f"Missing golden image {path.name}; run with UPDATE_GOLDEN=1 to create it."
    want = Image.open(path).convert("RGB")
    assert got.size == want.size
    diff = ImageChops.difference(got, want).convert("L").point(lambda v: 255 if v > 32 else 0)
    differing = diff.histogram()[255] / (got.width * got.height)
    assert differing <= 0.005, f"{differing:.2%} of pixels differ from {path.name}"
