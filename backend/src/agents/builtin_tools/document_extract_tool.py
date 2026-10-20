"""``document_extract`` — run a named field set over one document.

``docs/specs/structured-extraction.md`` §5, §8, §9. The model names a field set
and a document; the backend resolves the set's fields server-side, makes its
own constrained model call, verifies every extracted value through
``source_provenance``, writes a reviewable artifact and an ``.xlsx``, and
returns the values and their states.

Design notes
------------
* **The agent-facing spec is a constant per deployment.** No field appears in
  ``inputSchema``: ``field_set`` is a string the model names. The typed schema
  lives on a request of its own (``extraction.extractor``), which has no
  cacheable prefix to re-write — so an office can tune a field set over six
  iterations in one session and pay nothing for it. Generating the tool's own
  schema from the field list would sit ahead of the system prompt and the whole
  restored history, and re-write 30k–150k tokens at 1.25x base input rate on
  every edit (D1).
* **The field definitions never pass through the model** (D3). A model that
  read the field table through ``read_skill_file`` and re-typed the names into
  a tool call would pay for the table in the content blocks and could mangle a
  name, which silently breaks comparability with last month's rows. ``SKILL.md``
  names the available sets; the model names one; the backend reads it.
* **Mounted on a field set, not on a document** (D9) — the one deliberate
  divergence from ``verify_sources``' gate. A session with a document and no
  field set has nothing to extract, and a tool that cannot work costs prefix
  for nothing. The document is checked at call time, where a conversational
  error is the right failure.
* **The model is told the values and the states.** The values are the answer,
  and the follow-up question ("so what was the budget cap?") is the normal
  case — a model that cannot see them must re-run a document-sized call to
  answer it. Everything a reader needs beyond that — the quote, the locator,
  ``precision``, ``method``, the rendered table — leaves on the
  ``document_extraction`` SSE event and an ``EXTR#`` row (D8).
* **Nowhere near the first token.** The tool runs only when the model calls it,
  after it has read a document and decided to extract. No
  ``BeforeInvocationEvent`` and no ``BeforeModelCallEvent`` hook.
* **Metered** (D12), unlike its side-channel neighbours ``session_title`` and
  ``tool_group_summary``: those are bounded and tiny, while this call's input
  scales with the document, so one run over a 300-page award package is a real
  cost. A ``C#`` row prices it through the catalog. If
  ``DOCUMENT_EXTRACT_MODEL_ID`` names a model with no catalog row the call
  prices to ``None``, costs $0, counts against nothing, and
  ``UnmeteredModelCall`` is emitted so the gap is visible.
* Identity is captured by closure, never read from ``invocation_state`` or from
  model input, exactly as ``document_read`` and ``verify_sources`` do.
* **Content-free record per run** in ``AgentCoreStack/Compaction``
  (``DocumentExtract``, ``DocumentExtractFields``; properties ``format``,
  ``fieldCount``, ``schemaEnforced`` and the per-state counts), beside the
  ``document_read`` and ``SourceVerify`` records. No field name, value, quote,
  filename or field-set name is ever emitted — a field-set name is an
  institution's business process.
* **Holds no run state**, so a later change can run it concurrently over
  several documents (G5) without untangling anything here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from strands import tool
from strands.types.tools import ToolContext

from apis.shared.extraction.extraction_broker import queue_extraction
from apis.shared.extraction.extractor import ExtractionError, extract_fields
from apis.shared.extraction.field_sets import FieldSetError
from apis.shared.extraction.render import (
    XLSX_MIME_TYPE,
    render_markdown,
    render_workbook,
    workbook_filename,
)
from apis.shared.extraction.resolver import (
    FieldSetNotFound,
    field_set_names,
    resolve_field_set,
)
from apis.shared.extraction.rows import (
    Row,
    build_rows,
    event_fields,
    model_facing_result,
    state_counts,
)
from apis.shared.files.document_read import DocumentReadError, document_provenance_units
from apis.shared.files.source_provenance import document_units
from apis.shared.files.workspace import (
    WorkspaceError,
    WorkspaceStorageNotConfiguredError,
)

logger = logging.getLogger(__name__)

#: The tool's name — also the census / ledger key.
DOCUMENT_EXTRACT_TOOL_NAME = "document_extract"

_NO_STORAGE_MESSAGE = (
    "❌ Document storage is not configured (S3_USER_FILES_BUCKET_NAME is not "
    "set on the runtime)."
)


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _parse_pages(pages: Any, unit_count: int) -> Optional[List[int]]:
    """A 1-indexed page selection, or ``None`` for the whole document.

    Accepts a list of numbers or a string like ``"1-3,7"``. Out-of-range
    entries are dropped rather than raised on: a model guessing "pages 1-5" at
    a four-page document means "the front of it", and failing the call over the
    fifth would be pedantry at the user's expense.
    """
    if pages in (None, "", [], {}):
        return None
    wanted: List[int] = []
    try:
        if isinstance(pages, str):
            for part in pages.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start, _, end = part.partition("-")
                    wanted.extend(range(int(start), int(end) + 1))
                else:
                    wanted.append(int(part))
        elif isinstance(pages, (list, tuple)):
            wanted = [int(page) for page in pages]
        else:
            wanted = [int(pages)]
    except (TypeError, ValueError):
        return None
    kept = sorted({page for page in wanted if 1 <= page <= unit_count})
    return kept or None


def record_document_extract(
    fmt: Optional[str], rows: List[Row], *, schema_enforced: bool
) -> None:
    """One content-free EMF record per run. Never raises.

    The per-state split is the measurement §14 asks for: if every row of every
    run is ``supported``, the verification is not working — it is the table
    that was always produced, with badges on it.
    """
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import (
            prompt_cache_observability_enabled,
        )

        if not prompt_cache_observability_enabled():
            return
        counts = state_counts(rows)
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "DocumentExtract": 1,
                "DocumentExtractFields": len(rows),
            },
            properties={
                "format": fmt or "unknown",
                "fieldCount": len(rows),
                "schemaEnforced": schema_enforced,
                **counts,
            },
            units={"DocumentExtractFields": "Count"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("document_extract EMF skipped: %s", e)


async def _meter(
    *,
    user_id: str,
    session_id: str,
    tool_use_id: str,
    model_id: str,
    usage: Dict[str, int],
) -> None:
    """Write the extraction call's ``C#`` cost row (D12). Never raises.

    Unlike ``session_title`` and ``tool_group_summary``, this call's input
    scales with the document, so the "negligible side channel" finding does not
    transfer and it is billed like any other call. ``message_id`` is a string
    tag rather than a message index — the row's SK is ``C#<timestamp>#<uuid>``
    and ``message_id`` is only an attribute, which is the precedent the voice
    path already set with ``voice:<index>``.
    """
    if not usage or not (usage.get("inputTokens") or usage.get("outputTokens")):
        # A call that ran and reported no usage is an unmetered call, so it is
        # named as one rather than passed over in silence. Without this the gap
        # is invisible twice: no `C#` row to find and no metric to notice.
        try:
            from apis.shared.observability.emf import emit_unmetered_model_call

            logger.warning(
                "Unmetered document_extract call: model=%s reason=no_usage — the "
                "extraction ran but reported no token usage, so it is not "
                "counted against quota",
                model_id,
            )
            emit_unmetered_model_call(
                model_id,
                "no_usage",
                surface="document_extract",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("document_extract: unmetered metric skipped", exc_info=True)
        return
    try:
        from datetime import datetime, timezone

        from apis.shared.costs.calculator import CostCalculator
        from apis.shared.costs.pricing_config import get_model_pricing
        from apis.shared.sessions.metadata import store_message_metadata
        from apis.shared.sessions.models import (
            Attribution,
            MessageMetadata,
            ModelInfo,
            TokenUsage,
        )

        pricing = None
        try:
            pricing = await get_model_pricing(model_id)
        except Exception:  # noqa: BLE001
            logger.debug("document_extract: pricing lookup failed", exc_info=True)

        cost = None
        if pricing:
            try:
                total, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
                cost = {
                    "total": total,
                    "inputCost": breakdown.input_cost,
                    "outputCost": breakdown.output_cost,
                    "cacheReadCost": breakdown.cache_read_cost,
                    "cacheWriteCost": breakdown.cache_write_cost,
                }
            except Exception:  # noqa: BLE001
                logger.debug("document_extract: cost calc failed", exc_info=True)

        if cost is None:
            # The trap §9 names, made visible rather than silent: an extraction
            # model with no catalog row runs for free against quota.
            from apis.shared.observability.emf import emit_unmetered_model_call

            logger.warning(
                "Unmetered document_extract call: model=%s — usage recorded "
                "with no cost; not counted against quota",
                model_id,
            )
            emit_unmetered_model_call(
                model_id,
                "calculation_failed" if pricing else "no_pricing",
                surface="document_extract",
                session_id=session_id,
            )

        await store_message_metadata(
            session_id=session_id,
            user_id=user_id,
            message_id=f"extract:{tool_use_id}",
            message_metadata=MessageMetadata(
                token_usage=TokenUsage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                    total_tokens=usage.get("inputTokens", 0)
                    + usage.get("outputTokens", 0),
                    cache_write_input_tokens=usage.get("cacheWriteInputTokens") or None,
                    cache_read_input_tokens=usage.get("cacheReadInputTokens") or None,
                ),
                model_info=ModelInfo(
                    model_id=model_id,
                    model_name=model_id,
                    provider="bedrock",
                ),
                attribution=Attribution(
                    user_id=user_id,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ),
                cost=cost,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - metering must not fail a good extraction
        logger.warning("document_extract: failed to meter the call", exc_info=True)


async def _write_artifact(
    *, user_id: str, session_id: str, title: str, content: str
) -> Optional[str]:
    """The backend-authored markdown artifact (§8.2, D13). ``None`` on failure.

    ``create_artifact_record`` is a plain service function, so the row data
    never passes through the model to produce the table — which is the whole
    reason this is not the ``create_artifact`` tool.
    """
    try:
        from agents.builtin_tools.artifacts.service import create_artifact_record

        artifact_id, _ = await asyncio.to_thread(
            create_artifact_record,
            user_id,
            session_id,
            title,
            content,
            "text/markdown",
        )
        return artifact_id
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a missing artifact beats a lost run
        logger.warning("document_extract: failed to write artifact", exc_info=True)
        return None


async def _write_workbook(
    *, user_id: str, session_id: str, filename: str, data: bytes
) -> Optional[Tuple[str, str]]:
    """The in-process ``.xlsx`` (§8.3, D13). ``(upload_id, size_kb)`` or ``None``."""
    try:
        from agents.builtin_tools.office._storage import _store_document

        return await _store_document(
            user_id, session_id, filename, data, XLSX_MIME_TYPE
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a missing workbook beats a lost run
        logger.warning("document_extract: failed to store workbook", exc_info=True)
        return None


async def _persist(
    user_id: str, session_id: str, tool_use_id: str, payload: Dict[str, Any]
) -> None:
    """Write the ``EXTR#`` row off the event loop. Never raises."""
    try:
        from apis.shared.extraction.extraction_store import get_extraction_store

        await asyncio.to_thread(
            get_extraction_store().store,
            user_id=user_id,
            session_id=session_id,
            tool_use_id=tool_use_id,
            payload=payload,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - persistence is best-effort
        logger.warning("document_extract: failed to persist run", exc_info=True)


def make_document_extract_tool(session_id: str, user_id: str, records: List[Any]):
    """Create a ``document_extract`` tool bound to this turn's identity and skills.

    ``records`` is exactly the turn's effective skill set, so access to a field
    set is structural: a set the invoker cannot reach is simply absent, and
    there is no name the model can say to get at one.
    """
    if not session_id or not user_id:
        raise ValueError("document_extract requires a session_id and a user_id")

    available = field_set_names(records)

    @tool(name=DOCUMENT_EXTRACT_TOOL_NAME, context=True)
    async def document_extract(
        upload_id: str,
        field_set: str,
        tool_context: ToolContext,
        pages: Optional[str] = None,
    ) -> Any:
        """Pull a named set of fields out of one attached document, as a table.

        Use this when the user wants the same specific facts off a document —
        deadlines, caps, rates, eligibility, limits — and a field set exists for
        that kind of document. Each value comes back with a status saying
        whether it was actually found in the passage cited for it, and the user
        is shown a table and a spreadsheet automatically.

        Do not read the field-set file yourself and do not re-type the field
        names: name the set and the backend reads its definition.

        Each field comes back with one status:
        - supported: the value is in the passage cited for it. Safe to repeat.
        - quote_unsupported: the cited passage is in the document but the value
          is NOT in it. Treat this as a mistake — tell the user that field needs
          checking, and do not restate the value as fact.
        - unassessed: a passage was found but the value is not the kind of thing
          that can be matched against it (a yes/no, or a choice from a list).
          Report it without claiming it was confirmed.
        - unverified: no passage was found, or the document has no text to
          search. Do not present the value as sourced.

        A null value means the document does not state that field, which is a
        useful answer — say so plainly rather than guessing.

        Args:
            upload_id: The document's id, from the attachment note, the document
                listing, or an earlier document_read call.
            field_set: The name of the field set to run, as named in the skill's
                instructions.
            pages: Optional. Narrow a long document, e.g. "1-4" or "1,2,9".
                Use it when the fields are all in one section of a large
                package — it is the main lever on what a run costs.

        Returns:
            Each field's name, value and status. The table, the cited passages
            and the spreadsheet are already shown to the user.
        """
        from apis.shared.feature_flags import document_extract_model_id

        if not (field_set or "").strip():
            listing = ", ".join(available) or "(none)"
            return _error(
                f"❌ field_set is required. Available field sets: {listing}"
            )

        # Resolve the set before touching the document: a mistyped set name is
        # the model's mistake and costs nothing to correct, while reading a
        # 300-page package first would spend an S3 GET to find that out.
        try:
            resolved = await asyncio.to_thread(resolve_field_set, records, field_set)
        except FieldSetNotFound as exc:
            return _error(f"❌ {exc}")
        except FieldSetError as exc:
            # The author's mistake, not the model's. Say which, so the message
            # reaches a user who can act on it.
            return _error(
                f"❌ The field set '{field_set}' could not be read: {exc} "
                "This is a problem with how the field set is written, not with "
                "your request — tell the user it needs fixing by whoever "
                "maintains the skill."
            )

        try:
            meta, fmt, texts = await document_provenance_units(
                user_id, session_id, upload_id
            )
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except (WorkspaceError, DocumentReadError) as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - conversational, never raised through
            logger.error("document_extract: document read failed: %s", exc, exc_info=True)
            return _error(f"❌ Failed to read document '{upload_id}': {exc}")

        selected = _parse_pages(pages, len(texts))
        extract_texts = (
            [texts[page - 1] for page in selected] if selected else list(texts)
        )

        # Provenance is always verified against the WHOLE document, never the
        # narrowed selection. A quote the model took from page 2 of a run
        # narrowed to pages 3-5 is still a real passage of this document, and
        # searching only the selection would report it `unverified` — a
        # *manufactured* hallucination signal, which is the one error this must
        # never produce.
        units = document_units(fmt, list(texts)) if any(texts) else None

        model_id = document_extract_model_id()
        try:
            result = await extract_fields(
                resolved,
                "\n\n".join(extract_texts),
                filename=meta.filename,
                model_id=model_id,
            )
        except ExtractionError as exc:
            return _error(f"❌ Extraction did not run: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("document_extract: extraction failed: %s", exc, exc_info=True)
            return _error(f"❌ Extraction failed: {exc}")

        rows = build_rows(resolved, result.rows, units)

        content = render_markdown(
            rows,
            field_set=resolved,
            filename=meta.filename,
            model_id=result.model_id,
            schema_enforced=result.schema_enforced,
        )
        artifact_id = await _write_artifact(
            user_id=user_id,
            session_id=session_id,
            title=f"{resolved.title} — {meta.filename}",
            content=content,
        )

        download_upload_id: Optional[str] = None
        size_kb = ""
        workbook = render_workbook(
            rows,
            field_set=resolved,
            filename=meta.filename,
            model_id=result.model_id,
            schema_enforced=result.schema_enforced,
        )
        if workbook:
            stored = await _write_workbook(
                user_id=user_id,
                session_id=session_id,
                filename=workbook_filename(resolved, meta.filename),
                data=workbook,
            )
            if stored:
                download_upload_id, size_kb = stored

        tool_use_id = (tool_context.tool_use or {}).get("toolUseId", "")
        payload: Dict[str, Any] = {
            "toolUseId": tool_use_id,
            "fieldSet": resolved.name,
            "fieldSetVersion": resolved.version,
            "uploadId": meta.upload_id,
            "filename": meta.filename,
            "modelId": result.model_id,
            "schemaEnforced": result.schema_enforced,
            "artifactId": artifact_id,
            "downloadUploadId": download_upload_id,
            "fields": event_fields(rows),
        }

        # Persisted BEFORE the broker hand-off, for the reason verify_sources
        # records: the emit loop may never reach this payload on a cancelled
        # turn or a dropped connection, and the row is what survives a reload.
        await _persist(user_id, session_id, tool_use_id, payload)
        queue_extraction(user_id, session_id, payload)

        record_document_extract(fmt, rows, schema_enforced=result.schema_enforced)
        await _meter(
            user_id=user_id,
            session_id=session_id,
            tool_use_id=tool_use_id,
            model_id=result.model_id,
            usage=result.usage,
        )

        summary = model_facing_result(
            rows,
            field_set=resolved,
            artifact_id=artifact_id,
            schema_enforced=result.schema_enforced,
        )
        # The `_download_card` precedent: tell the model the table and the card
        # are already on screen, or it helpfully re-types both.
        note = (
            f"A table of these {len(rows)} fields is already displayed to the "
            "user, with the passage and page each value came from"
        )
        if download_upload_id:
            note += f", and a spreadsheet ({size_kb}) is in this chat's Files"
        note += (
            ". Do not re-type the table or write a download link. Report what "
            "the user asked about, and name any field whose status is not "
            "`supported`."
        )
        summary["note"] = note
        return {"content": [{"json": summary}], "status": "success"}

    return document_extract


__all__ = [
    "DOCUMENT_EXTRACT_TOOL_NAME",
    "make_document_extract_tool",
    "record_document_extract",
]
