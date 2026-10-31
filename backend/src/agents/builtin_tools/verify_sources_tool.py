"""``verify_sources`` — check a cited passage against the document it names.

``docs/specs/source-provenance-verification.md`` §8. The model names the value
it is about to assert and the passage it says supports that value; the backend
answers two separate questions — is the passage in the document, and does the
value actually appear in it — and collapses them into one of four states.

Design notes
------------
* **The claim is explicit, because it has to be.** Deciding which number in a
  sentence is "the asserted value for this quote" is not solvable
  deterministically, and guesswork dressed as verification is the failure mode
  this whole feature exists to kill. Only the model knows which value it is
  asserting, so it says.
* **Gated on session state, not on the tool picker.** Built inside the same
  ``_build_document_tools`` gate as ``document_read``: the governing capability
  is the user's own attachment, so the id stays out of ``INJECTED_TOOL_IDS``
  and there is no RBAC catalog entry. The gate's answer is already in the agent
  cache key (``has_document_tools``) and the flag is a deployment constant, so
  this adds **no new cache key element**.
* **The model is told the state and nothing else.** One short word per claim.
  A tool's result *is* the Converse payload and *is* in the cacheable prefix,
  so everything a reader needs beyond the state — quote, locator, precision,
  method — leaves on the ``source_provenance`` SSE event and a ``SRCV#`` row
  instead. The state is the deliberate exception: a model that cannot see
  ``quote_unsupported`` cannot correct it, and correcting it is the most
  valuable thing that can happen in the turn.
* **Nowhere near the first token.** The tool runs only when the model calls it,
  after it has read a document and formed an answer. No
  ``BeforeInvocationEvent`` and no ``BeforeModelCallEvent`` hook — the latter
  fires before *every* model call, so its cost would repeat per tool round.
* Identity is captured by closure (``make_verify_sources_tool``), never read
  from ``invocation_state`` or from model input, exactly as ``document_read``
  does. ``upload_id`` is the only document selector and it is resolved against
  the caller's own rows.
* **The document is named by what the model can see, or not at all.** A small
  attachment reaches the model as an inline ``document`` block named by its
  filename, with no upload id anywhere in the conversation, so a required id
  sent Haiku 4.5 to guess one (``"0"``, five samples of five on dev) and pay
  two extra tool rounds recovering. ``upload_id`` is therefore optional and
  falls back to the session's own documents: the only one, or the one whose
  filename it names. An ambiguous or unmatched name lists the session's
  documents so the next call can pick. The listing query runs only on that
  fallback; a real upload id resolves exactly as before.
* **Content-free record per call** in ``AgentCoreStack/Compaction``
  (``SourceVerify``, ``SourceVerifyClaims``; properties ``format`` and the
  per-state counts), beside the ``document_read`` records. No quote, value or
  filename is ever emitted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from strands import tool
from strands.types.tools import ToolContext

from apis.shared.files.document_read import (
    DocumentReadError,
    document_provenance_units,
    list_session_documents,
)
from apis.shared.files.provenance_broker import queue_provenance
from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
    Claim,
    document_units,
    verify_claims,
)
from apis.shared.files.workspace import (
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceStorageNotConfiguredError,
)

logger = logging.getLogger(__name__)

#: The tool's name — also the census / ledger key.
VERIFY_SOURCES_TOOL_NAME = "verify_sources"

#: Claims one call may carry. A turn asserting more than this many values from
#: one document should verify them in batches, which costs one extra tool round
#: and keeps both the stored row and the tool result bounded.
MAX_CLAIMS_PER_CALL = 12

#: Documents named in the "which document?" error. A session past this many is
#: told to call ``document_read`` for the full listing instead.
MAX_LISTED_DOCUMENTS = 20

#: Refused outright rather than scanned. A passage longer than this is not a
#: passage, and the quote is model-controlled input.
MAX_QUOTE_CHARS = 1200

_NO_STORAGE_MESSAGE = (
    "❌ Document storage is not configured (S3_USER_FILES_BUCKET_NAME is not set on the runtime)."
)


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def record_verify_sources(fmt: Optional[str], states: List[str]) -> None:
    """One content-free EMF record per call. Never raises.

    The per-state counts are the measurement the spec asks for: the split
    between ``supported`` and ``quote_unsupported`` is the honesty readout, and
    the ``unassessed`` share says whether the value check is answering the
    question often enough to be worth a badge.
    """
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "SourceVerify": 1,
                "SourceVerifyClaims": len(states),
            },
            properties={
                "format": fmt or "unknown",
                "supported": states.count(STATE_SUPPORTED),
                "quote_unsupported": states.count(STATE_QUOTE_UNSUPPORTED),
                "unassessed": states.count(STATE_UNASSESSED),
                "unverified": states.count(STATE_UNVERIFIED),
            },
            units={"SourceVerifyClaims": "Count"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("verify_sources EMF skipped: %s", e)


def _parse_claims(claims: Any) -> List[Claim]:
    """Model input to :class:`Claim` objects, or raise with a usable message."""
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a non-empty list of {id, value, quote} objects.")
    if len(claims) > MAX_CLAIMS_PER_CALL:
        raise ValueError(
            f"At most {MAX_CLAIMS_PER_CALL} claims per call; send the rest in another call."
        )
    parsed: List[Claim] = []
    for index, raw in enumerate(claims):
        if not isinstance(raw, dict):
            raise ValueError(f"claims[{index}] must be an object with id, value and quote.")
        quote = raw.get("quote")
        if isinstance(quote, str) and len(quote) > MAX_QUOTE_CHARS:
            raise ValueError(
                f"claims[{index}].quote is {len(quote)} characters; quote the sentence "
                f"that carries the value, at most {MAX_QUOTE_CHARS} characters."
            )
        parsed.append(
            Claim(
                claim_id=str(raw.get("id") or f"claim-{index + 1}"),
                value=raw.get("value"),
                quote=quote if isinstance(quote, str) else None,
                enum_field=bool(raw.get("from_fixed_set")),
            )
        )
    return parsed


def _name_key(name: str) -> str:
    """A filename reduced to what survives Bedrock's document-name sanitizer.

    The inline block for ``facilities_policy.md`` is named
    ``facilities_policy_md``, the ``[Attached files: …]`` marker says
    ``facilities_policy.md``, and a model may drop the extension or the case.
    All of them reduce to the same key.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _filename_keys(filename: str) -> set:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return {_name_key(filename), _name_key(stem)} - {""}


async def _resolve_session_document(user_id: str, session_id: str, requested: str) -> str:
    """The upload id of the session document ``requested`` names, or raise.

    ``requested`` is whatever the model sent that is not an upload id: a
    filename, a guess, or nothing. A unique filename match wins; otherwise a
    session with exactly one document means that document, because there is
    nothing else the model can have meant. Anything still ambiguous raises with
    the session's documents listed, so the next call can name one.
    """
    listing = await list_session_documents(user_id, session_id)
    documents = listing.get("documents") or []

    key = _name_key(requested)
    if key:
        named = [d for d in documents if key in _filename_keys(d["filename"])]
        if len(named) == 1:
            return named[0]["upload_id"]
    if len(documents) == 1:
        return documents[0]["upload_id"]

    if not documents:
        raise DocumentReadError(
            "This conversation has no readable document to verify a quote against."
        )
    shown = documents[:MAX_LISTED_DOCUMENTS]
    choices = ", ".join(f"'{d['upload_id']}' ({d['filename']})" for d in shown)
    more = (
        f" and {len(documents) - len(shown)} more (call document_read with no arguments for all)"
        if len(documents) > len(shown)
        else ""
    )
    lead = f"No document matches '{requested}'." if requested else "Say which document."
    raise DocumentReadError(
        f"{lead} Pass one of these as upload_id: {choices}{more}."
    )


async def _provenance_units(user_id: str, session_id: str, requested: str):
    """``document_provenance_units`` for the document the model named.

    A real upload id costs exactly what it did before. Only a missing or
    unknown one pays the session listing query.
    """
    requested = (requested or "").strip()
    if requested:
        try:
            return await document_provenance_units(user_id, session_id, requested)
        except WorkspaceFileNotFoundError:
            pass
    upload_id = await _resolve_session_document(user_id, session_id, requested)
    return await document_provenance_units(user_id, session_id, upload_id)


async def _persist(
    user_id: str, session_id: str, tool_use_id: str, verdicts: List[Dict[str, Any]]
) -> None:
    """Write the ``SRCV#`` row off the event loop. Never raises.

    A failed write costs reload survival only, and failing the turn over it
    would turn a degraded citation into a lost answer.
    """
    try:
        from apis.shared.files.provenance_store import get_source_provenance_store

        await asyncio.to_thread(
            get_source_provenance_store().store,
            user_id=user_id,
            session_id=session_id,
            tool_use_id=tool_use_id,
            claims=verdicts,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - persistence is best-effort
        logger.warning("verify_sources: failed to persist verdicts", exc_info=True)


def make_verify_sources_tool(session_id: str, user_id: str):
    """Create a ``verify_sources`` tool bound to the given identity."""
    if not session_id or not user_id:
        raise ValueError("verify_sources requires a session_id and a user_id")

    @tool(name=VERIFY_SOURCES_TOOL_NAME, context=True)
    async def verify_sources(
        claims: List[Dict[str, Any]],
        tool_context: ToolContext,
        upload_id: str = "",
    ) -> Any:
        """Check that each value you are asserting is really in the document.

        Call this before stating values you took from an attached document —
        amounts, dates, deadlines, names, percentages — one call per document.
        For each value, give the shortest verbatim passage from the document
        that carries it. Copy the passage exactly as the document has it; do not
        tidy it, join lines, or add quotation marks of your own.

        Each claim comes back with one state:
        - supported: the passage is in the document and the value is in the
          passage. Safe to cite with a page reference.
        - quote_unsupported: the passage is in the document but the value is
          NOT in it. Treat this as a mistake on your part — re-read the
          document and correct the value, or say you could not confirm it. Do
          not restate it as fact.
        - unassessed: the passage is in the document, but the value is not
          something that can be matched against it. Cite the passage, and do
          not claim the value is confirmed.
        - unverified: the passage is not in the document. Either you did not
          quote it exactly or it is not there. Re-read before asserting the
          value, and never present it as sourced.

        The user is shown the page or paragraph each passage was found on, so
        you do not need to repeat the location in your answer.

        Args:
            upload_id: Which document. Leave it out when the conversation has
                one document. Otherwise give the document's filename, or its
                upload_id from document_read's listing. Never guess an id.
            claims: One object per value, each with:
                id — a short label for the value, e.g. "total_award";
                value — the value exactly as you will state it to the user;
                quote — the verbatim passage from the document that carries
                it, one or two sentences;
                from_fixed_set — true only when the value is one of a fixed
                set you chose by reading the prose (for example "Yes" for
                "cost sharing is required") rather than a span of the text.

        Returns:
            The state for each claim id.
        """
        try:
            parsed = _parse_claims(claims)
        except ValueError as exc:
            return _error(f"❌ {exc}")

        try:
            meta, fmt, texts = await _provenance_units(user_id, session_id, upload_id)
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except WorkspaceError as exc:
            return _error(f"❌ {exc}")
        except DocumentReadError as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - conversational, never raised through the loop
            logger.error("verify_sources error: %s", exc, exc_info=True)
            return _error(f"❌ Failed to read document '{upload_id}' for verification: {exc}")

        # A document with no text layer (a scanned PDF) yields no units. Pass
        # None so every claim reads `unverified` with `document_unreadable`,
        # rather than inventing a document to search.
        units = document_units(fmt, texts) if any(texts) else None
        results = verify_claims(parsed, units)

        tool_use_id = (tool_context.tool_use or {}).get("toolUseId", "")
        verdicts = [
            {
                "id": result.claim_id,
                "state": result.state,
                "method": result.method,
                # The value as the model stated it. Display metadata, like the
                # quote beside it: a `quote_unsupported` that cannot name the
                # value it is warning about makes the reader guess which number
                # in the answer is the unconfirmed one. It costs nothing in the
                # prefix — the model already wrote it into this tool's
                # arguments, which are the content blocks.
                "value": str(claim.value) if claim.value is not None else "",
                "quote": claim.quote,
                "locator": result.locator.to_payload() if result.locator else None,
                "uploadId": meta.upload_id,
                "filename": meta.filename,
            }
            for claim, result in zip(parsed, results)
        ]

        # Persisted here, before the broker, for the same reason the tool-batch
        # summarizer persists before it returns: the emit loop may never reach
        # this payload (a cancelled turn, a dropped connection), and the row is
        # what makes the verdict survive a reload either way.
        await _persist(user_id, session_id, tool_use_id, verdicts)
        queue_provenance(
            user_id,
            session_id,
            {"toolUseId": tool_use_id, "claims": verdicts},
        )

        record_verify_sources(fmt, [result.state for result in results])

        # The state, and nothing else. Everything richer is display metadata
        # and would be paid at model rates on every later turn if it went into
        # the content blocks.
        return {
            "content": [
                {"json": {"claims": [{"id": r.claim_id, "state": r.state} for r in results]}}
            ],
            "status": "success",
        }

    return verify_sources


__all__ = [
    "MAX_CLAIMS_PER_CALL",
    "MAX_LISTED_DOCUMENTS",
    "MAX_QUOTE_CHARS",
    "VERIFY_SOURCES_TOOL_NAME",
    "make_verify_sources_tool",
    "record_verify_sources",
]
