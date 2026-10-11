"""Milestone 2: loose-file ingest and its load-report checks."""

import shutil

import pytest

import fixtures
from readystamp.ingest.loose import load_loose, natural_key
from readystamp.ingest.validate import report_to_csv


@pytest.fixture
def loose(fixture_root):
    return load_loose([fixture_root / "loose"])


def test_natural_sort():
    names = ["doc10.pdf", "doc2.pdf", "Doc1.pdf", "doc1a.pdf"]
    assert sorted(names, key=natural_key) == ["Doc1.pdf", "doc1a.pdf", "doc2.pdf", "doc10.pdf"]


def test_documents_in_natural_order_with_page_counts(loose):
    expected = [(n, c) for n, c in fixtures.LOOSE_FILES if n.endswith((".pdf", ".tif", ".jpg", ".png"))]
    got = [(d.source_name, d.page_count) for d in loose.documents]
    assert got == [(n, c or 0) for n, c in expected]
    assert [d.seq for d in loose.documents] == list(range(1, len(got) + 1))
    assert loose.source_kind == "loose"


def test_load_report_for_loose_fixture(loose):
    issues = [(i.severity, i.check, i.path.rsplit("/", 1)[-1]) for i in loose.load_report.issues]
    assert sorted(issues) == [("error", "pdf_encrypted", "doc10_locked.pdf"), ("info", "unsupported_type", "readme.txt")]
    enc = loose.load_report.by_severity("error")[0]
    assert enc.doc_seq == 7 and "password-protected" in enc.message
    assert loose.load_report.has_errors


def test_page_properties(loose):
    docs = {d.source_name: d for d in loose.documents}
    memo = docs["doc1_memo.pdf"].pages
    assert [p.source_page for p in memo] == [0, 1, 2]
    assert (memo[0].width_pt, memo[0].height_pt) == (612, 792)
    mixed = docs["doc2_mixed.pdf"].pages
    assert [p.is_landscape for p in mixed] == [False, True]
    rot = docs["doc3_rotated.pdf"].pages[0]
    assert rot.rotation == 90 and rot.is_landscape
    scan = docs["doc4_scan.tif"].pages
    assert [p.source_page for p in scan] == [0, 1]
    assert all(p.dpi == 300 and not p.is_color for p in scan)
    assert scan[0].width_pt == pytest.approx(612) and scan[0].height_pt == pytest.approx(792)
    assert docs["doc5_photo.jpg"].pages[0].is_color
    png = docs["doc6_diagram.png"].pages[0]
    assert png.dpi == 300 and not png.is_color   # no DPI tag -> default 300


def test_totals(loose):
    assert loose.totals() == {"documents": 7, "pages": 10, "color_pages": 1, "landscape_pages": 2}


def test_removing_rejected_document_clears_its_error(loose):
    loose.remove_documents({7})
    assert not loose.load_report.has_errors
    assert [d.seq for d in loose.documents] == list(range(1, 7))
    assert [i.check for i in loose.load_report.issues] == ["unsupported_type"]


def test_damaged_pdf_and_unreadable_image(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.7 this is not really a pdf")
    (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\xff garbage")
    prod = load_loose([tmp_path])
    checks = sorted((i.check, i.doc_seq) for i in prod.load_report.issues)
    assert checks == [("image_unreadable", 1), ("pdf_damaged", 2)]
    assert all(d.page_count == 0 for d in prod.documents)


def test_files_and_folders_mixed_hidden_skipped_and_deduplicated(fixture_root, tmp_path):
    folder = tmp_path / "drop"
    sub = folder / "sub"
    sub.mkdir(parents=True)
    shutil.copy(fixture_root / "loose" / "doc5_photo.jpg", sub / "b10.jpg")
    shutil.copy(fixture_root / "loose" / "doc5_photo.jpg", folder / "b2.jpg")
    (folder / ".DS_Store").write_bytes(b"junk")
    single = fixture_root / "loose" / "doc1_memo.pdf"
    prod = load_loose([folder, single, single])
    assert [d.source_name for d in prod.documents] == ["b2.jpg", "b10.jpg", "doc1_memo.pdf"]
    assert prod.load_report.issues == []


def test_missing_path_is_reported(tmp_path):
    prod = load_loose([tmp_path / "nope.pdf"])
    assert [i.check for i in prod.load_report.issues] == ["missing_file"]


def test_input_files_are_not_modified(fixture_root):
    import hashlib
    files = sorted((fixture_root / "loose").iterdir())
    before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    load_loose([fixture_root / "loose"])
    assert before == {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def test_report_csv(loose):
    text = report_to_csv(loose.load_report)
    lines = text.strip().split("\r\n")
    assert lines[0] == "Severity,Check,Document,Line,Path,Message"
    assert lines[1].startswith("error,pdf_encrypted,7,")
    assert lines[2].startswith("info,unsupported_type,")
