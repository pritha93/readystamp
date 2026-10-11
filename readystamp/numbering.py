"""Bates number assignment and the checks that must pass before stamping."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from readystamp import tokens
from readystamp.model import NumberingSettings, Production, unsafe_chars

#: Tokens that may appear in a prefix: they are the same for every page of a document.
PREFIX_TOKENS = {"DOCNUM", "FILENAME", "DATE", "FIELD"}
_PER_DOCUMENT_TOKENS = {"DOCNUM", "FIELD"}


class NumberingError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class NumberingPlan:
    """The numbers a setting would assign, without changing the production."""
    numbers: list[list[str]] = field(default_factory=list)   # per document, per page
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def sample(self, documents: int = 3) -> dict:
        """First/last number of the production and of the first few documents."""
        flat = [n for doc in self.numbers for n in doc]
        return {
            "first": flat[0] if flat else None,
            "last": flat[-1] if flat else None,
            "documents": [(doc[0], doc[-1]) for doc in self.numbers[:documents] if doc],
        }


def _fmt_page(n: int) -> str:
    return f"{n:,}"


def plan(production: Production, settings: NumberingSettings, run_date: dt.date | None = None) -> NumberingPlan:
    """Work out every page's number and every reason it cannot be used."""
    result = NumberingPlan()
    problems = result.problems
    if not production.documents or production.page_count == 0:
        problems.append("There are no pages to number.")
        return result

    if settings.mode == "keep":
        if production.source_kind != "opt":
            problems.append("Keeping existing numbers is only available for OPT productions; choose Renumber.")
            return result
        for doc in production.documents:
            nums = [p.original_bates or "" for p in doc.pages]
            if any(not n for n in nums):
                problems.append(f"Document {doc.seq} has a page without an existing Bates number.")
            result.numbers.append(nums)
    else:
        prefix_problems = tokens.validate(settings.prefix, production, allowed=PREFIX_TOKENS)
        if prefix_problems:
            problems.extend(f"Prefix: {p}" for p in prefix_problems)
            return result
        if settings.reset_per_document:
            used = {t.name for t in tokens.parse(settings.prefix) if isinstance(t, tokens.Token)}
            if not used & _PER_DOCUMENT_TOKENS:
                problems.append("Reset per document needs a per-document token in the prefix, such as {DOCNUM} or "
                                "{FIELD:Name}; otherwise every document would get the same numbers.")
                return result

        limit = 10 ** settings.padding
        counter = settings.start
        overflow_reported = False
        bad_prefix_docs = []
        page_no = 0
        for doc in production.documents:
            if settings.reset_per_document:
                counter = settings.start
            prefix = tokens.render(settings.prefix, tokens.Context(production, doc, run_date=run_date))
            if unsafe_chars(prefix) or not prefix.strip() and prefix:
                bad_prefix_docs.append((doc.seq, prefix))
            head = prefix + settings.separator
            nums = []
            for _ in doc.pages:
                page_no += 1
                if counter >= limit and not overflow_reported:
                    problems.append(
                        f"Padding of {settings.padding} digits is too small: page {_fmt_page(page_no)} would be "
                        f"{head}{counter}, which does not fit. The last number needed is "
                        f"{_last_counter(production, settings):,}; increase padding or lower the start number.")
                    overflow_reported = True
                nums.append(f"{head}{counter:0{settings.padding}d}")
                counter += 1
            result.numbers.append(nums)
        for seq, prefix in bad_prefix_docs[:5]:
            problems.append(f"Document {seq}: the prefix resolves to {prefix!r}, which is not safe in a filename.")

    flat = [n for doc in result.numbers for n in doc]
    dupes = [n for n, c in Counter(flat).items() if c > 1]
    if dupes:
        shown = ", ".join(dupes[:5]) + (f" and {len(dupes) - 5} more" if len(dupes) > 5 else "")
        problems.append(f"Bates numbers must be unique, but these repeat: {shown}.")
    return result


def _last_counter(production: Production, settings: NumberingSettings) -> int:
    if settings.reset_per_document:
        return settings.start + max(d.page_count for d in production.documents) - 1
    return settings.start + production.page_count - 1


def apply(production: Production, settings: NumberingSettings, run_date: dt.date | None = None) -> NumberingPlan:
    """Assign `new_bates` to every page. Raises NumberingError, changing nothing, if any check fails."""
    result = plan(production, settings, run_date)
    if not result.ok:
        raise NumberingError(result.problems)
    for doc, nums in zip(production.documents, result.numbers):
        for page, number in zip(doc.pages, nums):
            page.new_bates = number
    return result
