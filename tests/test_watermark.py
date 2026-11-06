"""Watermarks behind the page, and stamp opacity."""

import io

import pymupdf
import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError

from readystamp import numbering
from readystamp.ingest.loose import load_loose
from readystamp.model import NumberingSettings, Settings, ZoneId, ZoneSettings
from readystamp.stamping import layout
from readystamp.stamping.preview import render_preview

DPI = 100
WATERMARK = dict(template="DRAFT", rotation=45, size_pt=96, bold=True, color="#FF0000")


def settings(fmt: str, **zones: dict) -> Settings:
    s = Settings.model_validate({"zones": {z.value: {"enabled": False} for z in ZoneId}})
    for zone, kw in zones.items():
        s.zones[ZoneId(zone)] = ZoneSettings(enabled=True, **kw)
    s.output.format = fmt
    return s


def png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def red_pixels(img: Image.Image) -> int:
    r, g, _ = img.split()
    return ImageChops.subtract(r, g).point(lambda v: 255 if v > 60 else 0).histogram()[255]


def rgb(img: Image.Image) -> list[tuple[int, int, int]]:
    raw = img.convert("RGB").tobytes()
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


def dark_pixels(img: Image.Image) -> int:
    return img.convert("L").point(lambda v: 255 if v < 100 else 0).histogram()[255]


def production(tmp_path_factory, name: str, image: Image.Image):
    d = tmp_path_factory.mktemp(name)
    image.save(d / f"{name}.png", dpi=(100, 100))
    p = load_loose([d])
    numbering.apply(p, NumberingSettings(prefix="W"))
    return p


@pytest.fixture(scope="module")
def black_page(tmp_path_factory):
    im = Image.new("RGB", (850, 1100), (0, 0, 0))
    im.putpixel((0, 0), (0, 0, 255))          # one coloured pixel, so it stays a colour page
    return production(tmp_path_factory, "black", im)


@pytest.fixture(scope="module")
def white_page(tmp_path_factory):
    im = Image.new("RGB", (850, 1100), (255, 255, 255))
    im.putpixel((0, 0), (0, 0, 255))
    return production(tmp_path_factory, "white", im)


@pytest.fixture(scope="module")
def white_bw_page(tmp_path_factory):
    return production(tmp_path_factory, "whitebw", Image.new("L", (850, 1100), 255))


@pytest.fixture(scope="module")
def clear_page(tmp_path_factory):
    return production(tmp_path_factory, "clear", Image.new("RGBA", (850, 1100), (0, 0, 0, 0)))


def preview(prod, s):
    return png(render_preview(prod, s, 1, 1, DPI))


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_behind_never_covers_dark_content(black_page, white_page, fmt):
    on_top = settings(fmt, MC=WATERMARK)
    behind = settings(fmt, MC=WATERMARK | {"behind": True})
    assert red_pixels(preview(black_page, on_top)) > 2000         # drawn over the black page
    assert red_pixels(preview(black_page, behind)) == 0           # hidden under it
    assert red_pixels(preview(white_page, behind)) > 2000         # shows through white paper


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_opacity_lightens_the_stamp(white_page, black_page, fmt):
    for extra in ({}, {"behind": True}):
        solid = preview(white_page, settings(fmt, MC=WATERMARK | extra))
        light = preview(white_page, settings(fmt, MC=WATERMARK | extra | {"opacity": 0.4}))
        # Pure red ink at 40% on white is about (255, 153, 153).
        assert solid.getchannel("G").getextrema()[0] < 30
        greens = [g for r, g, _ in rgb(light) if r - g > 60]
        assert greens and min(greens) == pytest.approx(153, abs=8)
    # On top of dark content a translucent stamp still shows, but dimmer.
    dim = preview(black_page, settings(fmt, MC=WATERMARK | {"opacity": 0.4}))
    assert dim.getchannel("R").getextrema()[1] == pytest.approx(102, abs=8)


def test_bw_tiff_dithers_a_light_watermark(white_bw_page):
    def dots(**kw):
        return dark_pixels(preview(white_bw_page, settings("tiff", MC=WATERMARK | kw)))
    solid, light = dots(behind=True), dots(behind=True, opacity=0.35)
    assert 0.15 * solid < light < 0.6 * solid               # lighter, but not thresholded away
    assert dots(opacity=0.35) == pytest.approx(light, rel=0.1)


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_front_white_box_still_covers_a_watermark_behind(white_page, black_page, fmt):
    # A behind watermark must not change how later, front stamps are drawn (no leaked blend mode).
    s = settings(fmt, MC=WATERMARK | {"behind": True, "opacity": 0.5},
                 BR={"template": "{BATES}", "size_pt": 20, "knockout": True})
    img = preview(black_page, s)
    block = layout.page_blocks(black_page, black_page.documents[0], black_page.documents[0].pages[0], s)[-1]
    x0, y0, _, _ = (v * DPI / 72 for v in block.bounds(block.knockout_pad))
    assert img.convert("L").getpixel((int(x0) + 2, int(y0) + 2)) > 200


def test_transparent_image_areas_count_as_white_paper(clear_page):
    s = settings("tiff", MC=WATERMARK | {"behind": True})
    assert red_pixels(preview(clear_page, s)) > 2000


def test_behind_on_a_text_pdf_keeps_its_text(tmp_path):
    src = tmp_path / "memo.pdf"
    with pymupdf.open() as d:
        page = d.new_page()
        page.draw_rect(page.rect, fill=(1, 1, 1), color=None)    # an opaque white background
        page.insert_text((72, 400), "Quarterly memo body text", fontsize=24)
        d.save(src)
    prod = load_loose([src])
    numbering.apply(prod, NumberingSettings(prefix="T"))
    s = settings("pdf", MC=WATERMARK | {"behind": True, "opacity": 0.3})
    from readystamp.stamping import pdf_stamp
    with pymupdf.open() as out, pdf_stamp.SourceCache() as srcs:
        doc = prod.documents[0]
        stamped = pdf_stamp.stamp_page(out, srcs, prod, doc, doc.pages[0], s)
        text = stamped.get_text()
        img = Image.open(io.BytesIO(pdf_stamp.render_png(stamped, DPI))).convert("RGB")
        reopened = pymupdf.open("pdf", out.tobytes())
        assert reopened[0].get_text() == text
    assert "Quarterly memo body text" in text and "DRAFT" in text
    assert red_pixels(img) > 1000                       # visible through the white background


def test_settings():
    mc = Settings().zones[ZoneId.MC]
    assert (mc.enabled, mc.behind, mc.opacity, mc.rotation) == (False, True, 0.35, 45)
    assert ZoneSettings().opacity == 1 and not ZoneSettings().behind      # other zones unchanged
    with pytest.raises(ValidationError):
        ZoneSettings(opacity=0)
    with pytest.raises(ValidationError):
        ZoneSettings(opacity=1.5)
    old_preset = {"zones": {"MC": {"enabled": True, "template": "DRAFT", "rotation": 45}}}
    loaded = Settings.model_validate(old_preset).zones[ZoneId.MC]
    assert (loaded.behind, loaded.opacity) == (False, 1)                  # older presets look the same
