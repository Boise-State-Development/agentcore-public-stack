"""Version-keyed ledger rollups bumped per model call (docs/specs/release-ledger.md §5.2, D3).

Every ``C#`` cost row is a fact about one model call. The release ledger keys
those facts by the build that wrote them so "did 1.34.0 cost more per turn than
1.33.0" is a range query rather than a scan: there is no fleet-wide date index
on cost rows, and a nightly scan would read the whole 365-day table. So, beside
the ``ROLLUP#DAILY`` / ``#MONTHLY`` / ``#MODEL`` bumps and with their semantics
(after the stream, fire-and-forget, failures logged and swallowed), each call
``ADD``s itself into five row families of the ``system-cost-rollup`` table::

    LEDGER#VERSION          <date>#<v>#<b>
    LEDGER#MODEL#<month>    <date>#<v>#<b>#<modelId>
    LEDGER#TOOL#<month>     <date>#<v>#<b>#<toolName>     (one per tool the call ran)
    LEDGER#AGENT#<month>    <date>#<v>#<b>#<agentId>      (when the call ran under an Agent)
    LEDGER#PROJECT#<month>  <date>#<v>#<b>#<projectId>    (when it ran a project's harness)

:func:`ledger_updates` is the **one** mapping from a cost row to those counters,
and it reads the row exactly as it is stored (camelCase attributes, ``Decimal``
or ``float``). The live writer hands it the item it just put; the backfill
(``scripts/release_ledger_backfill.py``) hands it rows read back through
:data:`~apis.shared.observability.content_policy.LEDGER_ROW_PROJECTION`. One
function for both is what makes a backfilled range and a live range add up the
same way. It is pure, stdlib-only, and reads nothing outside that projection.

**Content-free** (D7): a ledger row carries ids (version, build, model, tool,
agent, project) and numbers. The cost row's ``sessionId`` and ``messageId`` are
read only to tell a voice or api-converse call from a chat one, and are never
written.

**A turn is counted once, on its last row.** A cost row does not store its
``call_index``, so the spec's first draft ("calls with ``call_index == 0``") is
not something a reader of the row can apply. The turn's *last* row is marked: it
alone carries ``turnDurationMs``, which ``StreamCoordinator._store_turn_call_metadata``
writes on the final row of every turn it meters (the success, failure and
Stop/disconnect paths all go through it), and it is the row that carries
``emptyAnswer``. So ``turns``, the outcome counters, ``emptyAnswerTurns``,
``documentTurns`` and ``turnDurationMsSum`` are read from that row only, and a
four-call turn adds 4 to ``calls`` and 1 to ``turns``. A call that is not a chat
turn's (a handoff brief, a voice session, an api-converse request, a
``document_extract`` side call) carries no ``turnDurationMs`` and is counted
under ``callKind.<kind>``, never as a turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

ROLLUP_TABLE_ENV = "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME"

#: Set once per ledger row by the live writer (``if_not_exists``): when live
#: counting first reached that row.
FIRST_LIVE_AT = "firstLiveAt"
#: Set by the backfill on every row it writes, and the PK of its run markers.
BACKFILLED_AT = "backfilledAt"
BACKFILL_PK = "LEDGER#BACKFILL"
#: Set on a ``C#`` row, in the same ``put_item`` that stores it, when the live
#: writer will count it. The backfill skips a row carrying it, so no backfill
#: range can count a call twice: a row's ``timestamp`` is when the call ran,
#: earlier than when it was stored and counted, so the first live turn's rows
#: sit just *before* ``firstLiveAt``.
LEDGERED = "ledgered"
UNKNOWN = "unknown"

#: How a turn ended, on every row of it (``outcome``). Declared here, in
#: ``apis.shared``, because the ledger writer runs in app-api too, which may not
#: import ``agents``; the stream coordinator imports it from here.
TURN_OUTCOMES = frozenset({"completed", "paused", "error", "stopped", "interrupted"})

#: ``outcome`` -> the turn counter it bumps.
OUTCOME_COUNTERS = {
    "completed": "completedTurns",
    "paused": "pausedTurns",
    "error": "errorTurns",
    "stopped": "stoppedTurns",
    "interrupted": "interruptedTurns",
}

#: The kinds a call is classified into. ``callKind`` on the row wins; the rest
#: are recognised by the id shapes their writers use.
CALL_KINDS = ("chat", "handoff_brief", "voice", "api_converse", "document_extract", "other")
_KIND_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")

#: What one character of a tool result costs as input, in tokens (D4). An
#: estimate, and labelled one everywhere it is shown: Bedrock reports usage per
#: call, never per content block.
CHARS_PER_TOKEN = 4

_MILLION = Decimal(1_000_000)
_USD_QUANTUM = Decimal("1e-10")


@dataclass
class LedgerUpdate:
    """One ``UpdateItem``: ``ADD`` the counters, ``SET`` the descriptive ids."""

    pk: str
    sk: str
    add: Dict[str, Any] = field(default_factory=dict)
    set: Dict[str, str] = field(default_factory=dict)

    def bump(self, name: str, value: Any) -> None:
        """Add ``value`` to counter ``name``, skipping zeros (an absent counter reads as 0)."""
        if not value:
            return
        self.add[name] = self.add.get(name, 0) + value

    def merge(self, other: "LedgerUpdate") -> None:
        for name, value in other.add.items():
            self.bump(name, value)
        self.set.update(other.set)


# ---------------------------------------------------------------------------
# Reading a cost row
# ---------------------------------------------------------------------------


def _get(row: Mapping[str, Any], path: str) -> Any:
    node: Any = row
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError, InvalidOperation):
        return 0


def _usd(value: Any) -> Decimal:
    """A dollar amount as ``Decimal``; a breakdown dict reads its ``total``. Bad values are 0."""
    if isinstance(value, Mapping):
        value = value.get("total")
    if value is None or isinstance(value, bool):
        return Decimal(0)
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)
    return amount if amount.is_finite() else Decimal(0)


def _key_part(value: Any) -> str:
    """An id as it goes into a sort key: a string, never empty."""
    text = str(value).strip() if value is not None else ""
    return text or UNKNOWN


def call_kind(row: Mapping[str, Any]) -> str:
    """What kind of call a cost row records (``callKind.<kind>`` on the version row).

    ``callKind`` on the row (a handoff brief) wins. Rows from writers that set
    none are told apart by the ids they write: api-converse uses a
    ``api-converse-<key>`` pseudo-session, voice a ``voice:<n>`` message id and
    ``document_extract`` an ``extract:<toolUseId>`` one. Everything else is a
    chat turn's call. An unexpected ``callKind`` value is bucketed ``other``
    rather than written as an attribute name.
    """
    kind = row.get("callKind")
    if kind:
        kind = str(kind)
        return kind if _KIND_PATTERN.match(kind) else "other"
    if str(row.get("sessionId") or "").startswith("api-converse-"):
        return "api_converse"
    message_id = row.get("messageId")
    if isinstance(message_id, str):
        if message_id.startswith("voice:"):
            return "voice"
        if message_id.startswith("extract:"):
            return "document_extract"
    return "chat"


def is_turn_row(row: Mapping[str, Any]) -> bool:
    """Whether this row is a chat turn's last row, the one that counts the turn."""
    return call_kind(row) == "chat" and row.get("turnDurationMs") is not None


def row_date(row: Mapping[str, Any]) -> Optional[str]:
    """``YYYY-MM-DD`` (UTC) of the row's ``timestamp`` — the date ``ROLLUP#DAILY`` uses."""
    raw = row.get("timestamp")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%Y-%m-%d")


def _tool_census(row: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    tools = row.get("toolCalls")
    if not isinstance(tools, Mapping):
        return {}
    return {str(name): entry for name, entry in tools.items() if name and isinstance(entry, Mapping)}


def _compaction_kinds(row: Mapping[str, Any]) -> List[str]:
    events = row.get("compactionEvents")
    if not isinstance(events, list):
        return []
    return [str(e.get("kind")) for e in events if isinstance(e, Mapping)]


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def ledger_updates(row: Mapping[str, Any]) -> List[LedgerUpdate]:
    """The ledger ``UpdateItem``s one cost row produces. Pure; ``[]`` for an undatable row."""
    date = row_date(row)
    if date is None:
        return []
    month = date[:7]
    version = _key_part(row.get("appVersion"))
    build = _key_part(row.get("build"))
    prefix = f"{date}#{version}#{build}"
    stamp = {"date": date, "appVersion": version, "build": build}

    kind = call_kind(row)
    turn = is_turn_row(row)
    usage = row.get("tokenUsage") if isinstance(row.get("tokenUsage"), Mapping) else {}
    input_tokens = _int(usage.get("inputTokens"))
    output_tokens = _int(usage.get("outputTokens"))
    cache_read = _int(usage.get("cacheReadInputTokens"))
    cache_write = _int(usage.get("cacheWriteInputTokens"))
    cost = _usd(row.get("cost"))
    wasted = _usd(row.get("wastedUsd"))
    status = row.get("cacheStatus")
    agent_switched = bool(row.get("agentSwitched"))
    model_switched = bool(row.get("modelSwitched"))
    first_token_ms = _int(_get(row, "latency.timeToFirstToken"))
    input_rate = _usd(_get(row, "modelInfo.pricingSnapshot.inputPricePerMtok"))
    tools = _tool_census(row)
    tool_calls = sum(_int(t.get("calls")) for t in tools.values())
    tool_errors = sum(_int(t.get("errors")) for t in tools.values())
    tool_result_chars = sum(_int(t.get("resultChars")) for t in tools.values())
    tool_input_chars = sum(_int(t.get("inputChars")) for t in tools.values())
    document_reads = _int(_get(row, "documentReads.calls"))
    compactions = _compaction_kinds(row)

    # -- LEDGER#VERSION ---------------------------------------------------
    version_row = LedgerUpdate("LEDGER#VERSION", prefix, set=dict(stamp))
    version_row.bump("calls", 1)
    version_row.bump(f"callKind.{kind}", 1)
    version_row.bump("cost", cost)
    version_row.bump("inputTokens", input_tokens)
    version_row.bump("outputTokens", output_tokens)
    version_row.bump("cacheReadTokens", cache_read)
    version_row.bump("cacheWriteTokens", cache_write)
    version_row.bump("wastedUsd", wasted)
    # The same split the session anatomy makes (admin costs service): an
    # agent- or model-switch miss is a subset of the avoidable misses, and its
    # dollars a subset of `wastedUsd`, never a deduction from it.
    if status == "miss_avoidable":
        version_row.bump("avoidableMissCount", 1)
        if agent_switched:
            version_row.bump("agentSwitchMissCount", 1)
            version_row.bump("agentSwitchUsd", wasted)
        elif model_switched:
            version_row.bump("modelSwitchMissCount", 1)
            version_row.bump("modelSwitchUsd", wasted)
    elif status == "partial_miss":
        version_row.bump("partialMissCount", 1)
        version_row.bump("partialMissUsd", wasted)
    elif status == "uncached":
        version_row.bump("uncachedCalls", 1)
    if status == "estimated" or row.get("usageEstimated"):
        version_row.bump("estimatedCalls", 1)
    if (input_tokens + output_tokens + cache_read + cache_write) > 0 and row.get("cost") is None:
        version_row.bump("unmeteredCalls", 1)
    version_row.bump("toolCalls", tool_calls)
    version_row.bump("toolErrors", tool_errors)
    version_row.bump("toolResultChars", tool_result_chars)
    version_row.bump("toolInputChars", tool_input_chars)
    version_row.bump("documentReads", document_reads)
    version_row.bump("documentTokens", _int(row.get("documentTokens")))
    version_row.bump("compactionEvents", compactions.count("checkpoint"))
    version_row.bump("forcedCompactions", compactions.count("forced"))
    version_row.bump("floorUnreachableCompactions", compactions.count("floor_unreachable"))
    if first_token_ms > 0:
        version_row.bump("firstTokenMsSum", first_token_ms)
        version_row.bump("firstTokenCount", 1)
    if turn:
        version_row.bump("turns", 1)
        version_row.bump("turnDurationMsSum", _int(row.get("turnDurationMs")))
        if row.get("hasDocuments") or _int(row.get("documentDigests")) > 0:
            version_row.bump("documentTurns", 1)
        outcome = row.get("outcome")
        if outcome in TURN_OUTCOMES:
            # `outcomeTurns` is the denominator for the outcome and empty-answer
            # shares: rows written before PR-2 carry no outcome at all, and
            # dividing by every turn would read those as clean.
            version_row.bump("outcomeTurns", 1)
            version_row.bump(OUTCOME_COUNTERS[outcome], 1)
            if row.get("emptyAnswer"):
                version_row.bump("emptyAnswerTurns", 1)
    updates = [version_row]

    # -- LEDGER#MODEL#<month> ---------------------------------------------
    model_id = _key_part(_get(row, "modelInfo.modelId"))
    model_row = LedgerUpdate(
        f"LEDGER#MODEL#{month}", f"{prefix}#{model_id}", set={**stamp, "modelId": model_id}
    )
    model_row.bump("calls", 1)
    model_row.bump("turns", 1 if turn else 0)
    for name in ("cost", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
                 "wastedUsd", "partialMissCount", "firstTokenMsSum", "firstTokenCount"):
        model_row.bump(name, version_row.add.get(name, 0))
    updates.append(model_row)

    # -- LEDGER#TOOL#<month> ----------------------------------------------
    for tool_name, entry in tools.items():
        tool_row = LedgerUpdate(
            f"LEDGER#TOOL#{month}", f"{prefix}#{tool_name}", set={**stamp, "toolId": tool_name}
        )
        result_chars = _int(entry.get("resultChars"))
        tool_row.bump("calls", _int(entry.get("calls")))
        tool_row.bump("errors", _int(entry.get("errors")))
        tool_row.bump("resultChars", result_chars)
        tool_row.bump("inputChars", _int(entry.get("inputChars")))
        tool_row.bump("durationMsSum", _int(entry.get("durationMs")))
        tool_row.bump("timedCalls", _int(entry.get("timedCalls")))
        # An estimate (D4): result characters / 4 tokens, priced at this
        # call's model input rate. Rows from before the census weighed results
        # have no `resultChars` and add nothing here.
        tool_row.bump(
            "estInputUsd",
            (Decimal(result_chars) / CHARS_PER_TOKEN * input_rate / _MILLION).quantize(_USD_QUANTUM),
        )
        if tool_row.add:
            updates.append(tool_row)

    # -- LEDGER#AGENT#<month> ---------------------------------------------
    agent_id = row.get("turnAgentId")
    if agent_id:
        agent_id = _key_part(agent_id)
        agent_row = LedgerUpdate(
            f"LEDGER#AGENT#{month}", f"{prefix}#{agent_id}", set={**stamp, "agentId": agent_id}
        )
        agent_row.bump("calls", 1)
        agent_row.bump("turns", 1 if turn else 0)
        agent_row.bump("cost", cost)
        agent_row.bump("wastedUsd", wasted)
        agent_row.bump("toolCalls", tool_calls)
        agent_row.bump("toolErrors", tool_errors)
        agent_row.bump("agentSwitched", 1 if agent_switched else 0)
        updates.append(agent_row)

    # -- LEDGER#PROJECT#<month> -------------------------------------------
    project_id = row.get("projectId")
    if project_id:
        project_id = _key_part(project_id)
        project_row = LedgerUpdate(
            f"LEDGER#PROJECT#{month}", f"{prefix}#{project_id}", set={**stamp, "projectId": project_id}
        )
        project_row.bump("calls", 1)
        project_row.bump("turns", 1 if turn else 0)
        project_row.bump("cost", cost)
        project_row.bump("wastedUsd", wasted)
        project_row.bump("toolCalls", tool_calls)
        project_row.bump("documentReads", document_reads)
        updates.append(project_row)

    return updates


def merge_updates(updates: Iterable[LedgerUpdate]) -> List[LedgerUpdate]:
    """Fold updates that share a key into one, in first-seen order (the backfill's batching)."""
    merged: Dict[tuple, LedgerUpdate] = {}
    for update in updates:
        key = (update.pk, update.sk)
        if key in merged:
            merged[key].merge(update)
        else:
            merged[key] = LedgerUpdate(update.pk, update.sk, dict(update.add), dict(update.set))
    return list(merged.values())


def update_item_kwargs(update: LedgerUpdate, *, first_live_at: Optional[str] = None) -> Dict[str, Any]:
    """The ``Table.update_item`` arguments for one update.

    Every attribute name is aliased, because ``callKind.<kind>`` is a *flat*
    attribute whose name contains a dot (not a nested map: ``ADD`` on a path
    under a map that does not exist yet fails) and several counters are
    DynamoDB reserved words.

    ``first_live_at`` is set by the live writer only, as
    ``firstLiveAt = if_not_exists(firstLiveAt, :now)`` on the same write: the
    earliest one across ``LEDGER#VERSION`` is where live counting began, which
    is where the backfill must stop (``scripts/release_ledger_backfill.py``).
    """
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    adds: List[str] = []
    sets: List[str] = []
    for index, (name, value) in enumerate(sorted(update.add.items())):
        names[f"#a{index}"] = name
        values[f":a{index}"] = value if isinstance(value, (int, Decimal)) else Decimal(str(value))
        adds.append(f"#a{index} :a{index}")
    for index, (name, value) in enumerate(sorted(update.set.items())):
        names[f"#s{index}"] = name
        values[f":s{index}"] = value
        sets.append(f"#s{index} = :s{index}")
    if first_live_at is not None:
        names["#firstLiveAt"] = FIRST_LIVE_AT
        values[":firstLiveAt"] = first_live_at
        sets.append("#firstLiveAt = if_not_exists(#firstLiveAt, :firstLiveAt)")
    expression = "ADD " + ", ".join(adds)
    if sets:
        expression += " SET " + ", ".join(sets)
    return {
        "Key": {"PK": update.pk, "SK": update.sk},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


# ---------------------------------------------------------------------------
# The live writer
# ---------------------------------------------------------------------------


async def write_ledger_updates(updates: Sequence[LedgerUpdate], table: Any) -> int:
    """Apply ``updates`` concurrently; returns how many failed. Never raises."""
    pending = [update for update in updates if update.add]
    if not pending:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    results = await asyncio.gather(
        *(asyncio.to_thread(table.update_item, **update_item_kwargs(u, first_live_at=now)) for u in pending),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        logger.warning(
            "Release ledger: %d of %d rollup writes failed (non-critical): %s",
            len(failures), len(pending), failures[0],
        )
    return len(failures)


def live_ledger_writes_on() -> bool:
    """Whether a cost row stored now will be counted by the live writer.

    The same two conditions :func:`bump_ledger_for_row` checks, so the
    :data:`LEDGERED` mark and the write it promises never disagree about
    whether the ledger is on.
    """
    from apis.shared.feature_flags import release_ledger_enabled

    return release_ledger_enabled() and bool(os.environ.get(ROLLUP_TABLE_ENV))


async def bump_ledger_for_row(row: Optional[Mapping[str, Any]]) -> None:
    """Add one stored cost row to the ledger, when ``RELEASE_LEDGER_ENABLED`` is on.

    Runs inside the fire-and-forget ``_update_system_rollups_async`` task, after
    the stream. Best-effort: a failure is logged and never reaches the turn.
    """
    if not row:
        return
    try:
        if not live_ledger_writes_on():
            return
        table_name = os.environ[ROLLUP_TABLE_ENV]
        from apis.shared.aws_clients import get_dynamodb_table

        await write_ledger_updates(ledger_updates(row), get_dynamodb_table(table_name))
    except Exception as exc:  # noqa: BLE001 - the ledger must never fail a turn
        logger.warning("Release ledger rollup skipped (non-critical): %s", exc)
