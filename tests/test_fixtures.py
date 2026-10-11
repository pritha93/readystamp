"""Milestone 1: every fixture generates with the properties later tests rely on."""

import hashlib
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

import fixtures


def _tiff_info(path: Path) -> tuple[str, str, tuple, int]:
    with Image.open(path) as im:
        return im.mode, im.info.get("compression"), tuple(round(v) for v in im.info["dpi"]), im.n_frames


# --- loose ------------------------------------------------------------------

def test_loose_contains_expected_files(fixture_root):
    names = sorted(p.name for p in (fixture_root / "loose").iterdir())
    assert names == sorted(n for n, _ in fixtures.LOOSE_FILES)


def test_memo_pdf_has_text_layer(fixture_root):
    with pymupdf.open(fixture_root / "loose" / "doc1_memo.pdf") as doc:
        assert doc.page_count == 3
        for page, expected in zip(doc, fixtures.MEMO_PAGE_TEXT):
            assert expected in page.get_text()
            assert page.rect.width < page.rect.height


def test_mixed_pdf_is_portrait_then_landscape(fixture_root):
    with pymupdf.open(fixture_root / "loose" / "doc2_mixed.pdf") as doc:
        assert doc.page_count == 2
        assert doc[0].rect.width < doc[0].rect.height
        assert doc[1].rect.width > doc[1].rect.height


def test_rotated_pdf_has_rotate_90(fixture_root):
    with pymupdf.open(fixture_root / "loose" / "doc3_rotated.pdf") as doc:
        page = doc[0]
        assert page.rotation == 90
        assert page.mediabox.width < page.mediabox.height   # portrait media box...
        assert page.rect.width > page.rect.height           # ...displayed as landscape


def test_loose_tiff_is_two_frame_group4(fixture_root):
    assert _tiff_info(fixture_root / "loose" / "doc4_scan.tif") == ("1", "group4", (300, 300), 2)


def test_loose_jpg_is_colour_and_png_is_grey_without_dpi(fixture_root):
    with Image.open(fixture_root / "loose" / "doc5_photo.jpg") as im:
        assert im.mode == "RGB"
    with Image.open(fixture_root / "loose" / "doc6_diagram.png") as im:
        assert im.mode == "L"
        assert "dpi" not in im.info


def test_locked_pdf_is_encrypted(fixture_root):
    with pymupdf.open(fixture_root / "loose" / "doc10_locked.pdf") as doc:
        assert doc.needs_pass


# --- productions --------------------------------------------------------------

def _opt_lines(prod: Path) -> list[list[str]]:
    raw = (prod / "VOL001" / "DATA" / "VOL001.opt").read_bytes()
    assert raw.endswith(b"\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    return [line.split(",") for line in raw.decode("utf-8").split("\r\n") if line]


def test_good_production_images(fixture_root):
    img_dir = fixture_root / "prod_good" / "VOL001" / "IMAGES" / "IMG001"
    tifs = sorted(img_dir.glob("*.tif"))
    assert [p.stem for p in tifs] == [f"REV{n:06d}" for n in range(101, 113)]
    for p in tifs:
        assert _tiff_info(p) == ("1", "group4", (300, 300), 1)
    with Image.open(img_dir / "REV000113.jpg") as im:
        assert im.mode == "RGB"


def test_good_opt_has_five_documents_with_backslash_paths(fixture_root):
    lines = _opt_lines(fixture_root / "prod_good")
    assert len(lines) == 13
    assert all(len(f) == 7 for f in lines)
    assert sum(f[3] == "Y" for f in lines) == 5
    assert all(f[2].startswith(".\\IMAGES\\IMG001\\") and "/" not in f[2] for f in lines)
    vol = fixture_root / "prod_good" / "VOL001"
    for f in lines:   # every path resolves against the volume folder
        assert (vol / f[2][2:].replace("\\", "/")).exists()


def test_good_dat_is_concordance_format(fixture_root):
    raw = (fixture_root / "prod_good" / "VOL001" / "DATA" / "VOL001.dat").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    rows = [r for r in text.split("\r\n") if r]
    assert len(rows) == 1 + 5
    header = [v.strip("\xfe") for v in rows[0].split("\x14")]
    assert header == ["BEGBATES", "ENDBATES", "CUSTODIAN", "CONFIDENTIALITY"]
    assert all(v.startswith("\xfe") and v.endswith("\xfe") for r in rows for v in r.split("\x14"))
    assert "\xae" in text and "\n" not in text.replace("\r\n", "")
    assert "Zoë" in text


def test_broken_production_defects(fixture_root):
    vol = fixture_root / "prod_broken" / "VOL001"
    img_dir = vol / "IMAGES" / "IMG001"
    on_disk = {p.name for p in img_dir.iterdir()}
    assert "REV000105.tif" not in on_disk
    assert "rev000106.TIF" in on_disk and "REV000106.tif" not in on_disk
    lines = _opt_lines(fixture_root / "prod_broken")
    keys = [f[0] for f in lines]
    assert keys.count("REV000110") == 2                       # duplicate key
    assert "REV000111" not in keys                            # numbering gap
    assert lines[3][3] == "Y" and lines[3][6] == "2" and lines[4][3] == "Y"   # page count mismatch
    # nothing on disk is left unreferenced (no stray info-level issues)
    referenced = {f[2].rsplit("\\", 1)[1].lower() for f in lines}
    assert {n.lower() for n in on_disk} <= referenced
    dat = (vol / "DATA" / "VOL001.dat").read_text(encoding="utf-8-sig")
    assert "\xfeREV000999\xfe" in dat


def test_multipage_production(fixture_root):
    vol = fixture_root / "prod_multipage" / "VOL001"
    assert _tiff_info(vol / "IMAGES" / "IMG001" / fixtures.MULTIPAGE_FILE)[3] == 4
    lines = _opt_lines(fixture_root / "prod_multipage")
    assert len({f[2] for f in lines}) == 1 and len(lines) == 4


# --- generator behaviour --------------------------------------------------------

def _digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_generation_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    fixtures.generate_all(a)
    fixtures.generate_all(b)
    da, db = _digests(a), _digests(b)
    da.pop("loose/doc10_locked.pdf")   # AES encryption uses a random IV
    db.pop("loose/doc10_locked.pdf")
    assert da == db


def test_regenerating_replaces_existing_fixtures(tmp_path):
    fixtures.generate_all(tmp_path)
    stray = tmp_path / "prod_good" / "stray.txt"
    stray.write_text("x")
    fixtures.generate_all(tmp_path)
    assert not stray.exists()


def test_cli_make_fixtures(tmp_path):
    from typer.testing import CliRunner

    from readystamp.cli import app

    result = CliRunner().invoke(app, ["make-fixtures", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "prod_good" / "VOL001" / "DATA" / "VOL001.opt").exists()
