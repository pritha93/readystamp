"""Milestone 2: Bates numbering."""

import datetime as dt

import pytest

from helpers import make_production
from readystamp import numbering
from readystamp.ingest.loose import load_loose
from readystamp.model import NumberingSettings


def ns(**kw):
    return NumberingSettings(**kw)


def test_acceptance_loose_fixture_renumber(fixture_root):
    prod = load_loose([fixture_root / "loose"])
    prod.remove_documents({7})   # the encrypted PDF
    numbering.apply(prod, ns(prefix="ABC", start=1, padding=6))
    memo = prod.documents[0]
    assert memo.source_name == "doc1_memo.pdf"
    assert [p.new_bates for p in memo.pages] == ["ABC000001", "ABC000002", "ABC000003"]
    flat = [p.new_bates for p in prod.pages()]
    assert flat == [f"ABC{n:06d}" for n in range(1, prod.page_count + 1)]


def test_separator_start_and_padding():
    prod = make_production([2, 1])
    numbering.apply(prod, ns(prefix="XYZ", separator="-", start=98, padding=4))
    assert [p.new_bates for p in prod.pages()] == ["XYZ-0098", "XYZ-0099", "XYZ-0100"]
    assert (prod.documents[0].beg_bates, prod.documents[0].end_bates) == ("XYZ-0098", "XYZ-0099")


def test_default_format_is_prefix_plus_six_digits():
    prod = make_production([1])
    numbering.apply(prod, ns(prefix="ABC"))
    assert prod.documents[0].pages[0].new_bates == "ABC000001"


def test_padding_overflow_names_first_overflowing_number():
    prod = make_production([400, 400, 400])   # 1,200 pages
    result = numbering.plan(prod, ns(prefix="ABC", padding=3))
    assert not result.ok
    assert len(result.problems) == 1
    msg = result.problems[0]
    assert "ABC1000" in msg and "page 1,000" in msg and "1,200" in msg
    with pytest.raises(numbering.NumberingError):
        numbering.apply(prod, ns(prefix="ABC", padding=3))
    assert all(p.new_bates is None for p in prod.pages())   # nothing changed


def test_padding_exactly_fits():
    prod = make_production([999])
    assert numbering.plan(prod, ns(padding=3)).ok
    assert not numbering.plan(prod, ns(padding=3, start=2)).ok


def test_reset_per_document_with_docnum():
    prod = make_production([3, 2])
    numbering.apply(prod, ns(prefix="EX{DOCNUM}", separator="-", padding=3, reset_per_document=True))
    assert [p.new_bates for p in prod.pages()] == ["EX1-001", "EX1-002", "EX1-003", "EX2-001", "EX2-002"]


def test_reset_per_document_docnum_padding_follows_document_count():
    prod = make_production([1] * 12)
    numbering.apply(prod, ns(prefix="EX{DOCNUM}_", padding=2, reset_per_document=True))
    assert prod.documents[0].pages[0].new_bates == "EX01_01"
    assert prod.documents[11].pages[0].new_bates == "EX12_01"


def test_reset_per_document_requires_per_document_token():
    prod = make_production([2, 2])
    result = numbering.plan(prod, ns(prefix="ABC", reset_per_document=True))
    assert len(result.problems) == 1 and "{DOCNUM}" in result.problems[0]


def test_reset_overflow_uses_largest_document():
    prod = make_production([5, 12])
    result = numbering.plan(prod, ns(prefix="D{DOCNUM}", padding=1, reset_per_document=True))
    assert not result.ok and "D210" in result.problems[0]


def test_unique_number_check_with_field_prefix():
    prod = make_production([2, 2, 1], kind="opt",
                           fields=[{"Custodian": "Smith"}, {"Custodian": "Smith"}, {"Custodian": "Lee"}])
    result = numbering.plan(prod, ns(prefix="{FIELD:custodian}_", reset_per_document=True))
    assert len(result.problems) == 1
    assert "must be unique" in result.problems[0] and "Smith_000001" in result.problems[0]
    ok = numbering.plan(prod, ns(prefix="{FIELD:Custodian}_"))
    assert ok.ok and ok.numbers[2] == ["Lee_000005"]


def test_resolved_prefix_must_be_filename_safe():
    prod = make_production([1, 1], kind="opt", fields=[{"Custodian": "A/B"}, {"Custodian": "C"}])
    result = numbering.plan(prod, ns(prefix="{FIELD:Custodian}"))
    assert any("Document 1" in p and "not safe" in p for p in result.problems)


def test_prefix_token_errors():
    prod = make_production([1])
    assert "Prefix: {BATES} cannot be used here." in numbering.plan(prod, ns(prefix="{BATES}")).problems
    assert any("Unknown token {NOPE}" in p for p in numbering.plan(prod, ns(prefix="{NOPE}")).problems)
    assert any("no DAT file is loaded" in p for p in numbering.plan(prod, ns(prefix="{FIELD:X}")).problems)


def test_prefix_date_token():
    prod = make_production([1])
    numbering.apply(prod, ns(prefix="P{DATE}_", padding=2), run_date=dt.date(2026, 10, 11))
    assert prod.documents[0].pages[0].new_bates == "P2026-10-11_01"


def test_keep_mode():
    prod = make_production([2, 1], kind="opt")
    numbering.apply(prod, ns(mode="keep"))
    assert [p.new_bates for p in prod.pages()] == ["REV000001", "REV000002", "REV000003"]


def test_keep_mode_needs_opt_and_unique_keys():
    assert "only available for OPT" in numbering.plan(make_production([1]), ns(mode="keep")).problems[0]
    prod = make_production([1, 1], kind="opt")
    prod.documents[1].pages[0].original_bates = "REV000001"
    assert "REV000001" in numbering.plan(prod, ns(mode="keep")).problems[0]


def test_empty_production():
    assert numbering.plan(make_production([]), ns()).problems == ["There are no pages to number."]


def test_sample():
    prod = make_production([3, 1, 2, 4])
    sample = numbering.plan(prod, ns(prefix="ABC")).sample()
    assert sample == {
        "first": "ABC000001", "last": "ABC000010",
        "documents": [("ABC000001", "ABC000003"), ("ABC000004", "ABC000004"), ("ABC000005", "ABC000006")],
    }
