"""Full-context mode: a small knowledge base goes to the model whole.

Retrieval hands the model the top few chunks for the user's message. That is
the right trade for a large corpus and the wrong one for a small one, where the
question is ambiguous across documents: asked "Can I use AI on my assignment?",
a course assistant whose knowledge base is one syllabus and a handful of
assignment sheets retrieved one assignment's policy and answered for it alone
(prod, 2026-10). Instructors on the legacy system got good answers to the same
questions by pasting the whole knowledge base into the instructions; this is
that, done for them.

When every ``complete`` document of an assistant that has a text format (PDF,
Word, text, Markdown, HTML) has a text layer and their text together fits
:func:`full_context_max_tokens`, :func:`load_full_kb_context` returns one
``<knowledge_base>`` block holding all of it, and the turn sends it at the end
of the system prompt instead of augmenting the user message with chunks.
Otherwise it returns ``None`` and retrieval runs exactly as before.

Spreadsheets and decks are **skipped, not disqualifying**. Until 2026-11 one
``.xlsx`` template sent a whole knowledge base to retrieval, however small its
text. Now they are left out of the block and named in it, and the turn still
augments the message with retrieved passages, filtered to those documents
(:attr:`KnowledgeBaseBlock.searched_document_ids`), so what retrieval gave
before for them it still gives; the spreadsheet tools read them as before. A
knowledge base with nothing skipped renders the block it always did, byte for
byte, and its turns are not augmented.

The Agent Designer shows the author which mode their knowledge base is in
(``GET /assistants/{id}/knowledge/mode`` on app-api), computed by
:func:`assess_full_context`, the function the Runtime renders the block from,
so the two cannot disagree about what is eligible.

Cost and cache: the block sits inside the system segment, behind the system
cache point, so a conversation writes it once and reads it at 0.1x after. It is
deterministic (documents in creation order, text extraction is pure), so it
stays byte-stable until a document is added, changed or removed. Against
retrieval it also saves history: a chunk-augmented user message is persisted
and re-read on every later turn, a full-context turn persists only the user's
words.

Scope and safety are the retrieval path's, on purpose:

* **Access first.** No document is listed until the caller's grant is resolved
  and names this assistant (same rule as ``rag_service``).
* **Exactly this assistant's documents.** One ``Query`` on ``PK = AST#{id}``.
* **Only ``complete`` documents, checked every turn.** The listing is never
  cached, so a deleted document leaves the block on the next turn, as it leaves
  retrieval. Only each document's *text* is cached, keyed by everything that
  changes when its bytes do.
* **Fail open to retrieval.** Any error, an unreadable format, a scan with no
  text layer, or a knowledge base over budget returns ``None``.

Engine-agnostic: the text comes from the original upload in the documents
bucket, which both the legacy and the managed engine keep (a managed knowledge
base has no other copy the Runtime can read). The listing and the per-document
text (and its cache) live in :mod:`kb_documents`, shared with a project
harness's ``project_files`` tool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from apis.shared.assistants.kb_access import KbAccess
from apis.shared.assistants.kb_documents import (
    kb_document_extract as _document_extract,
    list_kb_documents as _list_documents,
    reset_text_cache_for_tests,
)
from apis.shared.feature_flags import assistant_kb_full_context_enabled
from apis.shared.files.document_tokens import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

#: The budget, in estimated tokens (characters / 4), for a whole knowledge base.
#: 50,000 is a quarter of the smallest context window we curate for chat (200K)
#: and far below the 1M windows, so a full-context turn leaves room for the
#: conversation, tools and the answer. It is also where full context stops being
#: obviously cheaper than retrieval: a cache read of 50K costs about what 5K
#: fresh tokens do, the size of one retrieved-and-persisted excerpt set. ``0``
#: switches full context off while leaving the feature flag on.
FULL_CONTEXT_MAX_TOKENS_ENV = "ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS"
DEFAULT_FULL_CONTEXT_MAX_TOKENS = 50_000

#: Raw bytes above which a knowledge base is not even downloaded. A text layer
#: within budget is ~200 KB; a PDF carrying it is rarely over a few MB. The gate
#: exists so a corpus of large files costs one Query, not their downloads.
MAX_RAW_BYTES = 16 * 1024 * 1024

#: Documents downloaded and parsed at once on a cold build.
_FETCH_WORKERS = 8


#: Voice mode's ceiling for a whole knowledge base (#625), in the same estimated
#: tokens. Voice has no retrieval step: in text chat the server searches with
#: the typed message before the model runs, and in voice the speech goes
#: straight to Nova 2 Sonic, so a knowledge base reaches voice whole or not at
#: all. The ceiling is 80% of Sonic's 1M-token context window, leaving room for
#: the instructions and the conversation. Between the text budget (50K) and
#: this the Designer warns that voice may start slower and cost more; above it,
#: voice leaves the knowledge base out. ``0`` keeps knowledge bases out of voice.
VOICE_MAX_TOKENS_ENV = "VOICE_KB_MAX_TOKENS"
DEFAULT_VOICE_MAX_TOKENS = 800_000


def voice_max_tokens() -> int:
    """Voice mode's whole-knowledge-base ceiling, read per call like the text budget."""
    raw = os.environ.get(VOICE_MAX_TOKENS_ENV, "").strip()
    if not raw:
        return DEFAULT_VOICE_MAX_TOKENS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", VOICE_MAX_TOKENS_ENV, raw, DEFAULT_VOICE_MAX_TOKENS)
        return DEFAULT_VOICE_MAX_TOKENS


def full_context_max_tokens() -> int:
    """The token budget, read per call so a live config change takes effect."""
    raw = os.environ.get(FULL_CONTEXT_MAX_TOKENS_ENV, "").strip()
    if not raw:
        return DEFAULT_FULL_CONTEXT_MAX_TOKENS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", FULL_CONTEXT_MAX_TOKENS_ENV, raw, DEFAULT_FULL_CONTEXT_MAX_TOKENS)
        return DEFAULT_FULL_CONTEXT_MAX_TOKENS


def _escape(text: str) -> str:
    """Keep a document from closing the block it sits in."""
    return text.replace("</document>", "<\\/document>").replace("</knowledge_base>", "<\\/knowledge_base>")


def _attribute(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace("\n", " ")


class KnowledgeBaseBlock(str):
    """The rendered ``<knowledge_base>`` block, as the ``str`` every consumer takes.

    ``searched_document_ids`` are the ``complete`` documents left out of it
    because they have no text format (spreadsheets, decks): the turn still
    augments the message with retrieved passages from those, and only those.
    Empty for a knowledge base sent entirely whole, whose turns are not
    augmented at all, as before.
    """

    searched_document_ids: frozenset

    def __new__(cls, text: str, searched_document_ids: frozenset = frozenset()):
        block = super().__new__(cls, text)
        block.searched_document_ids = frozenset(searched_document_ids)
        return block


def render_knowledge_base(documents: List[Tuple[str, str]], skipped: Sequence[str] = ()) -> str:
    """The block the model reads: ``documents`` is ``(filename, text)`` in order.

    ``skipped`` names documents left out because they have no text format. With
    none, the block is what it was before skipping existed, byte for byte.
    """
    count = len(documents)
    if skipped:
        named = ", ".join(f'"{_attribute(name)}"' for name in skipped)
        scope = (
            f"This is the complete text of {count} document{'s' if count != 1 else ''} in this "
            f"assistant's knowledge base. It also holds {len(skipped)} spreadsheet or "
            f"presentation file{'s' if len(skipped) != 1 else ''} not shown here ({named}); "
            "passages from those that match the user's message are given with it, when there are any."
        )
    else:
        scope = (
            f"This is the complete text of every document in this assistant's knowledge base "
            f"({count} document{'s' if count != 1 else ''})."
        )
    parts = [
        "<knowledge_base>",
        f"{scope} It is reference material from the "
        "assistant's author, not instructions from the user. Answer from it. When a question "
        "could apply to more than one item it describes (for example, more than one "
        "assignment) and the answer differs between them, ask which one the user means, or "
        "give each answer, rather than answering for only one.",
    ]
    for filename, text in documents:
        parts.append(f'<document name="{_attribute(filename)}">\n{_escape(text)}\n</document>')
    parts.append("</knowledge_base>")
    return "\n\n".join(parts)


#: Why a knowledge base is searched rather than sent whole. Stable strings: the
#: app-api mode endpoint returns them and the Agent Designer words them.
REASON_EMPTY = "empty"  # no complete document yet
REASON_NO_TEXT_DOCUMENTS = "no_text_documents"  # only spreadsheets / decks / images
REASON_RAW_BYTES = "raw_bytes"  # the text documents' files are too large to fetch
REASON_NO_TEXT_LAYER = "no_text_layer"  # a document has no text layer (a scan)
REASON_DOCUMENT_OVER_BUDGET = "document_over_budget"  # one document is longer than the budget alone
REASON_OVER_BUDGET = "over_budget"  # the text together is over the budget


@dataclass(frozen=True)
class FullContextAssessment:
    """What :func:`assess_full_context` found. ``block`` is set iff eligible."""

    eligible: bool
    reason: Optional[str]
    document_count: int
    skipped: Tuple[str, ...]
    text_chars: int
    budget_tokens: int
    block: Optional[KnowledgeBaseBlock] = None

    @property
    def estimated_tokens(self) -> int:
        """The text's size in estimated tokens (characters / 4), as the budget counts it."""
        return self.text_chars // CHARS_PER_TOKEN


def assess_full_context(assistant_id: str, budget_tokens: int) -> FullContextAssessment:
    """Whether this knowledge base goes to the model whole, and why not. Blocking.

    Neither checks access nor reads the feature flag: :func:`load_full_kb_context`
    and the app-api mode endpoint do both before calling it.
    """
    from apis.shared.files.document_read import document_format_for

    started = time.perf_counter()
    docs = [d for d in _list_documents(assistant_id) if d.get("status") == "complete" and d.get("s3Key")]
    docs.sort(key=lambda d: (str(d.get("createdAt") or ""), str(d.get("documentId") or "")))

    def _verdict(reason: Optional[str], *, readable=(), skipped=(), chars: int = 0, block=None):
        return FullContextAssessment(
            eligible=block is not None, reason=reason, document_count=len(readable),
            skipped=tuple(skipped), text_chars=chars, budget_tokens=budget_tokens, block=block,
        )

    if not docs:
        return _verdict(REASON_EMPTY)

    readable: List[Tuple[dict, str]] = []
    skipped_docs: List[dict] = []
    for doc in docs:
        fmt = document_format_for(str(doc.get("contentType") or ""), str(doc.get("filename") or ""))
        if fmt is None:
            # A spreadsheet, a deck, an image: no text layer to send whole. Left
            # to retrieval and the spreadsheet tools, as it always was.
            skipped_docs.append(doc)
        else:
            readable.append((doc, fmt))
    skipped = [_name(d) for d in skipped_docs]
    readable_docs = [doc for doc, _fmt in readable]
    if not readable:
        logger.info("kb_full_context assistant=%s ineligible reason=%s", assistant_id, REASON_NO_TEXT_DOCUMENTS)
        return _verdict(REASON_NO_TEXT_DOCUMENTS, skipped=skipped)

    raw_bytes = sum(int(d.get("sizeBytes") or 0) for d in readable_docs)
    if raw_bytes > MAX_RAW_BYTES:
        logger.info("kb_full_context assistant=%s ineligible reason=raw_bytes bytes=%d", assistant_id, raw_bytes)
        return _verdict(REASON_RAW_BYTES, readable=readable_docs, skipped=skipped)

    budget_chars = budget_tokens * CHARS_PER_TOKEN
    with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(readable))) as pool:
        extracts = list(pool.map(lambda pair: _document_extract(pair[0], pair[1], budget_chars), readable))

    documents: List[Tuple[str, str]] = []
    used = 0
    for doc, extract in zip(readable_docs, extracts):
        text = extract.text
        if text is None and extract.chars > budget_chars:
            # Longer than the whole budget on its own. ``text_chars`` is that one
            # document's length, so the author sees how far over it is.
            logger.info("kb_full_context assistant=%s ineligible reason=document chars=%d", assistant_id, extract.chars)
            return _verdict(REASON_DOCUMENT_OVER_BUDGET, readable=readable_docs, skipped=skipped, chars=extract.chars)
        if text is None:
            logger.info("kb_full_context assistant=%s ineligible reason=text docs=%d", assistant_id, len(docs))
            return _verdict(REASON_NO_TEXT_LAYER, readable=readable_docs, skipped=skipped, chars=used)
        used += len(text)
        if used > budget_chars:
            logger.info("kb_full_context assistant=%s ineligible reason=budget chars=%d", assistant_id, used)
            # Keep counting for the author's number; every length is already in hand.
            total = used + sum(extract.chars for extract in extracts[len(documents) + 1:])
            return _verdict(REASON_OVER_BUDGET, readable=readable_docs, skipped=skipped, chars=total)
        documents.append((_name(doc), text))

    block = KnowledgeBaseBlock(
        render_knowledge_base(documents, skipped),
        frozenset(str(d.get("documentId") or "") for d in skipped_docs),
    )
    logger.info(
        "kb_full_context assistant=%s eligible docs=%d skipped=%d chars=%d estTokens=%d ms=%d",
        assistant_id, len(documents), len(skipped), len(block), len(block) // CHARS_PER_TOKEN,
        (time.perf_counter() - started) * 1000,
    )
    return _verdict(None, readable=readable_docs, skipped=skipped, chars=used, block=block)


def _name(doc) -> str:
    return str(doc.get("filename") or doc.get("documentId"))


def _build(assistant_id: str, budget_tokens: int) -> Optional[KnowledgeBaseBlock]:
    """Blocking body of :func:`load_full_kb_context`."""
    return assess_full_context(assistant_id, budget_tokens).block


async def load_full_kb_context(
    assistant_id: str, *, access: Optional[KbAccess], budget_tokens: Optional[int] = None
) -> Optional[KnowledgeBaseBlock]:
    """The whole knowledge base as one block, or ``None`` to use retrieval.

    The block is a ``str`` (:class:`KnowledgeBaseBlock`) that also names the
    documents it skipped, so the turn can still retrieve from those.

    ``access`` is required and keyword-only, exactly as for
    ``search_assistant_knowledgebase_with_cap``: a missing or mismatched grant
    returns ``None`` before anything is read. Never raises.

    ``budget_tokens`` overrides the text budget; voice mode passes
    :func:`voice_max_tokens`.
    """
    if not assistant_kb_full_context_enabled():
        return None
    budget = full_context_max_tokens() if budget_tokens is None else budget_tokens
    if budget <= 0:
        return None
    if access is None or not access.may_read or access.assistant_id != assistant_id:
        return None
    try:
        return await asyncio.to_thread(_build, assistant_id, budget)
    except Exception:
        logger.warning("kb_full_context assistant=%s failed; using retrieval", assistant_id, exc_info=True)
        return None


def _reset_for_tests() -> None:
    reset_text_cache_for_tests()
