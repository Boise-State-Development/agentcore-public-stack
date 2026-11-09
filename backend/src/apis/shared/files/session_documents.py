"""Which of a conversation's documents the model meant, when it did not give an id.

A small attachment reaches the model as an inline document block named by its
filename, and no upload id appears anywhere in the conversation. So a model
asked to work on "the attached solicitation" naturally sends the filename, or
nothing, where a tool wants an upload id. Both ``verify_sources`` and
``document_extract`` take what it sent and resolve it here, against the
session's own documents, instead of failing and making it list them first.

Pure: it takes the listing ``list_session_documents`` already returns and does
no I/O, so each tool decides when the listing is worth a query.

The rules, in order:

1. An exact upload id is that document.
2. A filename that matches exactly one document is that document. Matching is
   on :func:`name_key`, so ``facilities_policy.md``, the inline block's
   ``facilities_policy_md`` and a bare ``Facilities Policy`` agree.
3. One document asked for, and the session holds exactly one: that one, since
   there is nothing else the model can have meant.

Anything else is ambiguous, and the caller says so with :func:`document_choices`
so the next call can name one. Nothing here ever picks between two candidates.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

#: How many documents an ambiguity error lists. Beyond this the model is told
#: to call ``document_read`` with no arguments for the whole listing.
MAX_LISTED_DOCUMENTS = 20


def name_key(name: str) -> str:
    """A name reduced to what survives Bedrock's document-name sanitizer.

    The inline block for ``facilities_policy.md`` is named
    ``facilities_policy_md``, the ``[Attached files: …]`` marker says
    ``facilities_policy.md``, and a model may drop the extension or the case.
    All of them reduce to the same key.
    """
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def filename_keys(filename: str) -> Set[str]:
    """The keys a filename answers to: with and without its extension."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return {name_key(filename), name_key(stem)} - {""}


def match_session_document(
    documents: List[Dict[str, Any]], requested: str, *, only_request: bool = True
) -> Optional[str]:
    """The upload id ``requested`` names among ``documents``, or ``None``.

    ``only_request`` is whether this is the only document the caller asked
    for. Rule 3 (the session's sole document) applies only then: a model that
    names three documents in a one-document session has not meant that
    document three times.
    """
    requested = (requested or "").strip()
    if requested and any(d["upload_id"] == requested for d in documents):
        return requested

    key = name_key(requested)
    if key:
        named = [d for d in documents if key in filename_keys(d["filename"])]
        if len(named) == 1:
            return named[0]["upload_id"]
        if len(named) > 1:
            return None

    if only_request and len(documents) == 1:
        return documents[0]["upload_id"]
    return None


def document_choices(documents: Iterable[Dict[str, Any]]) -> str:
    """The session's documents as ``'id' (filename)``, bounded, for an error."""
    documents = list(documents)
    shown = documents[:MAX_LISTED_DOCUMENTS]
    choices = ", ".join(f"'{d['upload_id']}' ({d['filename']})" for d in shown)
    if len(documents) > len(shown):
        choices += (
            f" and {len(documents) - len(shown)} more (call document_read with "
            "no arguments for all)"
        )
    return choices


__all__ = [
    "MAX_LISTED_DOCUMENTS",
    "document_choices",
    "filename_keys",
    "match_session_document",
    "name_key",
]
