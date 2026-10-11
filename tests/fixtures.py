"""Generate all ReadyStamp test data, so no real client documents are ever needed.

Usage:
    python tests/fixtures.py OUTPUT_DIR
    readystamp make-fixtures OUTPUT_DIR

Layout produced under OUTPUT_DIR:

    loose/            mixed loose files (PDF, TIFF, JPG, PNG, encrypted PDF, .txt)
    prod_good/        a clean OPT + DAT production, 5 documents, 13 pages
    prod_broken/      the good production with deliberate defects (see BROKEN_EXPECTED_ISSUES)
    prod_multipage/   an OPT whose lines point to frames 1-4 of one multi-page TIFF

Productions use the standard volume layout that ReadyStamp also writes:
    <prod>/VOL001/IMAGES/IMG001/<key>.tif
    <prod>/VOL001/DATA/VOL001.opt     (paths relative to the volume folder: .\\IMAGES\\...)
    <prod>/VOL001/DATA/VOL001.dat
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "readystamp" / "fonts" / "LiberationSans-Regular.ttf"
FONT_BOLD = ROOT / "readystamp" / "fonts" / "LiberationSans-Bold.ttf"

DPI = 300
LETTER_PX = (int(8.5 * DPI), int(11 * DPI))      # 2550 x 3300
LETTER_PT = (612.0, 792.0)

# Concordance DAT characters
DAT_DELIM = "\x14"   # ¶ (ASCII 20)
DAT_QUOTE = "\xfe"   # þ (ASCII 254)
DAT_NEWLINE = "\xae"  # ® (ASCII 174)

# ---------------------------------------------------------------------------
# Expected contents, used by tests in later milestones
# ---------------------------------------------------------------------------

#: Loose files in natural-sort order, with the page count each should ingest as.
#: None = rejected or ignored at ingest.
LOOSE_FILES = [
    ("doc1_memo.pdf", 3),          # 3-page portrait PDF with a real text layer
    ("doc2_mixed.pdf", 2),         # portrait + landscape
    ("doc3_rotated.pdf", 1),       # /Rotate 90
    ("doc4_scan.tif", 2),          # 2-frame 1-bit Group IV TIFF
    ("doc5_photo.jpg", 1),         # colour JPG
    ("doc6_diagram.png", 1),       # greyscale PNG with no DPI tag (ingest should assume 300)
    ("doc10_locked.pdf", None),    # encrypted PDF -> load-report error; sorts after doc6
    ("readme.txt", None),          # unsupported type -> load-report info
]

#: Text placed on each page of doc1_memo.pdf, for text-layer preservation tests.
MEMO_PAGE_TEXT = [f"Memorandum page {n} of 3. The quick brown fox jumps over the lazy dog." for n in (1, 2, 3)]


@dataclass(frozen=True)
class OptLine:
    key: str
    file: str            # file name inside IMAGES/IMG001
    doc_break: bool
    page_count: int | None = None


def _doc(keys: list[str], ext: dict[str, str] | None = None, page_count: int | None = -1) -> list[OptLine]:
    ext = ext or {}
    count = len(keys) if page_count == -1 else page_count
    return [
        OptLine(k, f"{k}.{ext.get(k, 'tif')}", i == 0, count if i == 0 else None)
        for i, k in enumerate(keys)
    ]


def _keys(start: int, end: int, prefix: str = "REV") -> list[str]:
    return [f"{prefix}{n:06d}" for n in range(start, end + 1)]


#: prod_good: 5 documents, 13 pages; REV000113 is a colour JPG.
GOOD_OPT = (
    _doc(_keys(101, 103))
    + _doc(_keys(104, 104))
    + _doc(_keys(105, 108))
    + _doc(_keys(109, 110))
    + _doc(_keys(111, 113), ext={"REV000113": "jpg"})
)

GOOD_DAT_HEADER = ["BEGBATES", "ENDBATES", "CUSTODIAN", "CONFIDENTIALITY"]
GOOD_DAT_ROWS = [
    ["REV000101", "REV000103", "Smith, John", "Confidential"],
    ["REV000104", "REV000104", "Zoë Adams", "Confidential"],
    ["REV000105", "REV000108", "Smith, John", "Highly Confidential\nAttorneys' Eyes Only"],
    ["REV000109", "REV000110", "Lee, Morgan", "Confidential"],
    ["REV000111", "REV000113", "Zoë Adams", "Not Confidential"],
]

#: prod_broken OPT, line by line (1-based line numbers in comments).
BROKEN_OPT = (
    _doc(_keys(101, 103))                                   # 1-3
    + _doc(_keys(104, 104), page_count=2)                   # 4      page count says 2, has 1
    + _doc(_keys(105, 108))                                 # 5-8    REV000105.tif missing; REV000106 on disk as rev000106.TIF
    + [OptLine("REV000109", "REV000109.tif", True, 3),      # 9
       OptLine("REV000110", "REV000110.tif", False),        # 10
       OptLine("REV000110", "REV000110A.tif", False)]       # 11     duplicate key
    + _doc(["REV000112", "REV000113"], ext={"REV000113": "jpg"})  # 12-13  gap: REV000111 skipped
)

BROKEN_DAT_ROWS = [
    ["REV000101", "REV000103", "Smith, John", "Confidential"],
    ["REV000104", "REV000104", "Zoë Adams", "Confidential"],
    ["REV000105", "REV000108", "Smith, John", "Highly Confidential\nAttorneys' Eyes Only"],
    ["REV000109", "REV000110", "Lee, Morgan", "Confidential"],
    ["REV000112", "REV000113", "Zoë Adams", "Not Confidential"],
    ["REV000999", "REV000999", "Nobody", "Confidential"],    # DAT row 7 (incl. header): no matching document
]

BROKEN_MISSING_FILES = ["REV000105.tif"]
BROKEN_CASE_RENAMES = {"REV000106.tif": "rev000106.TIF"}

#: The exact load report prod_broken must produce (Milestone 4).
#: The case-only filename difference must resolve without any issue.
BROKEN_EXPECTED_ISSUES = [
    {"severity": "error", "check": "missing_file", "line": 5},
    {"severity": "warning", "check": "page_count_mismatch", "line": 4},
    {"severity": "error", "check": "duplicate_key", "line": 11},
    {"severity": "warning", "check": "bates_gap", "line": 12},
    {"severity": "warning", "check": "dat_no_document", "line": 7},
]

#: prod_multipage: two documents over the 4 frames of one TIFF.
MULTIPAGE_FILE = "MULTI000001.tif"
MULTIPAGE_OPT = [
    OptLine("MP000001", MULTIPAGE_FILE, True, 2),
    OptLine("MP000002", MULTIPAGE_FILE, False),
    OptLine("MP000003", MULTIPAGE_FILE, True, 2),
    OptLine("MP000004", MULTIPAGE_FILE, False),
]


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT), size)


def _draw_page(img: Image.Image, title: str, ink, accent=None) -> None:
    """Draw recognisable content: a frame, a title and some body lines."""
    d = ImageDraw.Draw(img)
    w, h = img.size
    m = w // 10
    d.rectangle([m, m, w - m, h - m], outline=ink, width=6)
    d.text((m + 60, m + 60), title, fill=ink, font=_font(110, bold=True))
    body = _font(48)
    for i in range(12):
        d.text((m + 60, m + 300 + i * 120), f"Line {i + 1}: sample production content for {title}.", fill=ink, font=body)
    if accent is not None:
        d.ellipse([w // 2 - 300, h // 2 + 500, w // 2 + 300, h // 2 + 1100], fill=accent)


def bw_page(title: str, size: tuple[int, int] = LETTER_PX) -> Image.Image:
    """A 1-bit page with content."""
    img = Image.new("L", size, 255)
    _draw_page(img, title, ink=0)
    return img.point(lambda v: 255 if v >= 128 else 0).convert("1")


def color_page(title: str, size: tuple[int, int] = LETTER_PX) -> Image.Image:
    img = Image.new("RGB", size, (255, 255, 255))
    _draw_page(img, title, ink=(20, 40, 120), accent=(200, 30, 30))
    return img


def grey_page(title: str, size: tuple[int, int] = LETTER_PX) -> Image.Image:
    img = Image.new("L", size, 255)
    _draw_page(img, title, ink=60, accent=170)
    return img


def save_g4(img: Image.Image | list[Image.Image], path: Path, dpi: int = DPI) -> None:
    frames = img if isinstance(img, list) else [img]
    frames[0].save(
        path, compression="group4", dpi=(dpi, dpi),
        save_all=len(frames) > 1, append_images=frames[1:],
    )


def _pdf_save(doc: pymupdf.Document, path: Path, **kwargs) -> None:
    doc.set_metadata({"producer": "readystamp fixtures", "creator": "readystamp fixtures"})
    doc.save(path, garbage=3, deflate=True, no_new_id=True, **kwargs)
    doc.close()


def _pdf_text_page(doc: pymupdf.Document, text: str, size=LETTER_PT) -> pymupdf.Page:
    page = doc.new_page(width=size[0], height=size[1])
    page.draw_rect(pymupdf.Rect(36, 36, size[0] - 36, size[1] - 36), color=(0, 0, 0), width=1)
    page.insert_text((72, 96), text, fontsize=12, fontname="helv")
    for i in range(10):
        page.insert_text((72, 140 + i * 24), f"Body line {i + 1}.", fontsize=11, fontname="helv")
    return page


# ---------------------------------------------------------------------------
# Fixture sets
# ---------------------------------------------------------------------------

def make_loose(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open()
    for text in MEMO_PAGE_TEXT:
        _pdf_text_page(doc, text)
    _pdf_save(doc, out / "doc1_memo.pdf")

    doc = pymupdf.open()
    _pdf_text_page(doc, "Mixed orientation: portrait page 1 of 2.")
    _pdf_text_page(doc, "Mixed orientation: landscape page 2 of 2.", size=(LETTER_PT[1], LETTER_PT[0]))
    _pdf_save(doc, out / "doc2_mixed.pdf")

    doc = pymupdf.open()
    page = _pdf_text_page(doc, "This page has /Rotate 90 and displays as landscape.")
    page.set_rotation(90)
    _pdf_save(doc, out / "doc3_rotated.pdf")

    save_g4([bw_page("Scan frame 1 of 2"), bw_page("Scan frame 2 of 2")], out / "doc4_scan.tif")
    color_page("Colour photo").save(out / "doc5_photo.jpg", quality=90, dpi=(DPI, DPI))
    grey_page("Greyscale diagram").save(out / "doc6_diagram.png")   # deliberately no DPI tag

    doc = pymupdf.open()
    _pdf_text_page(doc, "This file is password protected.")
    doc.set_metadata({"producer": "readystamp fixtures"})
    doc.save(out / "doc10_locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw="owner-secret", user_pw="user-secret")
    doc.close()

    (out / "readme.txt").write_text("Not a supported document type.\n", encoding="utf-8")
    return out


def _write_opt(path: Path, lines: list[OptLine], volume: str = "VOL001") -> None:
    rows = []
    for ln in lines:
        rows.append(",".join([
            ln.key, volume, f".\\IMAGES\\IMG001\\{ln.file}",
            "Y" if ln.doc_break else "", "", "",
            "" if ln.page_count is None else str(ln.page_count),
        ]))
    path.write_bytes(("\r\n".join(rows) + "\r\n").encode("utf-8"))


def _dat_value(v: str) -> str:
    return DAT_QUOTE + v.replace("\r\n", DAT_NEWLINE).replace("\n", DAT_NEWLINE) + DAT_QUOTE


def _write_dat(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [DAT_DELIM.join(_dat_value(v) for v in row) for row in [header, *rows]]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))


def _write_images(img_dir: Path, lines: list[OptLine]) -> None:
    img_dir.mkdir(parents=True, exist_ok=True)
    for ln in lines:
        target = img_dir / ln.file
        if target.exists():
            continue
        if ln.file.lower().endswith(".jpg"):
            color_page(ln.key).save(target, quality=90, dpi=(DPI, DPI))
        else:
            save_g4(bw_page(ln.key), target)


def make_prod_good(out: Path) -> Path:
    vol = out / "VOL001"
    _write_images(vol / "IMAGES" / "IMG001", GOOD_OPT)
    (vol / "DATA").mkdir(parents=True, exist_ok=True)
    _write_opt(vol / "DATA" / "VOL001.opt", GOOD_OPT)
    _write_dat(vol / "DATA" / "VOL001.dat", GOOD_DAT_HEADER, GOOD_DAT_ROWS)
    return out


def make_prod_broken(out: Path) -> Path:
    vol = out / "VOL001"
    img_dir = vol / "IMAGES" / "IMG001"
    _write_images(img_dir, BROKEN_OPT)
    for name in BROKEN_MISSING_FILES:
        (img_dir / name).unlink()
    for name, new_name in BROKEN_CASE_RENAMES.items():
        tmp = img_dir / (name + ".tmp")          # two-step rename also works on case-insensitive filesystems
        (img_dir / name).rename(tmp)
        tmp.rename(img_dir / new_name)
    (vol / "DATA").mkdir(parents=True, exist_ok=True)
    _write_opt(vol / "DATA" / "VOL001.opt", BROKEN_OPT)
    _write_dat(vol / "DATA" / "VOL001.dat", GOOD_DAT_HEADER, BROKEN_DAT_ROWS)
    return out


def make_prod_multipage(out: Path) -> Path:
    vol = out / "VOL001"
    img_dir = vol / "IMAGES" / "IMG001"
    img_dir.mkdir(parents=True, exist_ok=True)
    save_g4([bw_page(f"Multi-page frame {n} of 4") for n in range(1, 5)], img_dir / MULTIPAGE_FILE)
    (vol / "DATA").mkdir(parents=True, exist_ok=True)
    _write_opt(vol / "DATA" / "VOL001.opt", MULTIPAGE_OPT)
    return out


FIXTURE_SETS = {
    "loose": make_loose,
    "prod_good": make_prod_good,
    "prod_broken": make_prod_broken,
    "prod_multipage": make_prod_multipage,
}


def generate_all(root: Path) -> dict[str, Path]:
    """(Re)generate every fixture set under `root`. Existing fixture folders are replaced."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    made = {}
    for name, builder in FIXTURE_SETS.items():
        target = root / name
        if target.exists():
            shutil.rmtree(target)
        made[name] = builder(target)
    return made


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python tests/fixtures.py OUTPUT_DIR")
    for name, path in generate_all(Path(sys.argv[1])).items():
        print(f"{name}: {path}")
