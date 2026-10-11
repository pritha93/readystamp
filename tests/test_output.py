"""Milestone 5: PDF and TIFF output, load files, and the headless CLI run."""

import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

import fixtures
from readystamp.ingest.loose import load_loose
from readystamp.ingest.opt import load_opt
from readystamp.model import NumberingSettings, Settings
from readystamp.output import export as export_mod
from readystamp.output import loadfiles
from readystamp.output.export import RunBlocked, export, preflight

DELIM, QUOTE = "\x14", "\xfe"
RUN_DATE = dt.date(2026, 10, 11)


def settings(out: Path, fmt: str, **numbering) -> Settings:
    s = Settings(numbering=NumberingSettings(**({"prefix": "ABC"} | numbering)))
    s.output.format = fmt
    s.output.output_dir = out
    return s


def loose(fixture_root):
    p = load_loose([fixture_root / "loose"])
    p.remove_documents({7})            # the encrypted PDF
    return p


def good(fixture_root, with_dat=True):
    data = fixture_root / "prod_good" / "VOL001" / "DATA"
    return load_opt(data / "VOL001.opt", data / "VOL001.dat" if with_dat else None)


def read_dat(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "DAT must start with a UTF-8 BOM"
    text = raw.decode("utf-8-sig")
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    rows = []
    for line in text.split("\r\n")[:-1]:
        values = line.split(DELIM)
        assert all(v.startswith(QUOTE) and v.endswith(QUOTE) and len(v) >= 2 for v in values)
        rows.append([v[1:-1] for v in values])
    return rows


def read_opt(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "OPT must not have a BOM"
    text = raw.decode("utf-8")
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = [l.split(",") for l in text.split("\r\n")[:-1]]
    assert all(len(l) == 7 for l in lines)
    return lines


def read_xref(path: Path) -> list[dict]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "\n" not in text.replace("\r\n", "")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == loadfiles.XREF_HEADER
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def tiff_props(path: Path):
    with Image.open(path) as im:
        return im.mode, im.info.get("compression"), tuple(round(v) for v in im.info["dpi"])


def all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# --- PDF mode -----------------------------------------------------------------------------------

def test_loose_pdf_run(fixture_root, tmp_path):
    prod = loose(fixture_root)
    result = export(prod, settings(tmp_path / "out", "pdf"), RUN_DATE)
    vol = tmp_path / "out" / "VOL001"
    pdfs = sorted((vol / "PDF").iterdir(), key=lambda p: p.name)
    assert [p.name for p in pdfs] == ["ABC000001.pdf", "ABC000004.pdf", "ABC000006.pdf", "ABC000007.pdf",
                                      "ABC000009.pdf", "ABC000010.pdf"]
    assert not (vol / "DATA" / "VOL001.opt").exists()               # PDF mode writes no OPT
    assert not any(p.name.endswith(".partial") for p in all_files(tmp_path))
    with pymupdf.open(vol / "PDF" / "ABC000001.pdf") as d:
        assert d.page_count == 3
        for page, text, n in zip(d, fixtures.MEMO_PAGE_TEXT, range(1, 4)):
            assert text in page.get_text() and f"ABC00000{n}" in page.get_text()
    with pymupdf.open(vol / "PDF" / "ABC000007.pdf") as d:              # 2-frame TIFF -> 2 pages at native size
        assert d.page_count == 2
        assert d[0].rect.width == pytest.approx(612) and d[0].rect.height == pytest.approx(792)
    with pymupdf.open(vol / "PDF" / "ABC000006.pdf") as d:
        assert d[0].rotation == 90
    dat = read_dat(vol / "DATA" / "VOL001.dat")
    assert dat[0] == ["BEGBATES", "ENDBATES", "PAGECOUNT", "SOURCE_FILE", "FILEPATH"]
    assert dat[1] == ["ABC000001", "ABC000003", "3", "doc1_memo.pdf", "PDF\\ABC000001.pdf"]
    assert len(dat) == 1 + 6
    xref = read_xref(vol / "DATA" / "VOL001_xref.csv")
    assert len(xref) == 10
    assert xref[0] == {"DocNum": "1", "PageInDoc": "1", "OriginalBates": "", "NewBates": "ABC000001",
                       "SourceFile": str(fixture_root / "loose" / "doc1_memo.pdf"), "SourcePage": "1",
                       "OutputFile": "PDF\\ABC000001.pdf"}
    scan = [r for r in xref if r["SourceFile"].endswith("doc4_scan.tif")]
    assert [r["SourcePage"] for r in scan] == ["1", "2"]
    photo = [r for r in xref if r["SourceFile"].endswith("doc5_photo.jpg")]
    assert photo[0]["SourcePage"] == "0"
    assert result.pages_written == 10 and not result.failures


def test_opt_renumber_pdf_run_dat_columns(fixture_root, tmp_path):
    export(good(fixture_root), settings(tmp_path / "out", "pdf"), RUN_DATE)
    dat = read_dat(tmp_path / "out" / "VOL001" / "DATA" / "VOL001.dat")
    assert dat[0] == ["BEGBATES", "ENDBATES", "PAGECOUNT", "ORIG_BEGBATES", "ORIG_ENDBATES", "FILEPATH",
                      "SRC_BEGBATES", "SRC_ENDBATES", "CUSTODIAN", "CONFIDENTIALITY"]
    assert dat[2] == ["ABC000004", "ABC000004", "1", "REV000104", "REV000104", "PDF\\ABC000004.pdf",
                      "REV000104", "REV000104", "Zoë Adams", "Confidential"]
    assert dat[3][-1] == "Highly Confidential\xaeAttorneys' Eyes Only"   # line break written as ®


# --- TIFF mode --------------------------------------------------------------------------------

def test_loose_tiff_run(fixture_root, tmp_path):
    prod = loose(fixture_root)
    export(prod, settings(tmp_path / "out", "tiff"), RUN_DATE)
    img = tmp_path / "out" / "VOL001" / "IMAGES" / "IMG001"
    names = sorted(p.name for p in img.iterdir())
    assert names == sorted([f"ABC{n:06d}.tif" for n in range(1, 11) if n != 9] + ["ABC000009.jpg"])
    for p in img.glob("*.tif"):
        assert tiff_props(p) == ("1", "group4", (300, 300))
    with Image.open(img / "ABC000009.jpg") as im:
        assert im.format == "JPEG" and im.mode == "RGB"
    opt = read_opt(tmp_path / "out" / "VOL001" / "DATA" / "VOL001.opt")
    assert len(opt) == 10
    assert opt[0] == ["ABC000001", "VOL001", ".\\IMAGES\\IMG001\\ABC000001.tif", "Y", "", "", "3"]
    assert opt[1] == ["ABC000002", "VOL001", ".\\IMAGES\\IMG001\\ABC000002.tif", "", "", "", ""]
    assert [l[3] for l in opt].count("Y") == 6
    dat = read_dat(tmp_path / "out" / "VOL001" / "DATA" / "VOL001.dat")
    assert dat[0] == ["BEGBATES", "ENDBATES", "PAGECOUNT", "SOURCE_FILE"]


def test_keep_mode_on_good_production(fixture_root, tmp_path):
    s = settings(tmp_path / "out", "tiff", mode="keep")
    export(good(fixture_root), s, RUN_DATE)
    vol = tmp_path / "out" / "VOL001"
    names = sorted(p.stem for p in (vol / "IMAGES" / "IMG001").iterdir())
    assert names == [f"REV{n:06d}" for n in range(101, 114)]
    out_opt = read_opt(vol / "DATA" / "VOL001.opt")
    in_opt = [l.split(",") for l in (fixture_root / "prod_good" / "VOL001" / "DATA" / "VOL001.opt")
              .read_text().splitlines()]
    assert len(out_opt) == len(in_opt)
    for a, b in zip(out_opt, in_opt):        # differs from the input only in paths
        assert a[:2] + a[3:] == b[:2] + b[3:]
    dat = read_dat(vol / "DATA" / "VOL001.dat")
    assert "ORIG_BEGBATES" not in dat[0]
    xref = read_xref(vol / "DATA" / "VOL001_xref.csv")
    assert xref[0]["OriginalBates"] == "REV000101" and xref[0]["NewBates"] == "REV000101"
    assert xref[0]["SourceFile"] == "IMAGES\\IMG001\\REV000101.tif" and xref[0]["SourcePage"] == "0"
    assert all(r["OriginalBates"] and r["NewBates"] for r in xref)


def test_multipage_production_tiff(fixture_root, tmp_path):
    prod = load_opt(fixture_root / "prod_multipage" / "VOL001" / "DATA" / "VOL001.opt")
    export(prod, settings(tmp_path / "out", "tiff", prefix="MP"), RUN_DATE)
    xref = read_xref(tmp_path / "out" / "VOL001" / "DATA" / "VOL001_xref.csv")
    assert [r["SourcePage"] for r in xref] == ["1", "2", "3", "4"]
    assert len(list((tmp_path / "out" / "VOL001" / "IMAGES" / "IMG001").iterdir())) == 4


def test_folder_splitting(fixture_root, tmp_path):
    s = settings(tmp_path / "out", "tiff")
    s.output.images_per_folder = 5
    export(good(fixture_root), s, RUN_DATE)
    images = tmp_path / "out" / "VOL001" / "IMAGES"
    assert {f.name: len(list(f.iterdir())) for f in images.iterdir()} == {"IMG001": 5, "IMG002": 5, "IMG003": 3}
    opt = read_opt(tmp_path / "out" / "VOL001" / "DATA" / "VOL001.opt")
    assert opt[5][2] == ".\\IMAGES\\IMG002\\ABC000006.tif"


def test_colour_modes(fixture_root, tmp_path):
    s = settings(tmp_path / "lzw", "tiff")
    s.output.color_mode = "tiff_lzw"
    export(good(fixture_root), s, RUN_DATE)
    colour = tmp_path / "lzw" / "VOL001" / "IMAGES" / "IMG001" / "ABC000013.tif"
    assert tiff_props(colour) == ("RGB", "tiff_lzw", (300, 300))
    s = settings(tmp_path / "bw", "tiff")
    s.output.color_mode = "force_bw"
    export(good(fixture_root), s, RUN_DATE)
    for p in (tmp_path / "bw" / "VOL001" / "IMAGES" / "IMG001").iterdir():
        assert p.suffix == ".tif" and tiff_props(p)[:2] == ("1", "group4")


def test_pdf_pages_render_at_tiff_dpi(fixture_root, tmp_path):
    s = settings(tmp_path / "out", "tiff")
    s.output.tiff_dpi = 200
    export(loose(fixture_root), s, RUN_DATE)
    p = tmp_path / "out" / "VOL001" / "IMAGES" / "IMG001" / "ABC000001.tif"
    with Image.open(p) as im:
        assert im.size == (1700, 2200) and round(im.info["dpi"][0]) == 200
    with Image.open(tmp_path / "out" / "VOL001" / "IMAGES" / "IMG001" / "ABC000006.tif") as im:
        assert im.size == (2200, 1700)                         # /Rotate 90 page written as displayed


def test_dat_fields_selection(fixture_root, tmp_path):
    s = settings(tmp_path / "out", "tiff")
    s.output.dat_fields = ["begbates", "CUSTODIAN", "PAGECOUNT"]
    export(good(fixture_root), s, RUN_DATE)
    dat = read_dat(tmp_path / "out" / "VOL001" / "DATA" / "VOL001.dat")
    assert dat[0] == ["BEGBATES", "CUSTODIAN", "PAGECOUNT"]
    s2 = settings(tmp_path / "out2", "tiff")
    s2.output.dat_fields = ["NOPE"]
    assert any("Unknown DAT field" in e for e in preflight(good(fixture_root), s2).errors)


# --- preflight ----------------------------------------------------------------------------------

def test_preflight_blocks(fixture_root, tmp_path):
    broken_dir = fixture_root / "prod_broken" / "VOL001" / "DATA"
    broken = load_opt(broken_dir / "VOL001.opt", broken_dir / "VOL001.dat")
    with pytest.raises(RunBlocked, match="load report has 2 error"):
        export(broken, settings(tmp_path / "x", "tiff"))
    assert not (tmp_path / "x").exists()

    full = tmp_path / "full"
    full.mkdir()
    (full / "something.txt").write_text("x")
    assert any("not empty" in e for e in preflight(loose(fixture_root), settings(full, "pdf")).errors)

    inside = fixture_root / "loose" / "out"
    assert any("inside the input folder" in e for e in preflight(loose(fixture_root), settings(inside, "pdf")).errors)
    inside_opt = fixture_root / "prod_good" / "VOL001" / "STAMPED"
    assert any("inside the input folder" in e
               for e in preflight(good(fixture_root), settings(inside_opt, "tiff")).errors)

    s = settings(tmp_path / "y", "pdf")
    s.output.output_dir = None
    assert "Choose an output folder." in preflight(loose(fixture_root), s).errors

    s = settings(tmp_path / "z", "pdf", padding=1)
    assert any("too small" in e for e in preflight(loose(fixture_root), s).errors)

    s = settings(tmp_path / "w", "pdf")
    s.zones["TL"].enabled, s.zones["TL"].template = True, "{NOPE}"
    assert any(e.startswith("Zone TL: Unknown token") for e in preflight(loose(fixture_root), s).errors)


def test_disk_space_warning(fixture_root, tmp_path, monkeypatch):
    monkeypatch.setattr(export_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    check = preflight(loose(fixture_root), settings(tmp_path / "out", "tiff"))
    assert check.ok and check.warnings and "MB is free" in check.warnings[0]


# --- failures, cancel, inputs ------------------------------------------------------------------

def test_failing_page_fails_only_its_document(fixture_root, tmp_path, monkeypatch):
    real = export_mod.tiff_out.render_page

    def flaky(production, doc, page, *a, **k):
        if doc.seq == 3 and page.index_in_doc == 3:
            raise RuntimeError("boom")
        return real(production, doc, page, *a, **k)

    monkeypatch.setattr(export_mod.tiff_out, "render_page", flaky)
    result = export(good(fixture_root), settings(tmp_path / "out2", "tiff"), RUN_DATE)
    assert [seq for seq, _ in result.failures] == [3] and "boom" in result.failures[0][1]
    files = all_files(tmp_path / "out2")
    assert not any(p.name.endswith(".partial") for p in files)
    names = {p.stem for p in files if p.parent.name == "IMG001"}
    assert not names & {"ABC000005", "ABC000006", "ABC000007", "ABC000008"}   # doc 3 left out entirely
    opt = read_opt(tmp_path / "out2" / "VOL001" / "DATA" / "VOL001.opt")
    assert [l[0] for l in opt if l[3] == "Y"] == ["ABC000001", "ABC000004", "ABC000009", "ABC000011"]
    assert len(read_dat(tmp_path / "out2" / "VOL001" / "DATA" / "VOL001.dat")) == 1 + 4


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_cancel_stops_after_current_page(fixture_root, tmp_path, fmt):
    pages = []
    result = export(good(fixture_root), settings(tmp_path / "out", fmt), RUN_DATE,
                    progress=lambda done, total, seq: pages.append(done),
                    should_cancel=lambda: len(pages) >= 5)       # page 5 is in the middle of document 3
    assert result.cancelled and pages[-1] == 5
    assert [od.doc.seq for od in result.documents] == [1, 2]
    files = all_files(tmp_path / "out")
    assert not any(p.name.endswith(".partial") for p in files)
    assert len(read_xref(tmp_path / "out" / "VOL001" / "DATA" / "VOL001_xref.csv")) == 4


def test_inputs_are_never_modified(fixture_root, tmp_path):
    files = all_files(fixture_root)
    before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    export(good(fixture_root), settings(tmp_path / "a", "tiff"), RUN_DATE)
    export(loose(fixture_root), settings(tmp_path / "b", "pdf"), RUN_DATE)
    assert before == {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in all_files(fixture_root)}


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_run(fixture_root, tmp_path):
    from typer.testing import CliRunner

    from readystamp.cli import app
    from readystamp.presets import save_preset

    preset = tmp_path / "preset.json"
    save_preset(Settings(numbering=NumberingSettings(prefix="CLI")), preset)
    assert "output_dir" not in json.loads(preset.read_text())["output"]
    data = fixture_root / "prod_good" / "VOL001" / "DATA"
    runner = CliRunner()
    r = runner.invoke(app, ["run", "-i", str(data / "VOL001.opt"), "--dat", str(data / "VOL001.dat"),
                            "-p", str(preset), "-o", str(tmp_path / "out"), "-f", "tiff"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "out" / "VOL001" / "IMAGES" / "IMG001" / "CLI000001.tif").exists()

    r = runner.invoke(app, ["run", "-i", str(fixture_root / "loose"), "-o", str(tmp_path / "out2")])
    assert r.exit_code == 2 and "load report has 1 error" in r.output
    r = runner.invoke(app, ["run", "-i", str(fixture_root / "loose"), "-o", str(tmp_path / "out2"), "--skip-errors"])
    assert r.exit_code == 0, r.output
    assert "Left out doc10_locked.pdf" in r.output

    bad = tmp_path / "bad.json"
    bad.write_text('{"zones": {"BR": {"colour": "red"}}}')
    r = runner.invoke(app, ["run", "-i", str(fixture_root / "loose"), "-o", str(tmp_path / "o3"), "-p", str(bad)])
    assert r.exit_code == 2 and "Unknown setting 'colour' in zones → BR" in r.output
