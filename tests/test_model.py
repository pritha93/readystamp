"""Milestone 1: data model and settings models."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from readystamp.model import (
    Document, LoadReport, MarginSettings, NumberingSettings, OutputSettings, Page,
    Production, Settings, ZoneId, ZoneSettings, readable_errors,
)


def _page(i, **kw):
    return Page(index_in_doc=i, source_path=Path("a.pdf"), width_pt=612, height_pt=792, **kw)


def _errors(fn) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        fn()
    return readable_errors(exc.value)


# --- production model ----------------------------------------------------------

def test_document_bates_range_and_fields():
    doc = Document(seq=1, source_name="a.pdf", pages=[_page(1, new_bates="ABC000001"), _page(2, new_bates="ABC000002")],
                   fields={"Custodian": "Zoë"})
    assert (doc.beg_bates, doc.end_bates, doc.page_count) == ("ABC000001", "ABC000002", 2)
    assert doc.field("CUSTODIAN") == "Zoë"
    assert doc.field("missing") is None


def test_page_orientation_respects_rotation():
    assert not _page(1).is_landscape
    assert _page(1, rotation=90).is_landscape
    assert _page(1, rotation=90).display_size_pt == (792, 612)


def test_load_report_and_production():
    report = LoadReport()
    report.add("warning", "bates_gap", "Gap between A and C.", line=3)
    assert not report.has_errors
    report.add("error", "missing_file", "File not found.", line=5, path="x.tif")
    assert report.has_errors and len(report.by_severity("error")) == 1
    prod = Production(source_kind="loose", documents=[Document(seq=1, source_name="a", pages=[_page(1), _page(2)])],
                      load_report=report)
    assert prod.page_count == 2 and len(list(prod.pages())) == 2


# --- settings defaults --------------------------------------------------------------

def test_settings_defaults_match_spec():
    s = Settings()
    assert s.schema_version == 1
    assert (s.numbering.prefix, s.numbering.separator, s.numbering.start, s.numbering.padding) == ("", "", 1, 6)
    assert set(s.zones) == set(ZoneId)
    assert s.zones[ZoneId.BR].enabled and s.zones[ZoneId.BR].template == "{BATES}"
    assert s.zones[ZoneId.BL].enabled and s.zones[ZoneId.BL].template == "CONFIDENTIAL"
    for z in (ZoneId.BR, ZoneId.BL):
        assert (s.zones[z].font, s.zones[z].size_pt, s.zones[z].color) == ("sans", 10, "#000000")
    assert sum(z.enabled for z in s.zones.values()) == 2
    assert (s.margin.mode, s.margin.edge_offset_in, s.margin.band_in) == ("overlay", 0.25, 0.5)
    o = s.output
    assert (o.volume_name, o.images_per_folder, o.tiff_dpi, o.color_mode, o.jpeg_quality, o.bw_threshold) == \
        ("VOL001", 1000, 300, "jpeg", 85, 160)


def test_partial_zones_are_completed_as_disabled():
    s = Settings.model_validate({"zones": {"TC": {"enabled": True, "template": "DRAFT"}}})
    assert set(s.zones) == set(ZoneId)
    assert s.zones[ZoneId.TC].enabled
    assert not s.zones[ZoneId.BR].enabled


def test_settings_json_round_trip():
    s = Settings()
    s.zones[ZoneId.MC] = ZoneSettings(enabled=True, template="DRAFT", rotation=45, size_pt=72, color="#ff0000")
    again = Settings.model_validate(json.loads(s.model_dump_json()))
    assert again == s
    assert again.zones[ZoneId.MC].color == "#FF0000"


# --- settings validation -------------------------------------------------------------

def test_unknown_keys_are_rejected_with_their_names():
    msgs = _errors(lambda: Settings.model_validate({"colour": 1, "zones": {"BR": {"fontsize": 9}}}))
    assert "Unknown setting 'colour'" in msgs
    assert "Unknown setting 'fontsize' in zones → BR" in msgs


def test_unknown_zone_id_is_rejected():
    msgs = _errors(lambda: Settings.model_validate({"zones": {"XX": {}}}))
    assert any("zones" in m and "XX" in m for m in msgs)


def test_45_degree_rotation_only_in_middle_centre():
    Settings.model_validate({"zones": {"MC": {"rotation": 45}}})
    msgs = _errors(lambda: Settings.model_validate({"zones": {"TR": {"rotation": 45}}}))
    assert msgs == ["Zone TR: 45° rotation is only allowed in the middle-centre (MC) zone"]


@pytest.mark.parametrize("bad", ["ABC/", "A:B", "x*", 'q"', "<", ">", "|", "?", "back\\slash", "tab\there"])
def test_prefix_and_separator_must_be_filename_safe(bad):
    msgs = _errors(lambda: NumberingSettings(prefix=bad))
    assert msgs[0].startswith("prefix: must be safe in filenames")
    msgs = _errors(lambda: NumberingSettings(separator=bad))
    assert msgs[0].startswith("separator: must be safe in filenames")


def test_prefix_may_hold_tokens():
    # The ':' inside a token is not part of the filename; the resolved value is checked at numbering time.
    assert NumberingSettings(prefix="{FIELD:Custodian}_").prefix == "{FIELD:Custodian}_"
    assert NumberingSettings(prefix="{DOCNUM}-").prefix == "{DOCNUM}-"
    assert _errors(lambda: NumberingSettings(prefix="{DOCNUM}:"))


@pytest.mark.parametrize("bad", ["black", "#12345", "#GGGGGG", "000000"])
def test_zone_colour_must_be_hex(bad):
    assert _errors(lambda: ZoneSettings(color=bad)) == ["color: must be a colour like #000000"]


def test_numeric_ranges():
    assert _errors(lambda: NumberingSettings(padding=0))
    assert _errors(lambda: OutputSettings(jpeg_quality=101))
    assert _errors(lambda: OutputSettings(bw_threshold=256))
    assert _errors(lambda: OutputSettings(images_per_folder=0))
    assert _errors(lambda: MarginSettings(edge_offset_in=-1))
    assert _errors(lambda: ZoneSettings(size_pt=0))


def test_volume_name_must_be_safe():
    assert _errors(lambda: OutputSettings(volume_name="VOL/1"))
    assert _errors(lambda: OutputSettings(volume_name="  "))


def test_unsupported_schema_version():
    assert _errors(lambda: Settings(schema_version=2)) == ["schema_version: version 2 is not supported (expected 1)"]


def test_assignment_is_validated():
    s = Settings()
    with pytest.raises(ValidationError):
        s.numbering.padding = -1
