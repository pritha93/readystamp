"""Template tokens -> text.

A template is plain text with tokens in braces, e.g. "{BATES}" or
"Page {PAGE} of {PAGES}". Literal braces are written "{{" and "}}".
Token names are matched case-insensitively.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from readystamp.model import Document, Page, Production

PAGE_TOKENS = {"BATES", "PAGE"}                               # differ page to page
DOC_TOKENS = {"BEGBATES", "ENDBATES", "PAGES", "DOCNUM", "FILENAME", "ORIG_BATES"}
RUN_TOKENS = {"DATE"}
TOKENS = PAGE_TOKENS | DOC_TOKENS | RUN_TOKENS               # plus FIELD:Name

_SCAN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")


class TemplateError(ValueError):
    """A template that cannot be resolved; the message is user-readable."""


@dataclass(frozen=True)
class Token:
    name: str            # upper-case token name, or 'FIELD'
    arg: str | None = None  # column name for FIELD


def parse(template: str) -> list[str | Token]:
    """Split a template into literal strings and Tokens. Raises TemplateError."""
    parts: list[str | Token] = []
    literal: list[str] = []
    pos = 0
    for m in _SCAN.finditer(template):
        literal.append(template[pos:m.start()])
        pos = m.end()
        text = m.group(0)
        if text in ("{{", "}}"):
            literal.append(text[0])
            continue
        if m.group(1) is None:
            raise TemplateError(f"Unmatched '{text}' at position {m.start() + 1}; write '{text * 2}' for a literal brace.")
        body = m.group(1).strip()
        if body.upper().startswith("FIELD:"):
            column = body[len("FIELD:"):].strip()
            if not column:
                raise TemplateError("{FIELD:} needs a column name, e.g. {FIELD:Custodian}.")
            token = Token("FIELD", column)
        elif body.upper() in TOKENS:
            token = Token(body.upper())
        else:
            raise TemplateError(f"Unknown token {{{body}}}. Available: {', '.join('{' + t + '}' for t in sorted(TOKENS))}, {{FIELD:Name}}.")
        if literal:
            parts.append("".join(literal))
            literal = []
        parts.append(token)
    literal.append(template[pos:])
    if "".join(literal):
        parts.append("".join(literal))
    return parts


def field_columns(production: Production) -> dict[str, str]:
    """DAT column names present in the production, keyed by lower-case name."""
    columns: dict[str, str] = {}
    for name in production.dat_columns:
        columns.setdefault(name.lower(), name)
    for doc in production.documents:
        for name in doc.fields:
            columns.setdefault(name.lower(), name)
    return columns


def validate(template: str, production: Production, allowed: set[str] | None = None) -> list[str]:
    """User-readable problems with a template for this production; empty if it is fine.

    `allowed` limits which token names may appear (FIELD is named 'FIELD').
    """
    try:
        parts = parse(template)
    except TemplateError as exc:
        return [str(exc)]
    problems = []
    columns = field_columns(production)
    for part in parts:
        if not isinstance(part, Token):
            continue
        if allowed is not None and part.name not in allowed:
            label = "{FIELD:…}" if part.name == "FIELD" else "{" + part.name + "}"
            problems.append(f"{label} cannot be used here.")
        elif part.name == "FIELD" and part.arg.lower() not in columns:
            if columns:
                problems.append(f"{{FIELD:{part.arg}}}: there is no DAT column named '{part.arg}'. "
                                f"Columns: {', '.join(sorted(columns.values()))}.")
            else:
                problems.append(f"{{FIELD:{part.arg}}}: no DAT file is loaded, so field tokens have no values.")
    return problems


@dataclass
class Context:
    """What a template is resolved against: one page (or one document, for prefixes)."""
    production: Production
    document: Document
    page: Page | None = None
    run_date: dt.date | None = None

    def value(self, token: Token) -> str:
        doc, page = self.document, self.page
        match token.name:
            case "BATES":
                return (page.new_bates if page else None) or ""
            case "PAGE":
                return str(page.index_in_doc) if page else ""
            case "BEGBATES":
                return doc.beg_bates or ""
            case "ENDBATES":
                return doc.end_bates or ""
            case "ORIG_BATES":
                return (page.original_bates if page else (doc.pages[0].original_bates if doc.pages else None)) or ""
            case "PAGES":
                return str(doc.page_count)
            case "DOCNUM":
                width = len(str(len(self.production.documents)))
                return str(doc.seq).zfill(width)
            case "FILENAME":
                return Path(doc.source_name).stem
            case "DATE":
                return (self.run_date or dt.date.today()).isoformat()
            case "FIELD":
                return doc.field(token.arg) or ""
        raise TemplateError(f"Unknown token {{{token.name}}}.")


def render(template: str, ctx: Context) -> str:
    """Resolve a template. Raises TemplateError for a malformed template."""
    return "".join(p if isinstance(p, str) else ctx.value(p) for p in parse(template))
