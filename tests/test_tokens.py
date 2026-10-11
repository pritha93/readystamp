"""Milestone 2: every template token."""

import datetime as dt

import pytest

from helpers import make_production
from readystamp import numbering, tokens
from readystamp.model import NumberingSettings

DATE = dt.date(2026, 10, 11)


@pytest.fixture
def prod():
    p = make_production([3, 2] + [1] * 8, kind="opt", names=[f"IMG{n}.tif" for n in range(10)],
                        fields=[{"Custodian": "Zoë Adams", "Note": "line1\nline2"}] + [{"Custodian": "Lee"}] * 9)
    numbering.apply(p, NumberingSettings(prefix="ABC"))
    return p


def ctx(prod, doc=0, page=0):
    d = prod.documents[doc]
    return tokens.Context(prod, d, d.pages[page], run_date=DATE)


@pytest.mark.parametrize("template, doc, page, expected", [
    ("{BATES}", 0, 1, "ABC000002"),
    ("{BEGBATES}", 0, 1, "ABC000001"),
    ("{ENDBATES}", 0, 1, "ABC000003"),
    ("{ORIG_BATES}", 1, 0, "REV000004"),
    ("{PAGE}", 0, 2, "3"),
    ("{PAGES}", 0, 2, "3"),
    ("Page {PAGE} of {PAGES}", 1, 1, "Page 2 of 2"),
    ("{DOCNUM}", 1, 0, "02"),           # padded to width of 10 documents
    ("{DOCNUM}", 9, 0, "10"),
    ("{FILENAME}", 0, 0, "IMG0"),
    ("{DATE}", 0, 0, "2026-10-11"),
    ("{FIELD:Custodian}", 0, 0, "Zoë Adams"),
    ("{FIELD:custodian}", 0, 0, "Zoë Adams"),   # case-insensitive column
    ("{field:CUSTODIAN}", 0, 0, "Zoë Adams"),   # case-insensitive token
    ("{FIELD:Note}", 1, 0, ""),                  # column exists, no value for this document
    ("{FIELD:Note}", 0, 0, "line1\nline2"),
    ("{bates}", 0, 0, "ABC000001"),
    ("{{BATES}} = {BATES}", 0, 0, "{BATES} = ABC000001"),
    ("CONFIDENTIAL\n{BATES}", 0, 0, "CONFIDENTIAL\nABC000001"),
    ("{ BATES }", 0, 0, "ABC000001"),
    ("plain text", 0, 0, "plain text"),
    ("", 0, 0, ""),
])
def test_render(prod, template, doc, page, expected):
    assert tokens.validate(template, prod) == []
    assert tokens.render(template, ctx(prod, doc, page)) == expected


def test_orig_bates_blank_in_loose_mode():
    p = make_production([1])
    assert tokens.render("{ORIG_BATES}", tokens.Context(p, p.documents[0], p.documents[0].pages[0])) == ""


def test_bates_blank_before_numbering():
    p = make_production([1])
    assert tokens.render("[{BATES}]", tokens.Context(p, p.documents[0], p.documents[0].pages[0])) == "[]"


@pytest.mark.parametrize("template, fragment", [
    ("{NOPE}", "Unknown token {NOPE}"),
    ("{FIELD:Missing}", "no DAT column named 'Missing'"),
    ("{FIELD:}", "needs a column name"),
    ("{BATES", "Unmatched '{'"),
    ("BATES}", "Unmatched '}'"),
])
def test_validation_errors(prod, template, fragment):
    problems = tokens.validate(template, prod)
    assert len(problems) == 1 and fragment in problems[0]
    with pytest.raises(tokens.TemplateError) if "FIELD:Missing" not in template else _noop():
        tokens.render(template, ctx(prod))


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_field_token_without_dat():
    p = make_production([1])
    assert "no DAT file is loaded" in tokens.validate("{FIELD:Custodian}", p)[0]


def test_parse_structure():
    assert tokens.parse("A{BATES}B{FIELD:X}") == ["A", tokens.Token("BATES"), "B", tokens.Token("FIELD", "X")]
