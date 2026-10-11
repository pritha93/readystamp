"""Load-report checks: the catalogue of issue types, with their severities.

Ingest code reports problems through `report_issue`, so each check's severity
is defined once, here, matching the table in SPEC.md step 2.
"""

from __future__ import annotations

import csv
import io

from readystamp.model import LoadIssue, LoadReport, Severity

CHECKS: dict[str, Severity] = {
    # OPT production (Milestone 4)
    "missing_file": "error",            # OPT line references a file that does not exist
    "opt_too_few_fields": "error",
    "opt_first_line_not_break": "error",
    "duplicate_key": "error",
    "page_count_mismatch": "warning",
    "bates_gap": "warning",
    "dat_no_document": "warning",
    "document_no_dat": "warning",
    "dat_field_count": "error",
    "dat_duplicate_key": "warning",     # two DAT rows share a begin Bates; the first is used
    "unreferenced_image": "info",
    # Shared by both modes
    "image_unreadable": "error",        # image file cannot be opened or decoded
    "pdf_encrypted": "error",
    "pdf_damaged": "error",
    # Loose mode
    "unsupported_type": "info",
}


def report_issue(report: LoadReport, check: str, message: str, **ref) -> LoadIssue:
    """Add an issue with the severity the catalogue gives `check`."""
    return report.add(CHECKS[check], check, message, **ref)


def report_to_csv(report: LoadReport) -> str:
    """The load report as CSV (UTF-8 text; the caller adds a BOM when saving for Excel)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["Severity", "Check", "Document", "Line", "Path", "Message"])
    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(report.issues, key=lambda i: order[i.severity]):
        writer.writerow([i.severity, i.check, i.doc_seq or "", i.line or "", i.path or "", i.message])
    return buf.getvalue()
