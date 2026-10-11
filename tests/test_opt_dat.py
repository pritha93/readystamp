"""Milestone 4: OPT and DAT ingest, path resolution and the full load report."""

import shutil
from pathlib import Path

import pytest

import fixtures
from readystamp import numbering, tokens
from readystamp.ingest.dat import DatError, attach_dat, read_dat
from readystamp.ingest.opt import LoadError, PathResolver, decode_text, load_opt
from readystamp.model import NumberingSettings


def opt_of(root: Path, name: str) -> Path:
    return root / name / "VOL001" / "DATA" / "VOL001.opt"


def dat_of(root: Path, name: str) -> Path:
    return root / name / "VOL001" / "DATA" / "VOL001.dat"


def issues(prod):
    return sorted((i.severity, i.check, i.line) for i in prod.load_report.issues)


@pytest.fixture
def good(fixture_root):
    return load_opt(opt_of(fixture_root, "prod_good"), dat_of(fixture_root, "prod_good"))


# --- the fixture productions ----------------------------------------------------------

def test_good_production_loads_cleanly(good):
    assert good.source_kind == "opt" and good.volume_name == "VOL001"
    assert issues(good) == []
    assert [(d.source_name, d.page_count) for d in good.documents] == [
        ("REV000101", 3), ("REV000104", 1), ("REV000105", 4), ("REV000109", 2), ("REV000111", 3)]
    assert [p.original_bates for p in good.pages()] == [f"REV{n:06d}" for n in range(101, 114)]
    assert all(p.source_page == 0 and p.dpi == 300 for p in good.pages())
    last = good.documents[-1].pages[-1]
    assert last.source_path.name == "REV000113.jpg" and last.is_color
    assert good.totals()["color_pages"] == 1


def test_broken_production_report_is_exactly_as_expected(fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_broken"), dat_of(fixture_root, "prod_broken"))
    expected = sorted((i["severity"], i["check"], i["line"]) for i in fixtures.BROKEN_EXPECTED_ISSUES)
    assert issues(prod) == expected
    assert prod.load_report.has_errors
    # the filename that differs only in case resolves without an issue
    doc3 = prod.documents[2]
    assert [p.original_bates for p in doc3.pages] == ["REV000106", "REV000107", "REV000108"]
    assert doc3.pages[0].source_path.name == "rev000106.TIF"


def test_removing_documents_clears_their_errors(fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_broken"), dat_of(fixture_root, "prod_broken"))
    prod.remove_documents({3, 4})          # the missing image and the duplicate key
    assert not prod.load_report.has_errors
    assert [d.seq for d in prod.documents] == [1, 2, 3]


def test_multipage_tiff_lines_map_to_successive_frames(fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_multipage"))
    assert issues(prod) == []
    assert [(d.source_name, d.page_count) for d in prod.documents] == [("MP000001", 2), ("MP000003", 2)]
    assert [p.source_page for p in prod.pages()] == [0, 1, 2, 3]
    assert len({p.source_path for p in prod.pages()}) == 1
    assert [p.index_in_doc for p in prod.pages()] == [1, 2, 1, 2]


def test_frame_overflow_is_an_error(fixture_root, tmp_path):
    vol = tmp_path / "VOL001"
    shutil.copytree(fixture_root / "prod_multipage" / "VOL001", vol)
    opt = vol / "DATA" / "VOL001.opt"
    opt.write_bytes(opt.read_bytes() + b"MP000005,VOL001,.\\IMAGES\\IMG001\\MULTI000001.tif,,,,\r\n")
    prod = load_opt(opt)
    assert issues(prod) == [("error", "image_unreadable", 5), ("warning", "page_count_mismatch", 3)]
    assert "only 4 page(s)" in prod.load_report.by_severity("error")[0].message


# --- numbering and tokens on an OPT production -------------------------------------------

def test_keep_mode_uses_original_keys(good):
    numbering.apply(good, NumberingSettings(mode="keep"))
    assert [p.new_bates for p in good.pages()] == [p.original_bates for p in good.pages()]


def test_renumber_keeps_original_for_tokens(good):
    numbering.apply(good, NumberingSettings(prefix="ABC"))
    doc = good.documents[1]
    ctx = tokens.Context(good, doc, doc.pages[0])
    assert tokens.render("{BATES} was {ORIG_BATES}", ctx) == "ABC000004 was REV000104"


def test_field_tokens_from_dat(good):
    doc = good.documents[1]
    ctx = tokens.Context(good, doc, doc.pages[0])
    assert tokens.render("{FIELD:Custodian}", ctx) == "Zoë Adams"
    doc3 = good.documents[2]
    assert tokens.render("{FIELD:confidentiality}", tokens.Context(good, doc3, doc3.pages[0])) == \
        "Highly Confidential\nAttorneys' Eyes Only"
    assert tokens.validate("{FIELD:Custodian}", good) == []
    assert "no DAT column named 'Nope'" in tokens.validate("{FIELD:Nope}", good)[0]
    assert good.dat_columns == fixtures.GOOD_DAT_HEADER


# --- OPT parsing ------------------------------------------------------------------------------

def _write_prod(tmp_path: Path, opt_text: str, images=("A0001.tif",), encoding="utf-8") -> Path:
    img = tmp_path / "VOL001" / "IMAGES" / "IMG001"
    img.mkdir(parents=True)
    for name in images:
        fixtures.save_g4(fixtures.bw_page(name, size=(300, 300)), img / name)
    data = tmp_path / "VOL001" / "DATA"
    data.mkdir()
    (data / "VOL001.opt").write_bytes(opt_text.encode(encoding))
    return data / "VOL001.opt"


def test_parsing_rules(tmp_path):
    opt = _write_prod(tmp_path,
                      "A0001,VOL001,.\\IMAGES\\IMG001\\A0001.tif\r\n"      # 3 fields, no doc break
                      "\r\n"                                                 # blank line skipped
                      "A0002,VOL001\r\n"                                     # too few fields
                      "A0003 , VOL001 , IMAGES/IMG001/A0003.tif , y ,,,1\n",  # spaces, lower-case y, LF
                      images=("A0001.tif", "A0003.tif"))
    prod = load_opt(opt)
    assert issues(prod) == [("error", "opt_first_line_not_break", 1), ("error", "opt_too_few_fields", 3),
                            ("warning", "bates_gap", 4)]
    assert [(d.source_name, d.page_count) for d in prod.documents] == [("A0001", 1), ("A0003", 1)]


def test_page_count_may_be_blank(tmp_path):
    opt = _write_prod(tmp_path, "A0001,VOL001,.\\IMAGES\\IMG001\\A0001.tif,Y,,,\r\n")
    assert issues(load_opt(opt)) == []


def test_encodings(tmp_path):
    assert decode_text("Zoë".encode("utf-8-sig")) == "Zoë"
    assert decode_text("Zoë".encode("utf-16")) == "Zoë"
    assert decode_text("Zoë".encode("cp1252")) == "Zoë"
    assert decode_text("Zoë".encode("utf-8")) == "Zoë"
    opt = _write_prod(tmp_path, "É0001,VOL,.\\IMAGES\\IMG001\\A0001.tif,Y,,,1\r\n", encoding="cp1252")
    assert load_opt(opt).documents[0].source_name == "É0001"


def test_unreferenced_and_unreadable_images(tmp_path):
    opt = _write_prod(tmp_path, "A0001,VOL,.\\IMAGES\\IMG001\\A0001.tif,Y,,,2\r\n"
                                "A0002,VOL,.\\IMAGES\\IMG001\\A0002.tif,,,,\r\n",
                      images=("A0001.tif", "EXTRA.tif"))
    img = tmp_path / "VOL001" / "IMAGES" / "IMG001"
    (img / "A0002.tif").write_bytes(b"II*\x00 not a tiff")
    (img / "notes.txt").write_text("ignored")
    prod = load_opt(opt)
    assert issues(prod) == [("error", "image_unreadable", 2), ("info", "unreferenced_image", None)]
    assert prod.load_report.by_severity("info")[0].path.endswith("EXTRA.tif")


def test_missing_opt_is_a_load_error(tmp_path):
    with pytest.raises(LoadError, match="could not be found"):
        load_opt(tmp_path / "nope.opt")
    (tmp_path / "empty.opt").write_text("\r\n")
    with pytest.raises(LoadError, match="no image lines"):
        load_opt(tmp_path / "empty.opt")


# --- path resolution ----------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    img = tmp_path / "VOL001" / "IMAGES" / "IMG001"
    img.mkdir(parents=True)
    (img / "ABC0001.tif").write_bytes(b"x")
    (tmp_path / "VOL001" / "DATA").mkdir()
    return tmp_path / "VOL001"


@pytest.mark.parametrize("raw", [
    ".\\IMAGES\\IMG001\\ABC0001.tif",
    "IMAGES\\IMG001\\ABC0001.tif",
    "./IMAGES/IMG001/ABC0001.tif",
    "IMAGES/IMG001/ABC0001.tif",
    ".\\images\\img001\\abc0001.TIF",                    # case-insensitive
    "C:\\Productions\\VOL001\\IMAGES\\IMG001\\ABC0001.tif",  # absolute Windows path
    "\\\\server\\share\\VOL001\\Images\\IMG001\\ABC0001.tif",  # UNC path
])
def test_path_resolution_from_data_folder(tree, raw):
    found = PathResolver(tree / "DATA").resolve(raw)
    assert found is not None and found.name == "ABC0001.tif"


def test_path_resolution_from_volume_folder(tree):
    assert PathResolver(tree).resolve(".\\IMAGES\\IMG001\\ABC0001.tif") is not None


def test_absolute_posix_path(tree):
    path = tree / "IMAGES" / "IMG001" / "ABC0001.tif"
    assert PathResolver(tree / "DATA").resolve(str(path)) == path


@pytest.mark.parametrize("raw", ["IMAGES\\IMG001\\NOPE.tif", "C:\\elsewhere\\ABC0001.tif", "/no/such/file.tif"])
def test_unresolvable_paths(tree, raw):
    assert PathResolver(tree / "DATA").resolve(raw) is None


# --- DAT reading ----------------------------------------------------------------------------------

def _dat(tmp_path, rows, encoding="utf-8-sig", delim="\x14", quote="\xfe", name="x.dat"):
    lines = [delim.join(f"{quote}{v.replace(chr(10), chr(0xAE))}{quote}" for v in row) for row in rows]
    path = tmp_path / name
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode(encoding))
    return path


def test_read_concordance_dat(fixture_root):
    header, rows, lines, problems = read_dat(dat_of(fixture_root, "prod_good"))
    assert header == fixtures.GOOD_DAT_HEADER
    assert rows == fixtures.GOOD_DAT_ROWS
    assert lines == [2, 3, 4, 5, 6] and problems == []


@pytest.mark.parametrize("encoding", ["utf-16", "cp1252", "utf-8"])
def test_dat_encodings(tmp_path, encoding):
    path = _dat(tmp_path, [["BEGDOC", "NAME"], ["A1", "Zoë"]], encoding=encoding)
    assert read_dat(path)[1] == [["A1", "Zoë"]]


def test_dat_delimiter_overrides(tmp_path):
    path = _dat(tmp_path, [["BEGBATES", "NOTE"], ["A1", 'say "hi", ok']], delim=",", quote='"')
    path.write_text('BEGBATES,NOTE\r\nA1,"say ""hi"", ok"\r\n', encoding="utf-8")
    assert read_dat(path, delimiter="comma", qualifier="double_quote")[1] == [["A1", 'say "hi", ok']]
    tab = tmp_path / "t.dat"
    tab.write_text("BEGBATES\tNOTE\nA1\tx|y\n", encoding="utf-8")
    assert read_dat(tab, delimiter="tab", qualifier="none")[1] == [["A1", "x|y"]]
    with pytest.raises(DatError, match="Unknown delimiter"):
        read_dat(tab, delimiter="semicolons")


def test_dat_field_count_error(tmp_path, fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_good"))
    rows = [fixtures.GOOD_DAT_HEADER] + fixtures.GOOD_DAT_ROWS
    rows[3] = rows[3][:2]
    attach_dat(prod, _dat(tmp_path, rows))
    assert issues(prod) == [("error", "dat_field_count", 4), ("warning", "document_no_dat", None)]


def test_dat_key_column_choice(tmp_path, fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_good"))
    rows = [["DocID", "begdoc", "Custodian"]] + [[f"D{i}", r[0], r[2]] for i, r in enumerate(fixtures.GOOD_DAT_ROWS)]
    path = _dat(tmp_path, rows)
    assert attach_dat(prod, path) == "begdoc"                  # auto-selected, case-insensitive
    assert issues(prod) == []
    prod2 = load_opt(opt_of(fixture_root, "prod_good"))
    attach_dat(prod2, path, key_column="DocID")
    assert len(prod2.load_report.by_severity("warning")) == 10  # 5 docs without rows + 5 rows without docs
    with pytest.raises(DatError, match="no column named 'Nope'"):
        attach_dat(load_opt(opt_of(fixture_root, "prod_good")), path, key_column="Nope")
    nokey = _dat(tmp_path, [["A", "B"], ["1", "2"]], name="nokey.dat")
    with pytest.raises(DatError, match="Choose which DAT column"):
        attach_dat(load_opt(opt_of(fixture_root, "prod_good")), nokey)


def test_dat_duplicate_rows_and_missing_file(tmp_path, fixture_root):
    prod = load_opt(opt_of(fixture_root, "prod_good"))
    rows = [fixtures.GOOD_DAT_HEADER] + fixtures.GOOD_DAT_ROWS + [fixtures.GOOD_DAT_ROWS[0]]
    attach_dat(prod, _dat(tmp_path, rows))
    assert issues(prod) == [("warning", "dat_duplicate_key", 7)]
    with pytest.raises(DatError, match="could not be found"):
        attach_dat(prod, tmp_path / "none.dat")


def test_inputs_unchanged(fixture_root):
    import hashlib
    files = sorted(p for p in (fixture_root / "prod_broken").rglob("*") if p.is_file())
    before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    load_opt(opt_of(fixture_root, "prod_broken"), dat_of(fixture_root, "prod_broken"))
    assert before == [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
