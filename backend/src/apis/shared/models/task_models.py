"""Task models: the models the platform runs on a user's behalf outside the chat turn.

docs/specs/admin-model-assignments.md. A **task** is one named job the platform calls a model
for (titles, tool summaries, compaction, extraction). Each task has:

- **an assignment:** a catalog model id and, optionally, an effort level, set by an admin on
  the Task models tab, or nothing;
- **a default:** the code (or deploy-time) value from ``task_model_defaults``, used while
  nothing is assigned or the assignment no longer passes its checks.

Precedence, per call: a valid assignment, then the default. With nothing assigned every call
runs exactly as it did before this module existed.

Storage
-------
One item in the managed-models table, ``PK = SK = SYSTEM#TASK_MODELS``, holding a ``slots`` map
of task → ``{modelId, effort?, updatedAt, updatedBy}``. The catalog scan filters on
``begins_with(PK, "MODEL#")``, so the item never appears as a model row. No new table, index
or IAM: every process that resolves a task model already reads this table.

Read path, and what it costs
----------------------------
No task runs before the first token. Titles and tool summaries are side-channel tasks
concurrent with the stream, compaction runs in ``update_after_turn`` after the turn's last
call, and extraction runs inside a tool call. Even so, the read is cheap and bounded: the item
goes through the per-process ``config_cache`` (60 s TTL, single flight), like the catalog it is
validated against. That is at most one ``GetItem`` a minute per process, and none on a warm
cache. An admin write invalidates the cache in its own process. Every other process, the
Runtime's microVMs included, picks the change up within the TTL.

Resolution never raises. A store or catalog that cannot be read degrades to the assignment as
it was validated on write, or to the default.

Validation
----------
``eligibility_problem`` is the one rule for "may this model fill this task?", used on write by
the admin route and on every read here, so the two can't disagree. A model needs:

- an enabled catalog row, because pricing lives on the row (same reasoning as
  ``grants_model_access``);
- provider ``bedrock``, because every task calls Bedrock Converse;
- text in and text out;
- not retired. Retirement is resolved first, so a retired assignment follows its successor
  the way a turn's model does.

Effort must be one of the levels the model's row declares for its ``effort`` param (Anthropic's
``output_config.effort``). A level the row no longer declares is dropped at read time rather
than sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from apis.shared.caching import config_cache

from . import task_model_defaults as defaults
from .models import ManagedModel, ModelStatus
from .retirement import resolve_from_catalog

logger = logging.getLogger(__name__)

TASK_MODELS_KEY = "SYSTEM#TASK_MODELS"

#: ``config_cache`` key for the assignments item.
TASK_MODELS_CACHE_KEY = "task_models"

SOURCE_ASSIGNED = "assigned"
SOURCE_DEFAULT = "default"


class TaskModelSlot(str, Enum):
    SESSION_TITLE = "session_title"
    TOOL_SUMMARY = "tool_summary"
    COMPACTION_SUMMARY = "compaction_summary"
    DOCUMENT_EXTRACT = "document_extract"


def _compaction_default() -> str:
    # The Runtime's deploy-time override, read by name rather than imported: the constant
    # lives in agents/, which apis.shared may not import.
    return (
        os.environ.get("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID", "").strip()
        or defaults.COMPACTION_SUMMARY_DEFAULT_MODEL_ID
    )


def _extraction_default() -> str:
    from apis.shared.feature_flags import document_extract_model_id

    return document_extract_model_id()


def _extraction_available() -> bool:
    from apis.shared.feature_flags import document_extract_enabled

    return document_extract_enabled()


@dataclass(frozen=True)
class TaskSlotSpec:
    """One task, as the admin tab and the resolver both see it."""

    slot: TaskModelSlot
    title: str
    description: str
    guidance: str
    default_model_id: Callable[[], str]
    #: Whether a call writes a cost row today. An unmetered task's spend is the platform's,
    #: not counted against any user's quota, which the tab says next to the picker.
    metered: bool
    #: Whether the feature that runs this task is on in this deployment.
    available: Callable[[], bool] = lambda: True
    #: What the tab shows when the default is a deploy-time setting that may be empty.
    default_source: str = "code"


TASK_SLOTS: tuple[TaskSlotSpec, ...] = (
    TaskSlotSpec(
        slot=TaskModelSlot.SESSION_TITLE,
        title="Conversation titles",
        description=(
            "Names each new conversation from its first message, shown in the sidebar while "
            "the first answer is still streaming."
        ),
        guidance=(
            "Runs once per new conversation and only writes a few words. Pick a fast, "
            "low-cost model; a larger one makes titles slower without making them better."
        ),
        default_model_id=lambda: defaults.SESSION_TITLE_DEFAULT_MODEL_ID,
        metered=False,
    ),
    TaskSlotSpec(
        slot=TaskModelSlot.TOOL_SUMMARY,
        title="Tool activity summaries",
        description=(
            "Writes the one-line description of what the assistant just did with its tools, "
            'such as "Found the Syllabus Acknowledgment assignment in BIO 101".'
        ),
        guidance=(
            "Runs after every batch of tool calls, so it adds up quickly. Pick a fast, "
            "low-cost model that follows a short instruction reliably."
        ),
        default_model_id=lambda: defaults.TOOL_SUMMARY_DEFAULT_MODEL_ID,
        metered=False,
    ),
    TaskSlotSpec(
        slot=TaskModelSlot.COMPACTION_SUMMARY,
        title="Conversation compaction",
        description=(
            "Summarizes the older part of a long conversation so it fits the model's context. "
            "The summary replaces those turns, so whatever it drops, the assistant forgets."
        ),
        guidance=(
            "Runs rarely, only when a long conversation needs compacting, and the summary "
            "matters. Favor a model that keeps names, numbers and instructions exactly over the "
            "cheapest one: on our tests Nova Micro kept about 77% of planted facts and "
            "Nova 2 Lite about 99%. A change applies from each conversation's next compaction."
        ),
        default_model_id=_compaction_default,
        metered=False,
    ),
    TaskSlotSpec(
        slot=TaskModelSlot.DOCUMENT_EXTRACT,
        title="Structured extraction",
        description=(
            "Pulls a skill's named fields out of documents for the document_extract tool, one "
            "call per document."
        ),
        guidance=(
            "Pick a model with strict tool use (Claude 4.5 or later), so the field types are "
            "guaranteed rather than only requested. Its input scales with the document."
        ),
        default_model_id=_extraction_default,
        metered=True,
        available=_extraction_available,
        default_source="deploy",
    ),
)

_SPECS: Dict[TaskModelSlot, TaskSlotSpec] = {spec.slot: spec for spec in TASK_SLOTS}


def get_slot_spec(slot: TaskModelSlot | str) -> TaskSlotSpec:
    """The spec for ``slot``. Raises ``ValueError`` for an unknown slot."""
    return _SPECS[TaskModelSlot(slot)]


# =============================================================================
# Eligibility and effort: the one rule, for writes and reads
# =============================================================================


def _has_text(modalities: Iterable[str]) -> bool:
    return any(str(m).strip().lower() == "text" for m in modalities or ())


def eligibility_problem(record: Optional[ManagedModel]) -> Optional[str]:
    """Why ``record`` cannot fill a task, or ``None`` when it can.

    Every task is a text-in, text-out Bedrock Converse call, so the rule is the same for all
    of them today. It takes the slot's spec only once a task needs something more.
    """
    if record is None:
        return "It isn't in the model catalog, so its calls could not be priced."
    if not record.enabled:
        return "It's turned off in the model catalog."
    if record.status == ModelStatus.RETIRED:
        return "It's retired."
    if (record.provider or "").strip().lower() != "bedrock":
        return "Task models call Bedrock Converse, and this model runs on another transport."
    if not _has_text(record.input_modalities) or not _has_text(record.output_modalities):
        return "Task models need a model that reads and writes text."
    return None


def effort_levels(record: Optional[ManagedModel]) -> List[str]:
    """The effort levels ``record`` declares, low to high. Empty when it has no effort control."""
    if record is None or record.supported_params is None:
        return []
    spec = record.supported_params.params.get("effort")
    if spec is None or not spec.supported or not spec.allowed:
        return []
    return [str(level) for level in spec.allowed]


def effort_default(record: Optional[ManagedModel]) -> Optional[str]:
    """The row's own default effort, which applies when the assignment names none."""
    if record is None or record.supported_params is None:
        return None
    spec = record.supported_params.params.get("effort")
    if spec is None or not spec.supported or spec.default is None:
        return None
    return str(spec.default)


def _supports_temperature(record: Optional[ManagedModel]) -> bool:
    """Whether to send ``temperature``, read the way the turn path reads a row's params.

    A row that declares a params map lists the params the model takes, so a ``temperature``
    it leaves out is not sent: the Claude 5.x rows declare only ``effort`` and ``max_tokens``,
    and Bedrock refuses the call ("`temperature` is deprecated for this model") if it is.
    A row with no params map at all, or no row, keeps today's request.
    """
    if record is None or record.supported_params is None or not record.supported_params.params:
        return True
    spec = record.supported_params.params.get("temperature")
    return spec is not None and spec.supported


# =============================================================================
# The resolved choice
# =============================================================================


@dataclass(frozen=True)
class TaskModelChoice:
    """The model (and effort) one task call runs on."""

    slot: TaskModelSlot
    model_id: str
    effort: Optional[str]
    source: str
    record: Optional[ManagedModel] = None
    #: Set when an assignment exists but did not pass its checks, so the default ran.
    fallback_reason: Optional[str] = None

    @property
    def supports_temperature(self) -> bool:
        return _supports_temperature(self.record)

    def additional_model_request_fields(self) -> Optional[Dict[str, Any]]:
        """Converse ``additionalModelRequestFields`` for this choice, or ``None``.

        Effort is Anthropic's top-level ``output_config.effort``, which Converse carries in
        ``additionalModelRequestFields`` (``model_config._BEDROCK_PARAM_MAP``).
        """
        if not self.effort:
            return None
        return {"output_config": {"effort": self.effort}}

    def inference_config(self, *, temperature: float, max_tokens: int) -> Dict[str, Any]:
        """A Converse ``inferenceConfig``, dropping ``temperature`` for a model whose row refuses it."""
        config: Dict[str, Any] = {"maxTokens": max_tokens}
        if self.supports_temperature:
            config["temperature"] = temperature
        return config

    def converse_kwargs(self) -> Dict[str, Any]:
        """``modelId`` plus ``additionalModelRequestFields`` when there is any."""
        kwargs: Dict[str, Any] = {"modelId": self.model_id}
        extra = self.additional_model_request_fields()
        if extra:
            kwargs["additionalModelRequestFields"] = extra
        return kwargs


_warned: set[tuple[str, str, str]] = set()


def _warn_once(slot: TaskModelSlot, model_id: str, problem: str) -> None:
    key = (slot.value, model_id, problem)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "Task model assignment for %s no longer passes its checks; running the default",
        slot.value,
        extra={"event": "task_model_fallback", "slot": slot.value, "reason": problem},
    )


def choose_task_model(
    spec: TaskSlotSpec,
    assignment: Optional[Mapping[str, Any]],
    catalog: Optional[Sequence[ManagedModel]],
    default_model_id: str,
) -> TaskModelChoice:
    """Pure resolution of one task's model. ``catalog=None`` means it could not be read."""
    fallback_reason: Optional[str] = None
    assigned_id = str((assignment or {}).get("modelId") or "").strip()
    if assigned_id:
        effort = (assignment or {}).get("effort") or None
        if catalog is None:
            # Unverifiable right now. It passed these checks when it was written, which is
            # a better answer than the default during a catalog outage.
            return TaskModelChoice(spec.slot, assigned_id, effort, SOURCE_ASSIGNED)
        effective = resolve_from_catalog(assigned_id, catalog)
        problem = "It's retired with no replacement." if effective.denied else eligibility_problem(effective.record)
        if problem is None:
            if effort and effort not in effort_levels(effective.record):
                effort = None
            return TaskModelChoice(spec.slot, effective.model_id, effort, SOURCE_ASSIGNED, effective.record)
        fallback_reason = problem
        _warn_once(spec.slot, assigned_id, problem)

    if catalog is not None and default_model_id:
        effective = resolve_from_catalog(default_model_id, catalog)
        if effective.redirected and eligibility_problem(effective.record) is None:
            return TaskModelChoice(
                spec.slot, effective.model_id, None, SOURCE_DEFAULT, effective.record, fallback_reason
            )
        return TaskModelChoice(
            spec.slot, default_model_id, None, SOURCE_DEFAULT, effective.record, fallback_reason
        )
    return TaskModelChoice(spec.slot, default_model_id, None, SOURCE_DEFAULT, None, fallback_reason)


# =============================================================================
# Store
# =============================================================================


def _table_name() -> Optional[str]:
    return os.environ.get("DYNAMODB_MANAGED_MODELS_TABLE_NAME") or None


def _table(table_name: str) -> Any:
    from apis.shared.aws_clients import get_dynamodb_table

    return get_dynamodb_table(table_name)


def _read_item(table_name: str) -> List[dict]:
    response = _table(table_name).get_item(Key={"PK": TASK_MODELS_KEY, "SK": TASK_MODELS_KEY})
    item = response.get("Item")
    return [item] if item else []


def _slots_of(items: List[dict]) -> Dict[str, dict]:
    if not items:
        return {}
    slots = items[0].get("slots") or {}
    return {str(k): dict(v) for k, v in slots.items() if isinstance(v, Mapping)}


async def load_assignments(*, fresh: bool = False) -> Dict[str, dict]:
    """Every stored assignment, keyed by slot value. Empty when the table is not configured.

    ``fresh`` bypasses the cache, for the admin tab reading its own write back.
    """
    table_name = _table_name()
    if not table_name:
        return {}
    if fresh:
        config_cache.invalidate(TASK_MODELS_CACHE_KEY)
    items = await config_cache.get_or_load(
        TASK_MODELS_CACHE_KEY, lambda: asyncio.to_thread(_read_item, table_name)
    )
    return _slots_of(items)


def _write_slot(table_name: str, slot: str, value: Optional[dict]) -> None:
    from botocore.exceptions import ClientError

    table = _table(table_name)
    key = {"PK": TASK_MODELS_KEY, "SK": TASK_MODELS_KEY}
    names = {"#slots": "slots", "#slot": slot}
    if value is None:
        try:
            table.update_item(
                Key=key,
                UpdateExpression="REMOVE #slots.#slot",
                ConditionExpression="attribute_exists(#slots)",
                ExpressionAttributeNames=names,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return
    for _ in range(2):
        try:
            table.update_item(
                Key=key,
                UpdateExpression="SET #slots.#slot = :value",
                ConditionExpression="attribute_exists(#slots)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={":value": value},
            )
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        # No item (or no map) yet: create it. If another writer created it first, the
        # condition fails and the loop sets the slot on theirs.
        try:
            table.put_item(
                Item={**key, "slots": {slot: value}},
                ConditionExpression="attribute_not_exists(PK)",
            )
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("Could not write the task model assignment; try again.")


async def save_assignment(
    slot: TaskModelSlot,
    model_id: Optional[str],
    effort: Optional[str],
    *,
    updated_by: str,
) -> Optional[dict]:
    """Store (or, with ``model_id=None``, clear) one slot. Validation is the caller's job."""
    table_name = _table_name()
    if not table_name:
        raise RuntimeError("DYNAMODB_MANAGED_MODELS_TABLE_NAME environment variable is required")
    value: Optional[dict] = None
    if model_id:
        value = {
            "modelId": model_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "updatedBy": updated_by,
        }
        if effort:
            value["effort"] = effort
    await asyncio.to_thread(_write_slot, table_name, slot.value, value)
    config_cache.invalidate(TASK_MODELS_CACHE_KEY)
    return value


# =============================================================================
# Resolver
# =============================================================================


async def _load_catalog() -> Optional[List[ManagedModel]]:
    if not _table_name():
        return None
    from .managed_models import list_all_managed_models

    try:
        return await list_all_managed_models()
    except Exception:
        logger.warning("Model catalog unavailable; task models run unverified", exc_info=True)
        return None


async def resolve_task_model(
    slot: TaskModelSlot, *, default_model_id: Optional[str] = None
) -> TaskModelChoice:
    """The model one call of ``slot`` runs on. Never raises.

    ``default_model_id`` overrides the registry default for a caller that already holds a
    configured one (compaction's ``CompactionConfig.summary_model_id``).
    """
    spec = get_slot_spec(slot)
    try:
        default_id = default_model_id or spec.default_model_id()
    except Exception:
        logger.warning("Task model default for %s could not be read", slot.value, exc_info=True)
        default_id = ""
    try:
        assignments = await load_assignments()
    except Exception:
        logger.warning("Task model assignments unavailable; running defaults", exc_info=True)
        assignments = {}
    assignment = assignments.get(spec.slot.value)
    catalog = await _load_catalog()
    try:
        return choose_task_model(spec, assignment, catalog, default_id)
    except Exception:
        logger.warning("Task model resolution failed for %s; running the default", slot.value, exc_info=True)
        return TaskModelChoice(spec.slot, default_id, None, SOURCE_DEFAULT)


# =============================================================================
# Admin view
# =============================================================================


@dataclass
class TaskSlotView:
    """Everything the Task models tab shows for one task. Serialized by the admin route."""

    spec: TaskSlotSpec
    default_model_id: str
    default_record: Optional[ManagedModel]
    default_notice: Optional[str]
    assignment: Optional[dict]
    assignment_record: Optional[ManagedModel]
    assignment_problem: Optional[str]
    effective: TaskModelChoice
    eligible: List[ManagedModel] = field(default_factory=list)


def default_notice(spec: TaskSlotSpec, default_id: str, catalog: Sequence[ManagedModel]) -> Optional[str]:
    """What an admin should know about the default: the retirement watch, in one line."""
    if not default_id:
        return (
            "No default is configured for this deployment (DOCUMENT_EXTRACT_MODEL_ID is unset). "
            "Assign a model, or the task reports a configuration error."
        )
    effective = resolve_from_catalog(default_id, catalog)
    if effective.denied:
        return "The default is retired in the catalog with no replacement. Assign a model."
    if effective.redirected:
        name = effective.record.model_name if effective.record else effective.model_id
        return f"The default is retired in the catalog, so this task runs its replacement, {name}."
    if effective.record is None:
        return (
            "The default isn't in the model catalog, so its pricing and retirement aren't tracked "
            "here. Watch AWS model lifecycle notices for it, or add it to the catalog."
        )
    return None


def build_slot_views(
    assignments: Mapping[str, dict], catalog: Sequence[ManagedModel]
) -> List[TaskSlotView]:
    """One view per task, in display order, against an already-read catalog."""
    by_id = {m.model_id: m for m in catalog}
    eligible = [m for m in catalog if eligibility_problem(m) is None and m.status != ModelStatus.DEPRECATED]
    views: List[TaskSlotView] = []
    for spec in TASK_SLOTS:
        try:
            default_id = spec.default_model_id()
        except Exception:
            default_id = ""
        assignment = assignments.get(spec.slot.value)
        assigned_id = (assignment or {}).get("modelId")
        assignment_record = by_id.get(assigned_id) if assigned_id else None
        effective = choose_task_model(spec, assignment, catalog, default_id)
        views.append(
            TaskSlotView(
                spec=spec,
                default_model_id=default_id,
                default_record=by_id.get(default_id),
                default_notice=default_notice(spec, default_id, catalog),
                assignment=assignment,
                assignment_record=assignment_record,
                assignment_problem=effective.fallback_reason,
                effective=effective,
                eligible=eligible,
            )
        )
    return views


def validate_assignment(
    model_id: str, effort: Optional[str], catalog: Sequence[ManagedModel]
) -> tuple[str, Optional[str]]:
    """Check an admin's assignment. Returns the id to store. Raises ``ValueError`` with the reason.

    A retired id is stored as its successor, because that is what would run.
    """
    effective = resolve_from_catalog(model_id, catalog)
    if effective.denied:
        raise ValueError("That model is retired with no replacement.")
    problem = eligibility_problem(effective.record)
    if problem is not None:
        raise ValueError(problem)
    if effective.record is not None and effective.record.status == ModelStatus.DEPRECATED:
        raise ValueError("That model is deprecated. Choose an active model.")
    if effort:
        levels = effort_levels(effective.record)
        if not levels:
            raise ValueError("That model has no effort setting. Leave effort at the model default.")
        if effort not in levels:
            raise ValueError(f"Effort must be one of: {', '.join(levels)}.")
    return effective.model_id, effort or None
