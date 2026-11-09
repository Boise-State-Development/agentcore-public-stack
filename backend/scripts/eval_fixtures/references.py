"""The source text the ``grounded`` judge reads (eval-harness spec D17, EH-7).

Built from a case's fixtures, never from its session: the session's document
text is scrubbed before any judge reads it (§6.4), so two delivery paths stay
indistinguishable, and the reference is the same text whichever path produced
the answer. Text formats are read as they are, Word through ``docx_markdown``
(the rendering documents-as-text sends), PDF through pypdfium2, a workbook as
comma-separated rows per sheet. Images and decks have no source text here, so
a case whose files are only those has no reference and ``grounded`` reports
"no reference" rather than a score. ``kb_large`` and the large documents are
over the cap: grounding a 140k-token corpus costs more than the judgement is
worth, and their planted facts are checked deterministically anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from eval_fixtures import build, entities

#: Above this the reference costs more judge input than the verdict is worth.
MAX_REFERENCE_CHARS = 60_000

_TEXT = (".md", ".txt", ".html", ".csv")


def file_text(name: str, data: bytes) -> Optional[str]:
    """``name``'s text as a reader of the source would see it, or ``None`` for a format without one."""
    suffix = Path(name).suffix.lower()
    if suffix in _TEXT:
        return data.decode("utf-8", errors="replace")
    if suffix == ".docx":
        from apis.shared.files.docx_markdown import docx_to_markdown

        return docx_to_markdown(data)
    if suffix == ".xlsx":
        # Each sheet as comma-separated rows: what the spreadsheet tools compute from.
        import io

        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return "\n\n".join(
            f"[sheet {sheet.title}]\n" + "\n".join(
                ",".join("" if v is None else str(v) for v in row) for row in sheet.iter_rows(values_only=True)
            )
            for sheet in book.worksheets
        )
    if suffix == ".pdf":
        import pypdfium2

        document = pypdfium2.PdfDocument(data)
        return "\n".join(page.get_textpage().get_text_bounded() for page in document)
    return None


def _case_files(case: Any, fixtures_dir: Path) -> List[Tuple[str, bytes]]:
    files: List[Tuple[str, bytes]] = []
    for name in case.files:
        if name in build.GENERATED:
            files.append((name, build.build(name)))
        elif (fixtures_dir / name).is_file():
            files.append((name, (fixtures_dir / name).read_bytes()))
    project = entities.PROJECTS.get(case.run.project or "")
    if project is not None:
        files.extend((f.name, f.build()) for f in project.files)
    agent = entities.AGENTS.get(case.run.agent or "")
    if agent is not None:
        files.extend((f.name, f.build()) for f in agent.files)
    return files


def reference_for(case: Any, fixtures_dir: Path) -> Optional[str]:
    """The case's source text, or ``None`` when it has none worth judging against."""
    if case.large_documents:
        return None
    parts: Sequence[str] = [
        f"## {name}\n{text.strip()}"
        for name, data in _case_files(case, fixtures_dir)
        for text in [file_text(name, data)]
        if text and text.strip()
    ]
    reference = "\n\n".join(parts)
    if not reference or len(reference) > MAX_REFERENCE_CHARS:
        return None
    return reference
