"""``project_files`` / ``knowledge_files`` — list a knowledge base's documents and read one by name.

One tool, two voices. ``project_files`` is a project harness's (its Files);
``knowledge_files`` is an ordinary Agent's (its Knowledge, behind
``AGENT_KNOWLEDGE_FILES_ENABLED``). The bodies are the same functions; only the
name, the docstring and the words of the refusals differ (:class:`FilesToolVoice`),
so the two cannot drift in bounds, access or matching.

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
* **One voice per turn.** ``project_files`` is built on a project-harness turn
  (``_build_turn_tools`` in ``routes.py``, when ``turn_project_id`` is set);
  ``knowledge_files`` on every other Agent turn while its flag is on, never on a
  harness turn. An Agent's Loader instructions ("read Process.txt in your
  Knowledge in full") only worked in full-context mode: over budget, retrieval
  handed the model five unnamed passages, and one Agent went looking for the
  file in the user's Google Drive (2026-11 review). One tool with two modes
  (list, then read by ``filename``) rather than two, so the ``toolConfig``
  prefix grows by one spec.
* **Spec constant per deployment.** Each voice's name, docstring and signature
  are fixed; nothing about the project, the Agent, its files or the user
  reaches the spec, so every session shares one cached ``toolConfig``. A test
  pins each voice's digest.
* **The Knowledge voice names nobody.** A shared Agent's viewers are not its
  authors, so its listing carries no ``added_by`` (a project's members are
  colleagues, and its listing does).
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
* **Never offloaded.** Both voices are in the tool-result offloader's
  ``OFFLOAD_EXEMPT_TOOLS``, as ``document_read`` is: the page is the bound
  (a full one measured 9,149 tokens on Haiku 4.5), and an offloaded page came
  back as a preview of one escaped JSON line that line search could not split,
  so a section past the preview was unreachable.
* **Costs nothing before the first token.** The builder makes closures; the
  DynamoDB Query, the membership read and the S3 fetch run only when the model
  calls the tool.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from strands import tool

from apis.shared.assistants.kb_documents import kb_document_format, kb_document_text, list_kb_documents
from apis.shared.auth.models import User
from apis.shared.files.workspace import WORKSPACE_READ_MAX_BYTES

logger = logging.getLogger(__name__)

#: The tool's name — also what ``PROJECT_WORKSPACE_GUIDANCE`` tells the model to call.
PROJECT_FILES_TOOL_NAME = "project_files"

#: An ordinary Agent's voice of the same tool — what ``KNOWLEDGE_FILES_EXCERPTS_NOTE``
#: in ``rag_service`` names.
KNOWLEDGE_FILES_TOOL_NAME = "knowledge_files"

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
#: A file that is not ready is a normal answer, not a failed call: it was uploaded
#: fine and is still being prepared (or its preparation failed), so the result is
#: reported as a success and the chat does not show the call as failed.
NOT_READY = (
    "'{name}' is still being prepared (status: {status}), so it can't be read yet. Files are "
    "usually ready within a few minutes of upload. Answer from the ready files, tell the member "
    "this one isn't ready, and don't call this tool for it again this turn."
)
NOT_ADDED = (
    "'{name}' was not added to the project (its preparation failed), so it can't be read. "
    "Tell the member they can upload it again from the project's Files page."
)
NOT_TEXT = (
    "'{name}' is a {kind}, which this tool can't read as text. It reads PDF, Word, text, "
    "Markdown and HTML files."
)
NO_TEXT = (
    "'{name}' has no text to read: it is either a scanned document with no text layer or larger "
    "than this tool reads whole."
)
UNAVAILABLE = "The project's files can't be read right now. Try again, or ask the member to check the Files page."
AMBIGUOUS = (
    "{count} files are named '{name}': {described}. Tell the member, and ask them to "
    "rename or remove one on the Files page so each file has one name."
)
UNKNOWN = "No file named '{name}' in this project's Files.{suggestion} Call with no arguments to list them."

#: The Knowledge voice's words. An Agent's user is often not its author, so the
#: fixes are the author's, and the documents are never in the user's own storage.
KNOWLEDGE_NO_ACCESS = "You don't have access to this assistant's Knowledge."
KNOWLEDGE_NOT_READY = (
    "'{name}' is still being prepared (status: {status}), so it can't be read yet. Documents are "
    "usually ready within a few minutes of upload. Answer from the ready documents, tell the user "
    "this one isn't ready, and don't call this tool for it again this turn."
)
KNOWLEDGE_NOT_ADDED = (
    "'{name}' was not added to this assistant's Knowledge (its preparation failed), so it can't be "
    "read. Tell the user; the assistant's author can upload it again in the Agent Designer."
)
KNOWLEDGE_UNAVAILABLE = "This assistant's Knowledge can't be read right now. Try again shortly."
KNOWLEDGE_AMBIGUOUS = (
    "{count} documents are named '{name}': {described}. Tell the user; the assistant's author "
    "can rename or remove one in the Agent Designer so each document has one name."
)
KNOWLEDGE_UNKNOWN = (
    "No document named '{name}' in this assistant's Knowledge.{suggestion} Call with no arguments "
    "to list them. Knowledge is not in the user's Drive or other storage, so don't look for it there."
)

#: The ``project_files`` docstring, which is its spec's description. Byte for byte
#: what it was before the voices: a change re-writes every project's cached
#: ``toolConfig`` prefix.
_PROJECT_FILES_DOC = """List this project's Files, or read one of them by name.

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

_KNOWLEDGE_FILES_DOC = """List this assistant's Knowledge documents, or read one of them by name.

        Knowledge is the reference material the assistant's author added to
        it. The passages most relevant to each message are retrieved for you
        automatically; use this tool when that is not enough: your
        instructions or the user name a document, a task needs one document
        whole (follow a process, fill in a template, check a policy), or the
        user asks what the Knowledge holds. Knowledge is not in the user's
        Google Drive, OneDrive or other connected storage; read it here.

        - No arguments: every document with its type, status and size. Only a
          document listed as ready can be read.
        - filename: that document's whole text (exact name, else
          case-insensitive), at most 48 KB per call; when the result is
          truncated, call again with offset = next_offset for the rest.
          Spreadsheets, presentations and images have no text to read here.

        Args:
            filename: The document to read, as listed. Empty to list them.
            offset: Byte offset to continue a truncated read.
        """


@dataclass(frozen=True)
class FilesToolVoice:
    """What one tool says. The behaviour behind every voice is the same."""

    name: str
    doc: str
    no_access: str
    not_ready: str
    not_added: str
    unavailable: str
    ambiguous: str
    unknown: str
    #: List entries carry who added each file (project members are colleagues).
    lists_added_by: bool
    #: List entries carry the document's type (``pdf``, ``docx``, ``spreadsheet``…).
    lists_type: bool


PROJECT_FILES_VOICE = FilesToolVoice(
    name=PROJECT_FILES_TOOL_NAME,
    doc=_PROJECT_FILES_DOC,
    no_access=NO_ACCESS,
    not_ready=NOT_READY,
    not_added=NOT_ADDED,
    unavailable=UNAVAILABLE,
    ambiguous=AMBIGUOUS,
    unknown=UNKNOWN,
    lists_added_by=True,
    lists_type=False,
)

KNOWLEDGE_FILES_VOICE = FilesToolVoice(
    name=KNOWLEDGE_FILES_TOOL_NAME,
    doc=_KNOWLEDGE_FILES_DOC,
    no_access=KNOWLEDGE_NO_ACCESS,
    not_ready=KNOWLEDGE_NOT_READY,
    not_added=KNOWLEDGE_NOT_ADDED,
    unavailable=KNOWLEDGE_UNAVAILABLE,
    ambiguous=KNOWLEDGE_AMBIGUOUS,
    unknown=KNOWLEDGE_UNKNOWN,
    lists_added_by=False,
    lists_type=True,
)




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


def _type_label(doc: Mapping[str, Any]) -> str:
    """What kind of document a listing says this is: its text format, else its kind."""
    name = str(doc.get("filename") or "")
    fmt = kb_document_format(doc)
    if fmt:
        return fmt
    kind = _kind(name)
    return "other" if kind == "file of a type" else kind


def list_kb_files(
    assistant_id: str,
    *,
    voice: FilesToolVoice,
    emails: Optional[Callable[[], Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    """The listing payload: every document, deterministically ordered, bounded.

    ``emails`` returns ``user id → email``; it is called only by a voice that
    lists who added each file, and only when there is a file to describe.
    """
    docs = sorted(list_kb_documents(assistant_id), key=_sort_key)
    added_by_email = emails() if (voice.lists_added_by and emails and docs) else {}
    files: List[Dict[str, Any]] = []
    for doc in docs[:PROJECT_FILES_LIST_MAX]:
        entry: Dict[str, Any] = {
            "filename": str(doc.get("filename") or doc.get("documentId") or ""),
        }
        if voice.lists_type:
            entry["type"] = _type_label(doc)
        entry["status"] = "ready" if _is_ready(doc) else f"not ready ({_status(doc)})"
        entry["size_bytes"] = int(doc.get("sizeBytes") or 0)
        if added_by_email:
            added_by = doc.get("addedByUserId") or doc.get("importedByUserId")
            email = added_by_email.get(str(added_by)) if added_by else None
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


def list_project_files(project_id: str, assistant_id: str) -> Dict[str, Any]:
    """A project's Files, with who added each one."""
    return list_kb_files(assistant_id, voice=PROJECT_FILES_VOICE, emails=lambda: member_emails(project_id))


def match_project_file(
    docs: List[Mapping[str, Any]], filename: str, *, voice: FilesToolVoice = PROJECT_FILES_VOICE
) -> Mapping[str, Any]:
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
        raise ProjectFilesError(voice.ambiguous.format(count=len(exact), name=wanted, described=described))
    needle = wanted.casefold()
    near = [
        str(d.get("filename"))
        for d in sorted(docs, key=_sort_key)
        if needle and (needle in str(d.get("filename") or "").casefold() or str(d.get("filename") or "").casefold() in needle)
    ][:_CANDIDATES_MAX]
    suggestion = f" Did you mean: {', '.join(repr(n) for n in near)}?" if near else ""
    raise ProjectFilesError(voice.unknown.format(name=wanted, suggestion=suggestion))


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


def read_project_file(
    assistant_id: str, filename: str, offset: int, *, voice: FilesToolVoice = PROJECT_FILES_VOICE
) -> Dict[str, Any]:
    """One page of a document's text, located by name. Blocking."""
    if offset < 0:
        raise ProjectFilesError("offset must be 0 or more.")
    docs = list_kb_documents(assistant_id)
    doc = match_project_file(docs, filename, voice=voice)
    name = str(doc.get("filename") or "")
    if not _is_ready(doc):
        status = _status(doc)
        message = voice.not_added if status == "failed" else voice.not_ready
        return {
            "mode": "not_ready",
            "filename": name,
            "status": status,
            "message": message.format(name=name, status=status),
        }
    fmt = kb_document_format(doc)
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


def _make_files_tool(voice: FilesToolVoice, assistant_id: str, member: User, list_files, scope: str):
    """The tool in ``voice``, bound to one knowledge base and the invoking user.

    ``list_files`` is the zero-argument listing for this voice (a project's adds
    who added each file). ``scope`` names the binding in the failure log only.
    """
    # A copy without the bearer token: the tool may outlive this request in the agent cache.
    user = User(email=member.email, user_id=member.user_id, name=member.name, roles=list(member.roles or []))

    async def _access_denied() -> bool:
        from apis.shared.assistants.kb_access import resolve_kb_access

        access = await resolve_kb_access(assistant_id, user.user_id, user_email=user.email)
        return access is None or not access.may_read

    async def files_tool(filename: str = "", offset: int = 0) -> Dict[str, Any]:
        try:
            if await _access_denied():
                return _error(voice.no_access)
            if not filename.strip():
                payload = await asyncio.to_thread(list_files)
            else:
                payload = await asyncio.to_thread(
                    read_project_file, assistant_id, filename, int(offset or 0), voice=voice
                )
        except ProjectFilesError as exc:
            return _error(str(exc))
        except Exception:
            logger.warning("%s failed for %s", voice.name, scope, exc_info=True)
            return _error(voice.unavailable)
        logger.info(
            "%s: mode=%s files=%s bytes=%s",
            voice.name, payload.get("mode"), payload.get("count", 1), len(payload.get("content", "")),
        )
        return {"content": [{"json": payload}], "status": "success"}

    # The docstring is the spec's description and its Args are the parameters'
    # descriptions, so each voice's is set before decorating.
    files_tool.__doc__ = voice.doc
    files_tool.__name__ = files_tool.__qualname__ = voice.name
    return tool(name=voice.name)(files_tool)


def make_project_files_tool(project_id: str, assistant_id: str, member: User):
    """``project_files`` bound to one project's harness and the invoking member."""
    return _make_files_tool(
        PROJECT_FILES_VOICE,
        assistant_id,
        member,
        lambda: list_project_files(project_id, assistant_id),
        f"project {project_id}",
    )


def make_knowledge_files_tool(assistant_id: str, user: User):
    """``knowledge_files`` bound to one Agent's knowledge base and the invoking user."""
    return _make_files_tool(
        KNOWLEDGE_FILES_VOICE,
        assistant_id,
        user,
        lambda: list_kb_files(assistant_id, voice=KNOWLEDGE_FILES_VOICE),
        f"assistant {assistant_id}",
    )


__all__ = [
    "FilesToolVoice",
    "KNOWLEDGE_FILES_TOOL_NAME",
    "KNOWLEDGE_FILES_VOICE",
    "PROJECT_FILES_LIST_MAX",
    "PROJECT_FILES_VOICE",
    "PROJECT_FILES_TOOL_NAME",
    "PROJECT_FILE_TEXT_MAX_CHARS",
    "ProjectFilesError",
    "list_kb_files",
    "list_project_files",
    "make_knowledge_files_tool",
    "make_project_files_tool",
    "match_project_file",
    "read_project_file",
]
