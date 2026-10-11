"""Concordance DAT ingest and the join to OPT documents."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from readystamp.ingest.validate import report_issue
from readystamp.model import Production

CONCORDANCE_DELIMITER = "\x14"   # ¶
CONCORDANCE_QUALIFIER = "\xfe"   # þ
CONCORDANCE_NEWLINE = "\xae"     # ®

#: Column names auto-selected as the begin-Bates key, in order of preference.
KEY_CANDIDATES = ["BEGBATES", "BEGDOC", "BEG_BATES", "PRODBEG"]

#: Named alternatives the UI offers when a DAT does not use Concordance characters.
DELIMITERS = {"concordance": CONCORDANCE_DELIMITER, "comma": ",", "tab": "\t", "pipe": "|"}
QUALIFIERS = {"concordance": CONCORDANCE_QUALIFIER, "double_quote": '"', "none": ""}


class DatError(ValueError):
    """The DAT cannot be used at all. User-readable."""


def _char(value: str | None, named: dict[str, str], default: str, what: str) -> str:
    if value is None:
        return default
    if value in named:
        return named[value]
    if len(value) <= 1:
        return value
    raise DatError(f"Unknown {what} {value!r}; choose one of {', '.join(named)} or a single character.")


def read_dat(path: Path, delimiter: str | None = None, qualifier: str | None = None):
    """Return (header, rows, line_numbers, report_problems) for a DAT file.

    `rows` excludes the header; values have ® converted back to line breaks.
    `problems` lists (line, message) for rows whose field count differs from the header.
    """
    from readystamp.ingest.opt import decode_text

    delim = _char(delimiter, DELIMITERS, CONCORDANCE_DELIMITER, "delimiter")
    quote = _char(qualifier, QUALIFIERS, CONCORDANCE_QUALIFIER, "qualifier")
    if not delim:
        raise DatError("The DAT delimiter cannot be empty.")
    try:
        text = decode_text(Path(path).read_bytes())
    except OSError as exc:
        raise DatError(f"The DAT file {Path(path).name} could not be read: {exc.strerror or exc}.") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim,
                        quotechar=quote or None, quoting=csv.QUOTE_MINIMAL if quote else csv.QUOTE_NONE,
                        doublequote=True, strict=False)
    header: list[str] | None = None
    rows, lines, problems = [], [], []
    end = 0
    for row in reader:
        start, end = end + 1, reader.line_num
        if not any(v.strip() for v in row):
            continue
        row = [v.replace(CONCORDANCE_NEWLINE, "\n") for v in row]
        if header is None:
            header = [h.strip() for h in row]
            continue
        if len(row) != len(header):
            problems.append((start, f"DAT line {start} has {len(row)} field(s) but the header has {len(header)}."))
            continue
        rows.append(row)
        lines.append(start)
    if header is None:
        raise DatError(f"{Path(path).name} is empty.")
    if len(header) == 1 and len(text) > 0 and delim == CONCORDANCE_DELIMITER and "," in header[0]:
        raise DatError(f"{Path(path).name} does not look like a Concordance DAT (no ¶ delimiters). "
                       "Choose the delimiter and qualifier it uses.")
    return header, rows, lines, problems


def pick_key_column(header: list[str], requested: str | None) -> str:
    lowered = {h.lower(): h for h in header}
    if requested:
        if requested.lower() not in lowered:
            raise DatError(f"The DAT has no column named {requested!r}. Columns: {', '.join(header)}.")
        return lowered[requested.lower()]
    for name in KEY_CANDIDATES:
        if name.lower() in lowered:
            return lowered[name.lower()]
    raise DatError("Choose which DAT column holds the begin Bates number; none is named "
                   f"{', '.join(KEY_CANDIDATES)}. Columns: {', '.join(header)}.")


def attach_dat(production: Production, path: Path, key_column: str | None = None,
               delimiter: str | None = None, qualifier: str | None = None) -> str:
    """Join DAT rows to documents by begin Bates and record every mismatch. Returns the key column used."""
    if not Path(path).is_file():
        raise DatError(f"The DAT file {path} could not be found.")
    header, rows, lines, problems = read_dat(path, delimiter, qualifier)
    key = pick_key_column(header, key_column)
    report = production.load_report
    for line, message in problems:
        report_issue(report, "dat_field_count", message, line=line, path=str(path))

    col = header.index(key)
    by_key: dict[str, tuple[int, list[str]]] = {}
    for line, row in zip(lines, rows):
        value = row[col].strip()
        if value in by_key:
            report_issue(report, "dat_duplicate_key",
                         f"DAT line {line} repeats begin Bates {value} (first on line {by_key[value][0]}); "
                         "the first row is used.", line=line, path=str(path))
            continue
        by_key[value] = (line, row)

    matched: set[str] = set()
    for doc in production.documents:
        first_key = doc.source_name          # the first OPT key, even if that image is missing
        hit = by_key.get(first_key)
        if hit is None:
            report_issue(report, "document_no_dat",
                         f"Document {doc.seq} ({first_key}) has no row in the DAT.", doc_seq=doc.seq)
            continue
        matched.add(first_key)
        doc.fields = dict(zip(header, hit[1]))
    for value, (line, _) in by_key.items():
        if value not in matched:
            report_issue(report, "dat_no_document",
                         f"DAT line {line} ({key} {value}) does not match any document in the OPT.",
                         line=line, path=str(path))
    production.dat_columns = list(header)
    return key
