"""Milestone 1: bundled fonts load in both Pillow and PyMuPDF."""

import pymupdf
import pytest
from PIL import ImageFont

from readystamp.fonts import FONT_DIR, all_font_paths, font_path


def test_six_fonts_and_licence_are_bundled():
    paths = all_font_paths()
    assert len(paths) == 6 and all(p.exists() for p in paths)
    assert (FONT_DIR / "LICENSE-Liberation.txt").exists()


@pytest.mark.parametrize("path", all_font_paths(), ids=lambda p: p.name)
def test_font_loads_in_pillow_and_pymupdf(path):
    ImageFont.truetype(str(path), 10)
    font = pymupdf.Font(fontfile=str(path))
    assert font.name.startswith("Liberation")
    assert font.text_length("ABC000001", fontsize=10) > 0


def test_font_path_lookup():
    assert font_path("mono", bold=True).name == "LiberationMono-Bold.ttf"
    with pytest.raises(ValueError, match="Unknown font"):
        font_path("comic")
