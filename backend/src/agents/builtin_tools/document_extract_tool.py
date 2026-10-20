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

from apis.shared.extraction.batch import (
    MAX_DOCUMENTS,
    RELIABLE_DOCUMENT_COUNT,
    DocumentOutcome,
    extract_over_documents,
)
from apis.shared.extraction.extraction_broker import queue_extraction
from apis.shared.extraction.extractor import (
    ExtractionError,
    SchemaTooComplexError,
    extract_fields,
)
from apis.shared.extraction.field_sets import FieldSetError
from apis.shared.extraction.render import (
    XLSX_MIME_TYPE,
    batch_workbook_filename,
    render_batch_markdown,
    render_batch_workbook,
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

def _normalize_upload_ids(upload_ids: Any) -> List[str]:
    """Model input to a de-duplicated list of ids, in the order given.

    Tolerant on shape because an unenforced tool argument guarantees nothing
    (§6.2): a model that sends a bare string, or a comma-separated one, meant
    one document and should get it rather than an error about JSON. Duplicates
    are dropped — extracting the same document twice in one batch would double
    its cost and put it in the comparison twice.
    """
    if isinstance(upload_ids, str):
        candidates = [part.strip() for part in upload_ids.split(",")]
    elif isinstance(upload_ids, (list, tuple)):
        candidates = []
        for item in upload_ids:
            if isinstance(item, str):
                candidates.extend(part.strip() for part in item.split(","))
            elif item is not None:
                candidates.append(str(item).strip())
    elif upload_ids is None:
        return []
    else:
        candidates = [str(upload_ids).strip()]

    seen: Dict[str, None] = {}
    for candidate in candidates:
        if candidate:
            seen.setdefault(candidate, None)
    return list(seen)


def _outcome_note(outcomes: List[Any]) -> str:
    """One line per failed document, for the model to pass on.

    A set where three documents failed silently is a judgement made on eleven
    documents while reading as though it were made on fourteen. So the failures
    are named, with their reasons, and the model is told the count it actually
    has.
    """
    failed = [o for o in outcomes if not o.ok]
    if not failed:
        return ""
    lines = "; ".join(f"{o.filename or o.upload_id}: {o.error}" for o in failed[:10])
    more = f" (and {len(failed) - 10} more)" if len(failed) > 10 else ""
    return (
        f"{len(failed)} of {len(outcomes)} documents could not be extracted — "
        f"{lines}{more}. Say so; do not present the comparison as covering them."
    )


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
        upload_ids: List[str],
        field_set: str,
        tool_context: ToolContext,
        pages: Optional[str] = None,
    ) -> Any:
        """Pull a named set of fields out of one or more attached documents.

        Use this when the user wants the same specific facts off a document or
        a stack of them — deadlines, caps, rates, eligibility, limits — and a
        field set exists for that kind of document. Pass **every** document the
        user is asking about in one call: that is what lets you compare them,
        spot conflicts between them, and say which are worth pursuing. Calling
        this once per document instead throws that away.

        Each value comes back with a status saying whether it was actually found
        in the passage cited for it, and the user is shown a table and a
        spreadsheet automatically.

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
            upload_ids: The documents' ids, from the attachment note, the
                document listing, or earlier document_read calls. Pass them all
                at once.
            field_set: The name of the field set to run, as named in the skill's
                instructions.
            pages: Optional. Narrow every document to these pages, e.g. "1-4".
                Use it when the fields are all in one section of each package —
                it is the main lever on what a run costs. Provenance is still
                checked against the whole document.

        Returns:
            One row set per document, each field with its value and status. The
            tables, the cited passages and the spreadsheet are already shown to
            the user.
        """
        from apis.shared.feature_flags import document_extract_model_id

        if not (field_set or "").strip():
            listing = ", ".join(available) or "(none)"
            return _error(f"❌ field_set is required. Available field sets: {listing}")

        ids = _normalize_upload_ids(upload_ids)
        if not ids:
            return _error(
                "❌ upload_ids is required: pass the id of each document to "
                "extract from, as a list."
            )
        if len(ids) > MAX_DOCUMENTS:
            return _error(
                f"❌ {len(ids)} documents in one call exceeds the limit of "
                f"{MAX_DOCUMENTS}. Run them in batches — and tell the user the "
                "comparison covers only the batch you ran."
            )

        # Resolve the set before touching any document: a mistyped set name is
        # the model's mistake and costs nothing to correct, while reading fifty
        # packages first would spend fifty S3 GETs to find that out.
        try:
            resolved = await asyncio.to_thread(resolve_field_set, records, field_set)
        except FieldSetNotFound as exc:
            return _error(f"❌ {exc}")
        except FieldSetError as exc:
            return _error(
                f"❌ The field set '{field_set}' could not be read: {exc} "
                "This is a problem with how the field set is written, not with "
                "your request — tell the user it needs fixing by whoever "
                "maintains the skill."
            )

        model_id = document_extract_model_id()

        async def extract_one(upload_id: str, _label: str) -> DocumentOutcome:
            """Read, extract and verify one document. Never raises."""
            try:
                meta, fmt, texts = await document_provenance_units(
                    user_id, session_id, upload_id
                )
            except WorkspaceStorageNotConfiguredError:
                return DocumentOutcome(upload_id, upload_id, error=_NO_STORAGE_MESSAGE)
            except (WorkspaceError, DocumentReadError) as exc:
                return DocumentOutcome(upload_id, upload_id, error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "document_extract: read failed for %s: %s", upload_id, exc,
                    exc_info=True,
                )
                return DocumentOutcome(
                    upload_id, upload_id, error=f"could not be read: {exc}"
                )

            selected = _parse_pages(pages, len(texts))
            extract_texts = (
                [texts[page - 1] for page in selected] if selected else list(texts)
            )
            try:
                result = await extract_fields(
                    resolved,
                    "\n\n".join(extract_texts),
                    filename=meta.filename,
                    model_id=model_id,
                )
            except SchemaTooComplexError as exc:
                # A property of the field set, identical for every document in
                # the batch, so the batch stops rather than repeating it N times.
                return DocumentOutcome(
                    upload_id, meta.filename, error=str(exc), fatal_for_batch=True
                )
            except ExtractionError as exc:
                return DocumentOutcome(upload_id, meta.filename, error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "document_extract: extraction failed for %s: %s", upload_id, exc,
                    exc_info=True,
                )
                return DocumentOutcome(
                    upload_id, meta.filename, error=f"extraction failed: {exc}"
                )

            return DocumentOutcome(
                upload_id=upload_id,
                filename=meta.filename,
                result=result,
                fmt=fmt,
                texts=tuple(texts),
            )

        outcomes = await extract_over_documents(
            resolved, [(uid, uid) for uid in ids], extract_one
        )

        succeeded = [o for o in outcomes if o.ok]
        if not succeeded:
            note = _outcome_note(outcomes)
            return _error(f"❌ No document could be extracted. {note}")

        # Provenance is verified against the WHOLE document, never the narrowed
        # selection. A quote the model took from page 2 of a run narrowed to
        # pages 3-5 is still a real passage of this document, and searching only
        # the selection would report it `unverified` — a *manufactured*
        # hallucination signal, which is the one error this must never produce.
        per_document: List[Tuple[DocumentOutcome, List[Row]]] = []
        for outcome in succeeded:
            units = (
                document_units(outcome.fmt, list(outcome.texts))
                if any(outcome.texts)
                else None
            )
            per_document.append(
                (outcome, build_rows(resolved, outcome.result.rows, units))
            )

        enforced = all(o.result.schema_enforced for o in succeeded)
        tool_use_id = (tool_context.tool_use or {}).get("toolUseId", "")

        # ONE artifact and ONE workbook for the batch, with a document column —
        # not N of each. The deliverable is the comparison: "two collide on the
        # same March deadline" is a fact about the set, and it is only visible
        # in a table that holds the set.
        content = render_batch_markdown(
            per_document,
            field_set=resolved,
            model_id=model_id,
            schema_enforced=enforced,
            failures=[(o.filename or o.upload_id, o.error or "") for o in outcomes if not o.ok],
        )
        title = (
            f"{resolved.title} — {succeeded[0].filename}"
            if len(succeeded) == 1
            else f"{resolved.title} — {len(succeeded)} documents"
        )
        artifact_id = await _write_artifact(
            user_id=user_id, session_id=session_id, title=title, content=content
        )

        download_upload_id: Optional[str] = None
        size_kb = ""
        workbook = render_batch_workbook(
            per_document,
            field_set=resolved,
            model_id=model_id,
            schema_enforced=enforced,
        )
        if workbook:
            stored = await _write_workbook(
                user_id=user_id,
                session_id=session_id,
                filename=batch_workbook_filename(resolved, len(succeeded)),
                data=workbook,
            )
            if stored:
                download_upload_id, size_kb = stored

        # One row and one SSE frame PER DOCUMENT, all sharing this call's
        # toolUseId. The store's key carries the upload id too, or N documents
        # in one call would overwrite each other's row.
        total_usage: Dict[str, int] = {}
        for outcome, rows in per_document:
            payload: Dict[str, Any] = {
                "toolUseId": tool_use_id,
                "fieldSet": resolved.name,
                "fieldSetVersion": resolved.version,
                "uploadId": outcome.upload_id,
                "filename": outcome.filename,
                "modelId": outcome.result.model_id,
                "schemaEnforced": outcome.result.schema_enforced,
                "artifactId": artifact_id,
                "downloadUploadId": download_upload_id,
                "documentCount": len(succeeded),
                "fields": event_fields(rows),
            }
            await _persist(user_id, session_id, tool_use_id, payload)
            queue_extraction(user_id, session_id, payload)
            record_document_extract(
                outcome.fmt, rows, schema_enforced=outcome.result.schema_enforced
            )
            for key, value in (outcome.result.usage or {}).items():
                total_usage[key] = total_usage.get(key, 0) + value

        # One metering row for the batch: the per-document calls are one
        # logical unit of work and their token counts simply add.
        await _meter(
            user_id=user_id,
            session_id=session_id,
            tool_use_id=tool_use_id,
            model_id=model_id,
            usage=total_usage,
        )

        documents = [
            {
                "uploadId": outcome.upload_id,
                "filename": outcome.filename,
                **model_facing_result(
                    rows,
                    field_set=resolved,
                    artifact_id=None,
                    schema_enforced=outcome.result.schema_enforced,
                ),
            }
            for outcome, rows in per_document
        ]
        summary: Dict[str, Any] = {
            "fieldSet": resolved.name,
            "documentCount": len(succeeded),
            "documents": documents,
        }
        if artifact_id:
            summary["artifactId"] = artifact_id
        if not enforced:
            summary["schemaEnforced"] = False

        note = (
            f"A table of these {len(resolved.fields)} fields across "
            f"{len(succeeded)} document(s) is already displayed to the user, "
            "with the passage and page each value came from"
        )
        if download_upload_id:
            note += f", and one spreadsheet covering all of them ({size_kb}) is in this chat's Files"
        note += (
            ". Do not re-type the tables or write a download link. Compare the "
            "documents for the user: name any field whose status is not "
            "`supported`, and say which documents disagree or collide."
        )
        if len(succeeded) > RELIABLE_DOCUMENT_COUNT:
            # Straight out of the §17.7 measurement: the reduce stays reliable
            # to about ten documents, and every miss above that is a
            # cross-document fact going unnoticed. Working the comparison
            # field-by-field rather than document-by-document is the cheapest
            # thing that addresses that specific failure.
            note += (
                f" This is {len(succeeded)} documents, which is more than can be "
                "held in mind at once: go through the comparison one FIELD at a "
                "time across all of them, rather than one document at a time, or "
                "you will miss the deadline and personnel collisions that are the "
                "point of reading them together."
            )
        failures = _outcome_note(outcomes)
        if failures:
            note += " " + failures
        summary["note"] = note
        return {"content": [{"json": summary}], "status": "success"}

    return document_extract


__all__ = [
    "DOCUMENT_EXTRACT_TOOL_NAME",
    "make_document_extract_tool",
    "record_document_extract",
]
