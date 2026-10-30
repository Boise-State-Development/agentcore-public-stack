"""``project_files`` — a project harness lists the project's Files and reads one by name.

A project's Files are its hidden harness Agent's knowledge base: ``DOC#`` rows
under ``AST#{harnessAgentId}``, indexed into a managed Bedrock knowledge base.
Every turn retrieves the passages most relevant to the member's *message*
(``_search_and_augment`` in ``inference_api/chat/routes.py``, or the whole
knowledge base in the system prompt when it is small enough). That answers
"what does the playbook say about X" and cannot answer "grade submission 7" or
"what is in Files": retrieval by wording has no notion of a file. A faculty
project with 41 submissions reported exactly that (2026-10-29): the assistant
said it had no way to read the files, because ``list_word_documents`` and
``document_read`` only see the *conversation's* uploads.

Design notes
------------
* **Harness only.** Built on a project-harness turn
  (``_build_turn_tools`` in ``routes.py``, when ``turn_project_id`` is set)
  and never for an ordinary Agent, whose knowledge base is reached through
  retrieval alone. One tool with two modes (list, then read by ``filename``)
  rather than two, so the ``toolConfig`` prefix grows by one spec.
* **Spec constant per deployment.** The tool's name, docstring and signature
  are fixed; nothing about the project, its files or the member reaches the
  spec, so every session of every project shares one cached ``toolConfig``.
  A test pins the spec's digest.
* **Access is resolved on every call, the way retrieval resolves it**
  (``kb_access.resolve_kb_access``: the project membership model, failing
  closed), never read from the agent instance. The tool may outlive the turn
  in the agent cache, so a member removed mid-session is refused on their
  next call. Identity is captured by closure, never from ``invocation_state``.
* **Bounded.** A listing is capped at :data:`PROJECT_FILES_LIST_MAX` entries
  and says how many more there are. A read returns at most
  ``WORKSPACE_READ_MAX_BYTES`` (48 KB) per call, the same page ``document_read``
  uses for text documents, continued with ``offset``; the whole text is
  extracted once per process (``kb_documents.kb_document_text``'s cache) so
  paging does not re-download. A document past
  :data:`PROJECT_FILE_TEXT_MAX_CHARS` is reported, not sampled.
* **Costs nothing before the first token.** The builder makes closures; the
  DynamoDB Query, the membership read and the S3 fetch run only when the model
  calls the tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Optional

from strands import tool

from apis.shared.assistants.kb_documents import kb_document_text, list_kb_documents
from apis.shared.auth.models import User
from apis.shared.files.document_read import document_format_for
from apis.shared.files.workspace import WORKSPACE_READ_MAX_BYTES

logger = logging.getLogger(__name__)

#: The tool's name — also what ``PROJECT_WORKSPACE_GUIDANCE`` tells the model to call.
PROJECT_FILES_TOOL_NAME = "project_files"

#: Entries a listing carries before it says "and N more". A tool result is a
#: per-turn payload; 100 rows is ~10 KB.
PROJECT_FILES_LIST_MAX = 100

#: Characters of text a single file may hold to be readable here (~250K
#: tokens, 20 pages of 48 KB). Larger files are reported as too large rather
#: than sampled, so the model never mistakes an excerpt for the whole.
PROJECT_FILE_TEXT_MAX_CHARS = 1_000_000

#: Near-miss names offered when no file matches.
_CANDIDATES_MAX = 5

NO_ACCESS = "You don't have access to this project's files."
NOT_READY = "'{name}' is not ready yet (status: {status}), so it can't be read. Only files listed as ready can be."
NOT_TEXT = (
    "'{name}' is a {kind}, which this tool can't read as text. It reads PDF, Word, text, "
    "Markdown and HTML files."
)
NO_TEXT = (
    "'{name}' has no text to read: it is either a scanned document with no text layer or larger "
    "than this tool reads whole."
)
UNAVAILABLE = "The project's files can't be read right now. Try again, or ask the member to check the Files page."


class ProjectFilesError(Exception):
    """A refusal the model should see verbatim as an error result."""


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": f"❌ {text}"}], "status": "error"}


def _status(doc: Mapping[str, Any]) -> str:
    return str(doc.get("status") or "unknown")


def _is_ready(doc: Mapping[str, Any]) -> bool:
    return _status(doc) == "complete" and bool(doc.get("s3Key"))


def _sort_key(doc: Mapping[str, Any]):
    name = str(doc.get("filename") or "")
    return (name.casefold(), name, str(doc.get("documentId") or ""))


def member_emails(project_id: str) -> Dict[str, str]:
    """``user id → email`` for the project's members and owner, for ``added_by``.

    One Get and one Query on the projects table, only when the model lists.
    Best effort: a failure leaves ``added_by`` out rather than failing the list.
    """
    from apis.shared.projects.repository import ProjectRepository

    try:
        repository = ProjectRepository()
        emails = {m.user_id: m.email for m in repository.list_members(project_id) if m.user_id}
        project = repository.get_project(project_id)
        if project is not None and project.owner_id:
            emails[project.owner_id] = project.owner_email
        return emails
    except Exception:
        logger.warning("project_files: could not resolve member emails for project %s", project_id, exc_info=True)
        return {}


def list_project_files(project_id: str, assistant_id: str) -> Dict[str, Any]:
    """The listing payload: every file, deterministically ordered, bounded."""
    docs = sorted(list_kb_documents(assistant_id), key=_sort_key)
    emails = member_emails(project_id) if docs else {}
    files: List[Dict[str, Any]] = []
    for doc in docs[:PROJECT_FILES_LIST_MAX]:
        entry: Dict[str, Any] = {
            "filename": str(doc.get("filename") or doc.get("documentId") or ""),
            "status": "ready" if _is_ready(doc) else f"not ready ({_status(doc)})",
            "size_bytes": int(doc.get("sizeBytes") or 0),
        }
        added_by = doc.get("addedByUserId") or doc.get("importedByUserId")
        email = emails.get(str(added_by)) if added_by else None
        if email:
            entry["added_by"] = email
        files.append(entry)
    omitted = max(0, len(docs) - len(files))
    return {
        "mode": "list",
        "count": len(docs),
        "ready_count": sum(1 for d in docs if _is_ready(d)),
        "files": files,
        "truncated": omitted > 0,
        "omitted": omitted,
        "hint": "Read a file with filename=<name as listed>. Only ready files can be read.",
    }


def match_project_file(docs: List[Mapping[str, Any]], filename: str) -> Mapping[str, Any]:
    """The one document named ``filename``: exact, else case-insensitive.

    Raises :class:`ProjectFilesError` naming the candidates when the name is
    ambiguous (two files with one name) or unknown (near misses offered).
    """
    wanted = filename.strip()
    exact = [d for d in docs if str(d.get("filename") or "") == wanted]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        folded = wanted.casefold()
        exact = [d for d in docs if str(d.get("filename") or "").casefold() == folded]
        if len(exact) == 1:
            return exact[0]
    if len(exact) > 1:
        described = "; ".join(
            f"'{d.get('filename')}' ({int(d.get('sizeBytes') or 0)} bytes, added {d.get('createdAt') or 'unknown'})"
            for d in sorted(exact, key=_sort_key)
        )
        raise ProjectFilesError(
            f"{len(exact)} files are named '{wanted}': {described}. Tell the member, and ask them to "
            "rename or remove one on the Files page so each file has one name."
        )
    needle = wanted.casefold()
    near = [
        str(d.get("filename"))
        for d in sorted(docs, key=_sort_key)
        if needle and (needle in str(d.get("filename") or "").casefold() or str(d.get("filename") or "").casefold() in needle)
    ][:_CANDIDATES_MAX]
    suggestion = f" Did you mean: {', '.join(repr(n) for n in near)}?" if near else ""
    raise ProjectFilesError(
        f"No file named '{wanted}' in this project's Files.{suggestion} Call with no arguments to list them."
    )


def _kind(filename: str) -> str:
    """What to call a file this tool cannot read as text."""
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xls", ".csv", ".tsv")):
        return "spreadsheet"
    if lowered.endswith((".pptx", ".ppt")):
        return "presentation"
    if lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff")):
        return "image"
    return "file of a type"


def read_project_file(assistant_id: str, filename: str, offset: int) -> Dict[str, Any]:
    """One page of a file's text, located by name. Blocking."""
    if offset < 0:
        raise ProjectFilesError("offset must be 0 or more.")
    docs = list_kb_documents(assistant_id)
    doc = match_project_file(docs, filename)
    name = str(doc.get("filename") or "")
    if not _is_ready(doc):
        raise ProjectFilesError(NOT_READY.format(name=name, status=_status(doc)))
    content_type = str(doc.get("contentType") or "")
    fmt = document_format_for(content_type, name)
    if fmt is None:
        raise ProjectFilesError(NOT_TEXT.format(name=name, kind=_kind(name)))
    text = kb_document_text(doc, fmt, PROJECT_FILE_TEXT_MAX_CHARS)
    if text is None:
        raise ProjectFilesError(NO_TEXT.format(name=name))
    encoded = text.encode("utf-8")
    if offset and offset >= len(encoded):
        raise ProjectFilesError(f"offset {offset} is beyond the end of '{name}' ({len(encoded)} bytes).")
    chunk = encoded[offset: offset + WORKSPACE_READ_MAX_BYTES]
    end = offset + len(chunk)
    truncated = end < len(encoded)
    return {
        "mode": "text",
        "filename": name,
        "format": fmt,
        "size_bytes": int(doc.get("sizeBytes") or 0),
        "text_bytes": len(encoded),
        "content": chunk.decode("utf-8", errors="replace"),
        "offset": offset,
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }


def make_project_files_tool(project_id: str, assistant_id: str, member: User):
    """``project_files`` bound to one project's harness and the invoking member."""
    # A copy without the bearer token: the tool may outlive this request in the agent cache.
    user = User(email=member.email, user_id=member.user_id, name=member.name, roles=list(member.roles or []))

    async def _access_denied() -> bool:
        from apis.shared.assistants.kb_access import resolve_kb_access

        access = await resolve_kb_access(assistant_id, user.user_id, user_email=user.email)
        return access is None or not access.may_read

    @tool(name=PROJECT_FILES_TOOL_NAME)
    async def project_files(filename: str = "", offset: int = 0) -> Dict[str, Any]:
        """List this project's Files, or read one of them by name.

        The project's Files are the documents members added on the project's
        Files page. The passages most relevant to each message are retrieved
        for you automatically; use this tool when that is not enough: a member
        names a file, asks for work on one document (grade this submission,
        review that draft, compare two files), or asks what the project holds.

        - No arguments: every file with its status and size. Only a file
          listed as ready can be read.
        - filename: that file's whole text (exact name, else
          case-insensitive), at most 48 KB per call; when the result is
          truncated, call again with offset = next_offset for the rest.
          Spreadsheets, presentations and images have no text to read here.

        Args:
            filename: The file to read, as listed. Empty to list the files.
            offset: Byte offset to continue a truncated read.
        """
        try:
            if await _access_denied():
                return _error(NO_ACCESS)
            if not filename.strip():
                payload = await asyncio.to_thread(list_project_files, project_id, assistant_id)
            else:
                payload = await asyncio.to_thread(read_project_file, assistant_id, filename, int(offset or 0))
        except ProjectFilesError as exc:
            return _error(str(exc))
        except Exception:
            logger.warning("project_files failed for project %s", project_id, exc_info=True)
            return _error(UNAVAILABLE)
        logger.info(
            "project_files: mode=%s files=%s bytes=%s",
            payload.get("mode"), payload.get("count", 1), len(payload.get("content", "")),
        )
        return {"content": [{"json": payload}], "status": "success"}

    return project_files


__all__ = [
    "PROJECT_FILES_LIST_MAX",
    "PROJECT_FILES_TOOL_NAME",
    "PROJECT_FILE_TEXT_MAX_CHARS",
    "ProjectFilesError",
    "list_project_files",
    "make_project_files_tool",
    "match_project_file",
    "read_project_file",
]
