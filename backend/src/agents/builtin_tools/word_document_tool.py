"""Word document tools (create / modify / list / read).

Each tool runs python-docx code inside AWS Bedrock Code Interpreter and uses
the existing user-files store (``apis.shared.files``) for persistence and
delivery — generated/modified ``.docx`` files land in
``S3_USER_FILES_BUCKET_NAME`` with a ``FileMetadata`` row (status READY) in
``DYNAMODB_USER_FILES_TABLE_NAME``, so they appear in the chat's Files panel
and are downloadable via the app-api ``/files/{id}/preview-url`` route.

Tools
-----
* ``create_word_document`` — build a new document from python-docx code.
* ``modify_word_document`` — edit an existing document with python-docx code.
* ``list_word_documents``  — list the .docx files available in this chat.
* ``read_word_document``   — extract an existing document's text content.
* ``redline_word_document`` — propose edits as tracked changes and comments.
  Built only while ``WORD_TRACKED_CHANGES_ENABLED`` is on; stdlib, no Code
  Interpreter (``office/docx_redline.py``).

(A page-screenshot/preview tool is intentionally omitted: rasterizing a
.docx requires LibreOffice/poppler, which the Python-only Code Interpreter
sandbox does not provide.)

Design notes
------------
* The Code Interpreter + user-files storage plumbing is shared with the Excel
  toolset and lives in ``builtin_tools.office._storage``; this module keeps
  only the python-docx specifics (preamble, generate/modify/extract) and the
  four tool factories.
* Identity (``user_id`` / ``session_id``) is captured by closure via the
  ``make_*`` factories — the same pattern used by the artifacts and
  spreadsheet_analysis tools (the Strands runtime here does NOT populate
  ``ToolContext.invocation_state`` with identity). The tools are injected
  per-request through ``extra_tools`` (see ``_build_word_document_tools`` in
  ``apis/inference_api/chat/routes.py``); they are deliberately NOT registered
  in ``builtin_tools/__init__`` because they need request-scoped identity.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from strands import tool

from agents.builtin_tools.office.docx_redline import Change, RedlineError, redline
from agents.builtin_tools.office._storage import (
    _DocGenError,
    _ci_exec,
    _ci_read_bytes,
    _ci_write_bytes,
    _download_card,
    _download_s3_bytes,
    _error,
    _get_code_interpreter_id,
    _NO_CI_MESSAGE,
    _region,
    _storage_configured,
    _store_document,
    _validate_document_name,
)

logger = logging.getLogger(__name__)

# Word document MIME type (matches apis.shared.files.ALLOWED_MIME_TYPES).
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Sandbox path used to stage a source document loaded from S3.
_SANDBOX_SOURCE = "_source.docx"

_NO_STORAGE_MESSAGE = (
    "❌ Word document storage is not configured "
    "(S3_USER_FILES_BUCKET_NAME is not set on the runtime)."
)


# ---------------------------------------------------------------------------
# python-docx document builders (run in Code Interpreter)
# ---------------------------------------------------------------------------


_DOCX_PREAMBLE = (
    "from docx import Document\n"
    "from docx.shared import Pt, RGBColor, Inches\n"
    "from docx.enum.text import WD_ALIGN_PARAGRAPH\n"
)


def _generate_docx_bytes(
    code_interpreter_id: str, python_code: str, filename: str
) -> bytes:
    """Build a new .docx from user code and return its bytes.

    Blocking (boto3 / Code Interpreter) — call via ``asyncio.to_thread``.
    """
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    code_interpreter = CodeInterpreter(_region())
    code_interpreter.start(identifier=code_interpreter_id)
    try:
        # The user's code operates on a pre-initialized ``doc`` and must not
        # call Document()/doc.save() itself — we own the lifecycle.
        _ci_exec(
            code_interpreter,
            (
                f"{_DOCX_PREAMBLE}\n"
                "doc = Document()\n\n"
                f"{python_code}\n\n"
                f"doc.save({filename!r})\n"
            ),
        )
        data = _ci_read_bytes(code_interpreter, filename)
        if data is None:
            raise _DocGenError(
                f"Document '{filename}' was not produced. Make sure your code "
                "adds content to `doc`."
            )
        return data
    finally:
        try:
            code_interpreter.stop()
        except Exception:  # pragma: no cover - cleanup best-effort
            pass


def _modify_docx_bytes(
    code_interpreter_id: str,
    source_bytes: bytes,
    python_code: str,
    output_filename: str,
) -> bytes:
    """Load an existing .docx, apply user edits, return the new bytes.

    Blocking — call via ``asyncio.to_thread``.
    """
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    code_interpreter = CodeInterpreter(_region())
    code_interpreter.start(identifier=code_interpreter_id)
    try:
        _ci_write_bytes(code_interpreter, _SANDBOX_SOURCE, source_bytes)
        _ci_exec(
            code_interpreter,
            (
                f"{_DOCX_PREAMBLE}\n"
                f"doc = Document({_SANDBOX_SOURCE!r})\n\n"
                f"{python_code}\n\n"
                f"doc.save({output_filename!r})\n"
            ),
        )
        data = _ci_read_bytes(code_interpreter, output_filename)
        if data is None:
            raise _DocGenError(
                f"Modified document '{output_filename}' was not produced."
            )
        return data
    finally:
        try:
            code_interpreter.stop()
        except Exception:  # pragma: no cover - cleanup best-effort
            pass


def _extract_docx_text(code_interpreter_id: str, source_bytes: bytes) -> str:
    """Extract readable text (headings, paragraphs, tables) from a .docx.

    Blocking — call via ``asyncio.to_thread``.
    """
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    code_interpreter = CodeInterpreter(_region())
    code_interpreter.start(identifier=code_interpreter_id)
    try:
        _ci_write_bytes(code_interpreter, _SANDBOX_SOURCE, source_bytes)
        extraction = (
            "from docx import Document\n"
            f"doc = Document({_SANDBOX_SOURCE!r})\n"
            "lines = []\n"
            "for p in doc.paragraphs:\n"
            "    t = p.text.strip()\n"
            "    if not t:\n"
            "        continue\n"
            "    style = (p.style.name if p.style else '') or ''\n"
            "    if style.startswith('Heading'):\n"
            "        level = ''.join(ch for ch in style if ch.isdigit()) or '1'\n"
            "        lines.append('#' * min(int(level), 6) + ' ' + t)\n"
            "    else:\n"
            "        lines.append(t)\n"
            "for i, table in enumerate(doc.tables):\n"
            "    lines.append('')\n"
            "    lines.append('[Table %d]' % (i + 1))\n"
            "    for row in table.rows:\n"
            "        lines.append(' | '.join(c.text.strip() for c in row.cells))\n"
            "print('\\n'.join(lines))\n"
        )
        return _ci_exec(code_interpreter, extraction).strip()
    finally:
        try:
            code_interpreter.stop()
        except Exception:  # pragma: no cover - cleanup best-effort
            pass


# ---------------------------------------------------------------------------
# User-files lookup
# ---------------------------------------------------------------------------


async def _find_word_document(
    user_id: str, session_id: str, document_name: str
):
    """Find the newest READY .docx in this session matching ``document_name``.

    Returns the ``FileMetadata`` or ``None``. ``list_session_files`` returns
    newest-first, so the first match is the latest version.
    """
    from apis.shared.files import FileStatus, get_file_upload_repository

    target = (
        document_name
        if document_name.lower().endswith(".docx")
        else f"{document_name}.docx"
    )
    files = await get_file_upload_repository().list_session_files(
        session_id, status=FileStatus.READY
    )
    for meta in files:
        if (
            meta.user_id == user_id
            and meta.mime_type == _DOCX_MIME
            and meta.filename.lower() == target.lower()
        ):
            return meta
    return None


# ---------------------------------------------------------------------------
# Tool factories
# ---------------------------------------------------------------------------


def make_create_word_document_tool(session_id: str, user_id: str):
    """Create a ``create_word_document`` tool bound to the given identity."""

    @tool
    async def create_word_document(
        python_code: str,
        document_name: str,
    ) -> Any:
        """Create a new Word (.docx) document using python-docx code.

        Executes python-docx code in a sandboxed Code Interpreter to build a
        document from scratch, saves it to the user's files, and returns a
        download card. Great for structured reports with headings,
        paragraphs, tables, and matplotlib charts.

        Available libraries in the sandbox: python-docx, matplotlib, pandas,
        numpy.

        Args:
            python_code: python-docx code that builds the document. A blank
                document is already available as ``doc = Document()`` — do NOT
                call ``Document()`` or ``doc.save()`` yourself; the tool saves
                it for you. ``Pt``, ``RGBColor``, ``Inches`` and
                ``WD_ALIGN_PARAGRAPH`` are already imported.

                Example:
                    doc.add_heading('Quarterly Report', level=1)
                    doc.add_paragraph('Revenue increased by 15%...')
                    table = doc.add_table(rows=2, cols=2)
                    table.style = 'Light Grid Accent 1'
                    table.rows[0].cells[0].text = 'Quarter'
                    # ALWAYS bold a header row this way. A table style's
                    # own header emphasis is conditional formatting that
                    # not every viewer applies, so set it on the runs:
                    for cell in table.rows[0].cells:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True

                To embed a chart, save a PNG with matplotlib then insert it:
                    import matplotlib.pyplot as plt
                    plt.figure(figsize=(8, 5))
                    plt.bar(['Q1', 'Q2'], [100, 120])
                    plt.savefig('chart.png', dpi=200, bbox_inches='tight')
                    plt.close()
                    doc.add_picture('chart.png', width=Inches(6))

                Start a major section on a fresh page with an explicit
                break -- python-docx does no layout, so a document that
                should be several pages contains no page boundaries at
                all unless you add them:
                    doc.add_page_break()
                    doc.add_heading('Appendix A', level=1)

            document_name: File name WITHOUT extension (.docx is added
                automatically). Use only letters, numbers, hyphens, and
                underscores (e.g. "sales-report", "Q4_analysis").

        Returns:
            An inline download card. The document is also saved to this
            chat's Files.
        """
        is_valid, error_msg = _validate_document_name(document_name)
        if not is_valid:
            return _error(
                f"❌ Invalid document name '{document_name}': {error_msg}\n\n"
                "Examples: sales-report, Q4_analysis, report-final"
            )

        filename = f"{document_name}.docx"
        code_interpreter_id = _get_code_interpreter_id()
        if not code_interpreter_id:
            return _error(_NO_CI_MESSAGE)
        if not _storage_configured():
            return _error(_NO_STORAGE_MESSAGE)

        try:
            file_bytes = await asyncio.to_thread(
                _generate_docx_bytes, code_interpreter_id, python_code, filename
            )
        except _DocGenError as exc:
            return _error(
                f"❌ Failed to create '{filename}'.\n\n```\n{exc}\n```\n\n"
                "Check the python-docx code for errors."
            )
        except Exception as exc:  # noqa: BLE001 - surface any sandbox error
            logger.error(f"create_word_document sandbox error: {exc}")
            return _error(f"❌ Failed to create '{filename}': {exc}")

        try:
            upload_id, size_kb = await _store_document(
                user_id, session_id, filename, file_bytes, _DOCX_MIME
            )
        except Exception as exc:  # noqa: BLE001 - storage failure is terminal
            logger.error(f"create_word_document storage error: {exc}")
            return _error(f"❌ Created '{filename}' but failed to save it: {exc}")

        return _download_card(filename, upload_id, size_kb, "Created")

    return create_word_document


def make_modify_word_document_tool(session_id: str, user_id: str):
    """Create a ``modify_word_document`` tool bound to the given identity."""

    @tool
    async def modify_word_document(
        document_name: str,
        python_code: str,
        output_name: Optional[str] = None,
    ) -> Any:
        """Modify an existing Word (.docx) document with python-docx code.

        Loads a document previously created in this chat, runs your
        python-docx code against it, and saves the result (as a new file so
        the original is preserved). Returns a download card.

        Use ``list_word_documents`` first if you are unsure of the exact name.

        Args:
            document_name: Name of the existing document to edit (with or
                without the .docx extension), e.g. "sales-report".
            python_code: python-docx code that edits the document. The loaded
                document is available as ``doc = Document(...)`` — do NOT call
                ``Document()`` or ``doc.save()`` yourself. ``Pt``, ``RGBColor``,
                ``Inches`` and ``WD_ALIGN_PARAGRAPH`` are already imported.

                Example (append a section):
                    doc.add_heading('Addendum', level=1)
                    doc.add_paragraph('Updated figures for Q2.')

            output_name: Optional name (without extension) for the edited copy.
                Defaults to the source name (a new versioned copy is saved).

        Returns:
            An inline download card for the edited document.
        """
        code_interpreter_id = _get_code_interpreter_id()
        if not code_interpreter_id:
            return _error(_NO_CI_MESSAGE)
        if not _storage_configured():
            return _error(_NO_STORAGE_MESSAGE)

        source = await _find_word_document(user_id, session_id, document_name)
        if source is None:
            return _error(
                f"❌ No Word document named '{document_name}' was found in this "
                "chat. Use list_word_documents to see what's available."
            )

        out_base = output_name or source.filename
        if out_base.lower().endswith(".docx"):
            out_base = out_base[: -len(".docx")]
        is_valid, error_msg = _validate_document_name(out_base)
        if not is_valid:
            return _error(
                f"❌ Invalid output name '{out_base}': {error_msg}"
            )
        output_filename = f"{out_base}.docx"

        try:
            source_bytes = await asyncio.to_thread(
                _download_s3_bytes, source.s3_bucket, source.s3_key
            )
            file_bytes = await asyncio.to_thread(
                _modify_docx_bytes,
                code_interpreter_id,
                source_bytes,
                python_code,
                output_filename,
            )
        except _DocGenError as exc:
            return _error(
                f"❌ Failed to modify '{source.filename}'.\n\n```\n{exc}\n```\n\n"
                "Check the python-docx code for errors."
            )
        except Exception as exc:  # noqa: BLE001 - surface any sandbox error
            logger.error(f"modify_word_document error: {exc}")
            return _error(f"❌ Failed to modify '{source.filename}': {exc}")

        try:
            upload_id, size_kb = await _store_document(
                user_id, session_id, output_filename, file_bytes, _DOCX_MIME
            )
        except Exception as exc:  # noqa: BLE001 - storage failure is terminal
            logger.error(f"modify_word_document storage error: {exc}")
            return _error(
                f"❌ Modified '{source.filename}' but failed to save it: {exc}"
            )

        return _download_card(output_filename, upload_id, size_kb, "Updated")

    return modify_word_document


def _parse_changes(changes: Any) -> Tuple[List[Change], List[str]]:
    """The tool's ``changes`` argument as :class:`Change` objects, or the problems."""
    if not isinstance(changes, list) or not changes:
        return [], ["`changes` must be a non-empty list of {find, replace?, comment?, occurrence?}."]
    parsed: List[Change] = []
    problems: List[str] = []
    for index, raw in enumerate(changes):
        label = f"Change {index + 1}"
        if not isinstance(raw, dict):
            problems.append(f"{label}: expected an object with `find`.")
            continue
        find, replace, comment, occurrence = (
            raw.get("find"), raw.get("replace"), raw.get("comment"), raw.get("occurrence")
        )
        if not isinstance(find, str):
            problems.append(f"{label}: `find` must be text.")
            continue
        if replace is not None and not isinstance(replace, str):
            problems.append(f"{label}: `replace` must be text.")
            continue
        if comment is not None and not isinstance(comment, str):
            problems.append(f"{label}: `comment` must be text.")
            continue
        if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int)):
            problems.append(f"{label}: `occurrence` must be a whole number.")
            continue
        parsed.append(Change(find, replace, comment, occurrence))
    return parsed, problems


def _default_redline_name(source_base: str) -> str:
    """``<source>-redline``, reduced to what ``_validate_document_name`` accepts.

    Real file names carry spaces and punctuation ("Q3 Budget (draft)"), and
    the validator allows only letters, digits, ``_`` and single inner hyphens.
    Accents are folded to ASCII, every other run of disallowed characters
    becomes one hyphen, and a name with nothing left falls back to "document".
    A name the model passes is still validated as given.
    """
    ascii_base = unicodedata.normalize("NFKD", source_base).encode("ascii", "ignore").decode()
    safe = re.sub(r"[^A-Za-z0-9_]+", "-", ascii_base).strip("-")
    return f"{safe or 'document'}-redline"


def make_redline_word_document_tool(session_id: str, user_id: str, user_name: Optional[str]):
    """Create a ``redline_word_document`` tool bound to the given identity.

    Built only while ``WORD_TRACKED_CHANGES_ENABLED`` is on
    (``docs/specs/word-tracked-changes.md`` §3). ``user_name`` is the default
    author of each revision: a redline goes back to the counterparty as the
    user's.
    """

    @tool
    async def redline_word_document(
        document_name: str,
        changes: List[Dict[str, Any]],
        author: Optional[str] = None,
        output_name: Optional[str] = None,
    ) -> Any:
        """Propose edits to a Word (.docx) document as tracked changes and comments.

        Use this instead of modify_word_document whenever the user wants a
        redline, markup, suggested edits or comments: on a contract, policy or
        anything someone else will review. Each change shows in Word's
        Reviewing pane and can be accepted or rejected. The original is kept;
        the redline is saved as a new file and returned as a download card.

        All or nothing: if any change cannot be placed, nothing is saved and
        the error names each problem, so fix those changes and call again.

        Args:
            document_name: The document to mark up (with or without .docx),
                e.g. "services-agreement". Use list_word_documents if unsure.
            changes: The edits, in any order. Each is an object:
                - find: wording copied exactly from ONE paragraph (quotes,
                  dashes and spacing may differ). Include enough words to be
                  unique, or pass occurrence.
                - replace: the new wording for `find`; "" deletes it. Give the
                  whole new phrase: only the words that differ are marked, so
                  {"find": "within thirty (30) days", "replace": "within
                  ninety (90) days"} marks just "thirty (30)" → "ninety (90)".
                  To insert, repeat the neighbouring words in both.
                - comment: optional comment on the change, or on `find` alone
                  when there is no `replace`.
                - occurrence: 1-based, only when `find` appears more than once.
            author: Name shown on each change. Defaults to the user's name.
            output_name: File name for the redline, without extension.
                Defaults to "<document_name>-redline".

        Returns:
            A download card for the redline and one line per change applied.
        """
        if not _storage_configured():
            return _error(_NO_STORAGE_MESSAGE)

        parsed, problems = _parse_changes(changes)
        if problems:
            return _error("❌ Nothing was changed:\n" + "\n".join(f"- {p}" for p in problems))

        source = await _find_word_document(user_id, session_id, document_name)
        if source is None:
            return _error(
                f"❌ No Word document named '{document_name}' was found in this "
                "chat. Use list_word_documents to see what's available."
            )

        source_base = source.filename[: -len(".docx")] if source.filename.lower().endswith(".docx") else source.filename
        out_base = output_name or _default_redline_name(source_base)
        if out_base.lower().endswith(".docx"):
            out_base = out_base[: -len(".docx")]
        is_valid, error_msg = _validate_document_name(out_base)
        if not is_valid:
            return _error(f"❌ Invalid output name '{out_base}': {error_msg}")
        output_filename = f"{out_base}.docx"
        reviewer = (author or user_name or "").strip() or "Reviewer"

        try:
            source_bytes = await asyncio.to_thread(
                _download_s3_bytes, source.s3_bucket, source.s3_key
            )
            result = await asyncio.to_thread(redline, source_bytes, parsed, reviewer)
        except RedlineError as exc:
            return _error(
                f"❌ Nothing was changed in '{source.filename}':\n"
                + "\n".join(f"- {p}" for p in exc.problems)
            )
        except Exception as exc:  # noqa: BLE001 - surface any read/parse error
            logger.error(f"redline_word_document error: {exc}")
            return _error(f"❌ Failed to redline '{source.filename}': {exc}")

        try:
            upload_id, size_kb = await _store_document(
                user_id, session_id, output_filename, result.docx, _DOCX_MIME
            )
        except Exception as exc:  # noqa: BLE001 - storage failure is terminal
            logger.error(f"redline_word_document storage error: {exc}")
            return _error(
                f"❌ Redlined '{source.filename}' but failed to save it: {exc}"
            )

        details = f"Tracked changes by {reviewer}: " + "; ".join(result.applied) + "."
        return _download_card(output_filename, upload_id, size_kb, "Redlined", details)

    return redline_word_document


def make_list_word_documents_tool(session_id: str, user_id: str):
    """Create a ``list_word_documents`` tool bound to the given identity."""

    @tool
    async def list_word_documents() -> Dict[str, Any]:
        """List the Word (.docx) documents available in this chat.

        Returns the file names and sizes of documents created or modified in
        this conversation. Use the names with modify_word_document or
        read_word_document.
        """
        from apis.shared.files import FileStatus, get_file_upload_repository

        files = await get_file_upload_repository().list_session_files(
            session_id, status=FileStatus.READY
        )
        seen: set[str] = set()
        rows = []
        for meta in files:  # newest-first
            if meta.user_id != user_id or meta.mime_type != _DOCX_MIME:
                continue
            if meta.filename in seen:
                continue
            seen.add(meta.filename)
            rows.append(f"- {meta.filename} ({meta.size_bytes / 1024:.1f} KB)")

        if not rows:
            text = (
                "No Word documents in this chat yet. Use create_word_document "
                "to make one."
            )
        else:
            text = "Word documents in this chat:\n" + "\n".join(rows)
        return {"content": [{"text": text}], "status": "success"}

    return list_word_documents


def make_read_word_document_tool(session_id: str, user_id: str):
    """Create a ``read_word_document`` tool bound to the given identity."""

    @tool
    async def read_word_document(document_name: str) -> Dict[str, Any]:
        """Read the text content of an existing Word (.docx) document.

        Extracts headings, paragraphs, and tables from a document created in
        this chat so you can reference or summarize its contents. Use
        list_word_documents first if unsure of the exact name.

        Args:
            document_name: Name of the document to read (with or without the
                .docx extension), e.g. "sales-report".

        Returns:
            The document's text content.
        """
        code_interpreter_id = _get_code_interpreter_id()
        if not code_interpreter_id:
            return _error(_NO_CI_MESSAGE)
        if not _storage_configured():
            return _error(_NO_STORAGE_MESSAGE)

        source = await _find_word_document(user_id, session_id, document_name)
        if source is None:
            return _error(
                f"❌ No Word document named '{document_name}' was found in this "
                "chat. Use list_word_documents to see what's available."
            )

        try:
            source_bytes = await asyncio.to_thread(
                _download_s3_bytes, source.s3_bucket, source.s3_key
            )
            # python-docx reads only direct-child runs, so both sides of a
            # tracked change are invisible to it until they are written out.
            from apis.shared.files.document_read import annotate_docx_for_model

            source_bytes = await asyncio.to_thread(
                annotate_docx_for_model, source_bytes
            )
            text = await asyncio.to_thread(
                _extract_docx_text, code_interpreter_id, source_bytes
            )
        except _DocGenError as exc:
            return _error(f"❌ Failed to read '{source.filename}': {exc}")
        except Exception as exc:  # noqa: BLE001 - surface any sandbox error
            logger.error(f"read_word_document error: {exc}")
            return _error(f"❌ Failed to read '{source.filename}': {exc}")

        body = text or "(The document has no extractable text.)"
        return {
            "content": [
                {"text": f"Content of {source.filename}:\n\n{body}"}
            ],
            "status": "success",
        }

    return read_word_document
