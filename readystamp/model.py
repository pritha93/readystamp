"""Core data model and settings models.

Every input, however it was loaded, becomes a Production holding ordered
Documents, each holding ordered Pages. Settings holds everything a preset
saves. All models are Pydantic v2; settings models reject unknown keys.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 1

# Characters that are not safe in Windows/macOS/Linux filenames.
UNSAFE_FILENAME_CHARS = set('\\/:*?"<>|')
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Escaped braces and {TOKEN} / {FIELD:Name} spans; their resolved values are checked at numbering time.
_TOKEN_SPAN = re.compile(r"\{\{|\}\}|\{[^{}]*\}")


def unsafe_chars(value: str) -> list[str]:
    """Return the characters in `value` that are not safe in a filename."""
    bad = []
    for ch in value:
        if (ch in UNSAFE_FILENAME_CHARS or ord(ch) < 32 or ord(ch) == 127) and ch not in bad:
            bad.append(ch)
    return bad


def _describe_chars(chars: list[str]) -> str:
    return " ".join(repr(c) if c.isprintable() else f"control character 0x{ord(c):02X}" for c in chars)


# --------------------------------------------------------------------------
# Production model
# --------------------------------------------------------------------------

class Page(BaseModel):
    index_in_doc: int = Field(ge=1)          # 1-based
    source_path: Path                        # PDF file or image file
    source_page: int = Field(default=0, ge=0)  # page in a PDF; frame in a multi-page TIFF; else 0
    original_bates: str | None = None        # image key from the OPT, if loaded from one
    width_pt: float = Field(gt=0)            # images: pixels / dpi * 72
    height_pt: float = Field(gt=0)
    dpi: int | None = None                   # images only; 300 if the file has none
    is_color: bool = False                   # images: mode not '1' or 'L'; PDFs: decided at render time
    rotation: Literal[0, 90, 180, 270] = 0   # PDF /Rotate value
    new_bates: str | None = None             # assigned by numbering
    opt_path: str | None = None              # image path exactly as written in the OPT, if loaded from one

    @property
    def is_pdf(self) -> bool:
        return self.source_path.suffix.lower() == ".pdf"

    @property
    def display_size_pt(self) -> tuple[float, float]:
        """Page size as displayed, after applying /Rotate."""
        if self.rotation in (90, 270):
            return self.height_pt, self.width_pt
        return self.width_pt, self.height_pt

    @property
    def is_landscape(self) -> bool:
        w, h = self.display_size_pt
        return w > h


class Document(BaseModel):
    seq: int = Field(ge=1)                   # 1-based order in the production
    pages: list[Page] = Field(default_factory=list)
    source_name: str                         # filename, or first image key for OPT docs
    source_path: Path | None = None          # loose mode: the file this document came from
    fields: dict[str, str] = Field(default_factory=dict)  # DAT columns, if loaded

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def beg_bates(self) -> str | None:
        return self.pages[0].new_bates if self.pages else None

    @property
    def end_bates(self) -> str | None:
        return self.pages[-1].new_bates if self.pages else None

    def field(self, name: str) -> str | None:
        """Look up a DAT field by name, case-insensitively."""
        lowered = name.lower()
        for key, value in self.fields.items():
            if key.lower() == lowered:
                return value
        return None


Severity = Literal["error", "warning", "info"]


class LoadIssue(BaseModel):
    severity: Severity
    check: str                               # stable id, e.g. 'missing_file'
    message: str                             # plain, user-readable sentence
    doc_seq: int | None = None
    line: int | None = None                  # OPT/DAT line number, 1-based
    path: str | None = None


class LoadReport(BaseModel):
    issues: list[LoadIssue] = Field(default_factory=list)

    def add(self, severity: Severity, check: str, message: str, **ref) -> LoadIssue:
        issue = LoadIssue(severity=severity, check=check, message=message, **ref)
        self.issues.append(issue)
        return issue

    def by_severity(self, severity: Severity) -> list[LoadIssue]:
        return [i for i in self.issues if i.severity == severity]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


class Production(BaseModel):
    source_kind: Literal["loose", "opt"]
    volume_name: str = "VOL001"
    documents: list[Document] = Field(default_factory=list)
    load_report: LoadReport = Field(default_factory=LoadReport)
    dat_columns: list[str] = Field(default_factory=list)   # loaded DAT header, in file order
    upload_root: Path | None = None   # loose files uploaded through the UI: the folder they were copied to

    def pages(self):
        for doc in self.documents:
            yield from doc.pages

    @property
    def page_count(self) -> int:
        return sum(d.page_count for d in self.documents)

    def totals(self) -> dict[str, int]:
        """Counts shown on the Review screen. PDF colour is only known after rendering."""
        pages = list(self.pages())
        return {
            "documents": len(self.documents),
            "pages": len(pages),
            "color_pages": sum(p.is_color for p in pages),
            "landscape_pages": sum(p.is_landscape for p in pages),
        }

    def renumber_seq(self) -> None:
        for i, doc in enumerate(self.documents, start=1):
            doc.seq = i

    def remove_documents(self, seqs: set[int]) -> None:
        """Drop documents by seq, with their load-report issues, and renumber seq 1..n."""
        removed = [d for d in self.documents if d.seq in seqs]
        removed_paths = {str(d.source_path) for d in removed if d.source_path is not None}
        self.documents = [d for d in self.documents if d.seq not in seqs]
        self.load_report.issues = [
            i for i in self.load_report.issues
            if not (i.doc_seq in seqs or (i.path is not None and i.path in removed_paths))
        ]
        old_to_new = {d.seq: n for n, d in enumerate(self.documents, start=1)}
        for issue in self.load_report.issues:
            if issue.doc_seq is not None:
                issue.doc_seq = old_to_new.get(issue.doc_seq)
        self.renumber_seq()


# --------------------------------------------------------------------------
# Settings models (everything a preset saves)
# --------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ZoneId(StrEnum):
    TL = "TL"
    TC = "TC"
    TR = "TR"
    ML = "ML"
    MC = "MC"
    MR = "MR"
    BL = "BL"
    BC = "BC"
    BR = "BR"


class NumberingSettings(_Strict):
    mode: Literal["keep", "renumber"] = "renumber"   # 'keep' only in OPT mode
    prefix: str = ""                      # may contain field tokens, e.g. '{FIELD:Custodian}_'
    separator: str = ""
    start: int = Field(default=1, ge=0)
    padding: int = Field(default=6, ge=1, le=15)     # digits; counter is zero-padded
    reset_per_document: bool = False      # counter restarts at `start` for each doc

    @field_validator("prefix", "separator")
    @classmethod
    def _filename_safe(cls, value: str) -> str:
        bad = unsafe_chars(_TOKEN_SPAN.sub("", value))
        if bad:
            raise ValueError(f"must be safe in filenames; remove {_describe_chars(bad)}")
        return value


class ZoneSettings(_Strict):
    enabled: bool = False
    template: str = ""                    # may hold several lines and tokens
    font: Literal["sans", "serif", "mono"] = "sans"
    size_pt: float = Field(default=10, gt=0, le=200)
    bold: bool = False
    color: str = "#000000"                # forced to black for black-and-white TIFF output
    rotation: Literal[0, 90, 270, 45] = 0  # 45 allowed only in MC
    knockout: bool = False                # white box behind the text
    page_rule: Literal["all", "first", "last", "odd", "even", "portrait", "landscape"] = "all"

    @field_validator("color")
    @classmethod
    def _hex_color(cls, value: str) -> str:
        if not _HEX_COLOR.match(value):
            raise ValueError("must be a colour like #000000")
        return value.upper()


def default_zones() -> dict[ZoneId, ZoneSettings]:
    """The first-launch preset: BR = {BATES}, BL = CONFIDENTIAL, 10 pt sans, black."""
    zones = {z: ZoneSettings() for z in ZoneId}
    zones[ZoneId.BR] = ZoneSettings(enabled=True, template="{BATES}")
    zones[ZoneId.BL] = ZoneSettings(enabled=True, template="CONFIDENTIAL")
    return zones


class MarginSettings(_Strict):
    mode: Literal["overlay", "add_band"] = "overlay"
    edge_offset_in: float = Field(default=0.25, ge=0, le=2)
    band_in: float = Field(default=0.5, gt=0, le=3)   # add_band only


class OutputSettings(_Strict):
    format: Literal["pdf", "tiff"] = "pdf"
    output_dir: Path | None = None        # must be empty or not yet exist; not saved in presets
    volume_name: str = "VOL001"
    images_per_folder: int = Field(default=1000, ge=1)   # tiff mode
    tiff_dpi: int = Field(default=300, ge=72, le=1200)   # used when rasterizing PDFs
    color_mode: Literal["jpeg", "tiff_lzw", "force_bw"] = "jpeg"
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    bw_threshold: int = Field(default=160, ge=0, le=255)
    dat_fields: list[str] | None = None   # None = default set

    @field_validator("volume_name")
    @classmethod
    def _volume_safe(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        bad = unsafe_chars(value)
        if bad:
            raise ValueError(f"must be safe in filenames; remove {_describe_chars(bad)}")
        return value


class Settings(_Strict):
    numbering: NumberingSettings = Field(default_factory=NumberingSettings)
    zones: dict[ZoneId, ZoneSettings] = Field(default_factory=default_zones)
    margin: MarginSettings = Field(default_factory=MarginSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    schema_version: int = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(f"version {value} is not supported (expected {SCHEMA_VERSION})")
        return value

    @model_validator(mode="after")
    def _complete_zones(self) -> "Settings":
        # A preset may list only some zones; the rest are disabled.
        for zone in ZoneId:
            if zone not in self.zones:
                self.zones[zone] = ZoneSettings()
        for zone, zs in self.zones.items():
            if zs.rotation == 45 and zone != ZoneId.MC:
                raise ValueError(f"Zone {zone.value}: 45° rotation is only allowed in the middle-centre (MC) zone")
        return self


# --------------------------------------------------------------------------
# User-readable validation messages
# --------------------------------------------------------------------------

def readable_errors(exc: ValidationError) -> list[str]:
    """Turn a Pydantic ValidationError into plain sentences, one per problem."""
    messages = []
    for err in exc.errors():
        loc = [str(p) for p in err["loc"]]
        if err["type"] == "extra_forbidden":
            name = loc[-1]
            where = " → ".join(loc[:-1])
            messages.append(f"Unknown setting '{name}'" + (f" in {where}" if where else ""))
            continue
        where = " → ".join(loc) if loc else "Settings"
        msg = err["msg"]
        for prefix in ("Value error, ", "Assertion failed, "):
            if msg.startswith(prefix):
                msg = msg[len(prefix):]
        messages.append(f"{where}: {msg}" if loc else msg)
    return messages
