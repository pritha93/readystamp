"""Small builders for synthetic productions used across tests."""

from pathlib import Path

from readystamp.model import Document, Page, Production


def make_production(page_counts: list[int], kind: str = "loose", fields: list[dict] | None = None,
                    names: list[str] | None = None, orig_prefix: str = "REV") -> Production:
    docs, key = [], 1
    for seq, count in enumerate(page_counts, start=1):
        name = names[seq - 1] if names else f"doc{seq}.pdf"
        pages = []
        for i in range(count):
            pages.append(Page(index_in_doc=i + 1, source_path=Path(name), source_page=i,
                              width_pt=612, height_pt=792,
                              original_bates=f"{orig_prefix}{key:06d}" if kind == "opt" else None))
            key += 1
        docs.append(Document(seq=seq, pages=pages, source_name=name,
                             fields=(fields[seq - 1] if fields else {})))
    return Production(source_kind=kind, documents=docs)
