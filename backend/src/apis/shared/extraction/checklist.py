"""A workflow checklist call: one generic strict tool, the field list in the instruction, types checked after.

``docs/specs/workflow-designer.md`` §22.1–§22.3. A checklist step runs named field sets over a
workflow run's documents, one model call per set, on the prompt-step path (the documents first, a
cachePoint after them, one warm call per model). This module is the part of that call that does not
depend on the Runtime: the constant request pieces, and reading a reply back into typed values.

WHY ONE GENERIC TOOL, AND NOT THE FIELD SET'S OWN SCHEMA
--------------------------------------------------------
``document_extract`` sends each set's own strict schema (``schema.build_model``) as its tool. That
is right for a one-off side call, and wrong here: ``toolConfig`` renders ahead of the documents, so
a per-set tool makes every set write the whole document prefix again. Measured on dev (§22.1): a
per-set tool, or a per-set native output schema, gave **0 of 12** later sets a cache read; one
constant tool gave **12 of 12**, at the same accuracy. One tool list holding every set's schema is
refused by Bedrock ("The compiled grammar is too large") at 20 fields in all.

So the request carries :data:`TOOL_SPEC`, ``record_checklist {fields: [{name, value, quote}]}``,
the same for every set, every run and every deployment, with ``strict: true`` so Bedrock guarantees
the reply's *shape*; the field list travels in the instruction (:func:`instruction`), written exactly
as ``extractor._prompt`` writes it. What strict mode can no longer guarantee, the *types*, is checked
here: every value arrives as a string and :func:`read_answers` coerces it by the field's declared type
and validates the result with the set's own generated model (``schema.model_for_field_set``). A value
that does not coerce becomes null; it is never guessed into shape.

``toolChoice`` is ``auto``: Bedrock refuses a forced choice on Sonnet 5.5. A reply with no tool call
is the caller's to retry once.

WHY ``strict_tools`` IS SET HERE
--------------------------------
``BedrockModel(strict_tools=True)`` puts ``strict: true`` on every tool in that model's requests.
:func:`build_checklist_model` builds a dedicated instance whose requests only ever carry
:data:`TOOL_SPEC`, which we author, so "every tool" is that one schema
(``tests/architecture/test_strict_tools_single_call_site.py`` lists this module beside the extractor).

Nothing here is on a turn's path, and nothing reaches a turn's ``toolConfig`` or system prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import ValidationError

from apis.shared.files.source_provenance import DocumentUnits

from .field_sets import (
    TYPE_BOOLEAN,
    TYPE_CURRENCY,
    TYPE_DATE,
    TYPE_ENUM,
    TYPE_LIST,
    TYPE_NUMBER,
    Field,
    FieldSet,
)
from .rows import Row, build_rows, event_fields, model_facing_result
from .schema import model_for_field_set

logger = logging.getLogger(__name__)

#: The one tool every checklist call carries.
TOOL_NAME = "record_checklist"

#: ``record_checklist``, exactly as §22.1 measured it: a list of ``{name, value, quote}``, ``value``
#: a string or null, every property required, ``additionalProperties: false``. Already in strict
#: mode's subset, so ``ensure_strict_json_schema`` leaves it as it is (pinned by a test). Constant:
#: its bytes are part of every checklist call's cached prefix, and a change rewrites it once.
TOOL_SPEC: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Record one checklist's values: one entry per field asked for.",
    "inputSchema": {
        "json": {
            "type": "object",
            "additionalProperties": False,
            "required": ["fields"],
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "value", "quote"],
                        "properties": {
                            "name": {"type": "string", "description": "The field's name, exactly as listed."},
                            "value": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "description": (
                                    "The value, or null if the document does not state it. "
                                    "For a verdict: pass, fail or cant_tell."
                                ),
                            },
                            "quote": {
                                "type": "string",
                                "description": "The exact passage the value comes from; empty if none.",
                            },
                        },
                    },
                }
            },
        }
    },
}

#: The system prompt of every checklist call, constant per deployment so it sits in the cached
#: prefix. ``extractor._SYSTEM_PROMPT``'s rules, framed as one step of a workflow, plus how to answer.
SYSTEM_PROMPT = (
    "You are filling in one checklist for a step of a workflow the user started. The user's message "
    "gives you the documents the workflow shares, then any results from earlier steps, then the "
    "checklist: the fields to find.\n\n"
    "Rules, in order of importance:\n"
    "1. Every value must come from the documents in front of you. Never infer a value, never "
    "estimate one, and never repeat a value from another field because it looks plausible. A field "
    "that asks for a judgement (one of a listed set of answers) is answered from what the documents "
    "show, and with the answer that says you cannot tell when they do not let you decide.\n"
    "2. If the documents do not state a field, set its value to null and its quote to an empty "
    "string. A null is a correct, useful answer. A guess is not.\n"
    "3. For every value you do give, quote the passage you took it from verbatim, copied character "
    "for character from the document, including its own punctuation and casing. Do not tidy it, do "
    "not join lines, and do not paraphrase. The quote is checked against the document automatically.\n"
    "4. Quote the shortest passage that actually carries the value, ideally the sentence containing it.\n"
    "5. Give the value as the document expresses it, as text: keep currency symbols and thousands "
    "separators as written, give a yes-or-no field as yes or no, and give a field with listed answers "
    "as exactly one of them.\n\n"
    f"Answer by calling the {TOOL_NAME} tool once, with one entry per field, each named exactly as "
    "the checklist lists it. Do not answer in prose."
)

#: Ahead of a step's own guidance inside the instruction.
GUIDANCE_HEADING = "Guidance for this checklist:"

#: The instruction's last line.
CALL_ONCE = (
    f"Call {TOOL_NAME} once, with one entry per field above: its value as text (null if the documents "
    "do not state it) and the exact passage it comes from, copied verbatim (empty if none)."
)


def field_list(field_set: FieldSet) -> str:
    """The "Fields to find" lines, exactly as ``extractor._prompt`` writes them (pinned by a test)."""
    return "\n".join(
        f"- {field.name} ({field.type}): {field.find}"
        + (f" One of: {', '.join(field.enum)}." if field.enum else "")
        + (" May legitimately be absent." if field.optional else "")
        for field in field_set.fields
    )


def instruction(field_set: FieldSet, guidance: str = "") -> str:
    """One set's instruction: the set, its fields, the step's guidance (if any), and the call to make."""
    parts = [
        f"Checklist: {field_set.title} (version {field_set.version})",
        f"Fields to find:\n{field_list(field_set)}",
    ]
    if (guidance or "").strip():
        parts.append(f"{GUIDANCE_HEADING}\n{guidance.strip()}")
    parts.append(CALL_ONCE)
    return "\n\n".join(parts)


def tool_input_of(content: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    """The first ``record_checklist`` call's input in a reply's content, or ``None`` when there is none.

    An input whose ``fields`` is not a list counts as no call: strict mode should make that
    impossible, and the caller's retry is the honest answer if it ever happens.
    """
    for block in content or ():
        tool_use = block.get("toolUse") if isinstance(block, dict) else None
        if not isinstance(tool_use, dict) or tool_use.get("name") != TOOL_NAME:
            continue
        payload = tool_use.get("input")
        if isinstance(payload, dict) and isinstance(payload.get("fields"), list):
            return payload
    return None


# --- Coercion --------------------------------------------------------------------------------------

#: A value that names its own absence is null, whatever the field's type.
_ABSENT = frozenset({"", "null", "none", "n/a", "na", "not found", "not stated", "not specified", "unknown", "-", "—"})
_TRUE = frozenset({"yes", "y", "true", "t", "1"})
_FALSE = frozenset({"no", "n", "false", "f", "0"})
#: Comma groups only in thousands position, so "1, 2 and 3" is three numbers, never 123.
_NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")
_ORDINAL_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
#: The date shapes ``source_provenance`` reads, so a coerced date is one its value check can match.
_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%b. %d, %Y",
    "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y",
)


def _enum_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _as_number(text: str) -> Optional[float]:
    """The one number ``text`` states, or ``None`` when it states none or several."""
    found = _NUMBER_RE.findall(text)
    if len(found) != 1:
        return None
    try:
        return float(found[0].replace(",", ""))
    except ValueError:
        return None


def _as_date(text: str) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", _ORDINAL_RE.sub("", text)).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed: date = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        return parsed.isoformat()
    return None


def coerce_value(field: Field, raw: Any) -> Any:
    """``raw`` (a reply's string) as ``field``'s type, or ``None`` when it does not coerce.

    - **number**: the one number the string states (``"15 pages"`` → ``15.0``); none or several → null.
    - **currency**: the string as written (structured-extraction D6), when it states a number.
    - **date**: ISO ``YYYY-MM-DD`` from the shapes ``source_provenance`` reads.
    - **boolean**: yes/no, true/false.
    - **enum**: one of the set's answers, matched exactly, then ignoring case and punctuation
      (``"Can't tell"`` → ``cant_tell``).
    - **string**: as written.

    A ``list`` field is never coerced: a string can't carry a list without a convention, so a set
    holding one is never called (:func:`unsupported_fields`).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.casefold() in _ABSENT:
        return None
    if field.type == TYPE_NUMBER:
        return _as_number(text.replace("$", "").replace("%", ""))
    if field.type == TYPE_CURRENCY:
        return text if _NUMBER_RE.search(text) else None
    if field.type == TYPE_DATE:
        return _as_date(text)
    if field.type == TYPE_BOOLEAN:
        key = text.casefold().rstrip(".")
        if key in _TRUE:
            return True
        if key in _FALSE:
            return False
        return None
    if field.type == TYPE_ENUM:
        if text in field.enum:
            return text
        key = _enum_key(text)
        matches = [member for member in field.enum if _enum_key(member) == key]
        return matches[0] if len(matches) == 1 else None
    return text


def unsupported_fields(field_set: FieldSet) -> List[str]:
    """The fields a checklist call can't fill: ``list`` fields, which the format refuses on a checklist
    step. A set edited to hold one after the run was created is that set's ``error``, never a call."""
    return [field.name for field in field_set.fields if field.type == TYPE_LIST]


def read_answers(field_set: FieldSet, tool_input: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """A ``record_checklist`` input as ``{field_name: {"value": …, "quote": …}}``, in the set's order.

    - A field the reply leaves out is null, with an empty quote.
    - An unknown name is dropped; a name given twice keeps its first entry.
    - Each value is coerced by its field's type (:func:`coerce_value`), and the whole answer is then
      validated by the set's own generated model; a value that still fails is null.

    The result has exactly the shape ``extractor.extract_fields`` returns, so the provenance rows
    (``rows.build_rows``) read both the same way.
    """
    by_name = {field.name: field for field in field_set.fields}
    given: Dict[str, Mapping[str, Any]] = {}
    for entry in tool_input.get("fields") or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        if name in by_name and name not in given:
            given[name] = entry

    answers: Dict[str, Dict[str, Any]] = {}
    for field in field_set.fields:
        entry = given.get(field.name)
        quote = entry.get("quote") if entry is not None else ""
        answers[field.name] = {
            "value": coerce_value(field, entry.get("value")) if entry is not None else None,
            "quote": quote if isinstance(quote, str) else "",
        }
    return _validated(field_set, answers)


def _validated(field_set: FieldSet, answers: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``answers`` once the set's generated model accepts them, nulling any value it refuses."""
    model = model_for_field_set(field_set)
    for _ in range(len(field_set.fields) + 1):
        try:
            model.model_validate(answers)
            return answers
        except ValidationError as exc:
            refused = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
            refused &= set(answers)
            if not refused:
                break
            for name in refused:
                answers[name] = {"value": None, "quote": answers[name].get("quote") or ""}
    logger.warning("workflow checklist: a set's answer did not validate; every value is null")
    return {name: {"value": None, "quote": answer.get("quote") or ""} for name, answer in answers.items()}


# --- Provenance and the step's answer --------------------------------------------------------------


def verified_rows(
    field_set: FieldSet,
    answers: Dict[str, Dict[str, Any]],
    documents: Sequence[Optional[DocumentUnits]],
) -> List[Row]:
    """Every answer through ``source_provenance``, against the run's documents, in the set's order.

    A checklist call reads every document the run shares, so a quote may come from any of them.
    Each field is verified against each document (``rows.build_rows``, one ``verify_claims`` per
    document), and keeps the verdict of the first document, in the run's order, where its quote is
    located; a quote located in none keeps the first document's verdict (``unverified``). Nothing
    here makes a verdict: each one is ``verify_claims``'s own, and picking the document that holds
    the quote never lifts a state past what that document gives (``quote_unsupported`` stays it).
    """
    candidates = [build_rows(field_set, answers, units) for units in (documents or [None])]
    picked: List[Row] = []
    for index in range(len(field_set.fields)):
        located = next((rows[index] for rows in candidates if rows[index].verification.locator is not None), None)
        picked.append(located or candidates[0][index])
    return picked


def row_fields(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    """The rows as a ``WFCHK#`` row's ``fields``: a value not found is null, never ``""``."""
    fields = event_fields(rows)
    for item in fields:
        if item.get("value") == "":
            item["value"] = None
    return fields


@dataclass(frozen=True)
class SetResult:
    """One field set's outcome, as the step's answer renders it."""

    name: str
    title: str
    status: str  # "done" | "error"
    error: Optional[str] = None
    field_set: Optional[FieldSet] = None
    rows: Tuple[Row, ...] = ()


#: A set's ``error`` code, as the step's answer says it.
ERROR_TEXT = {
    "field_set_missing": "the field set is no longer in the workflow's skill, or could not be read",
    "unsupported_field": "the field set now holds a list field, which a checklist step can't fill",
    "no_tool_call": "the model did not record an answer, twice",
    "timeout": "the call ran out of time",
    "cut_off": "the answer reached the output limit before it was recorded",
    "model_error": "the model call failed",
}


def _cell(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


def render_set(result: SetResult) -> str:
    """One set as a Markdown table of values and states, bounded as ``document_extract``'s result is.

    Values and states only: the quotes, locators and methods are display, kept on the ``WFCHK#``
    row. Each value is cut at ``rows.MAX_VALUE_CHARS``, and a set whose values pass
    ``rows.MAX_RESULT_CHARS`` keeps its names and states alone (``model_facing_result``).
    """
    heading = f"### {_cell(result.title or result.name)}"
    if result.status != "done" or result.field_set is None:
        reason = ERROR_TEXT.get(result.error or "", "it could not be run")
        return f"{heading}\n\nNot filled in: {reason}."
    payload = model_facing_result(list(result.rows), field_set=result.field_set, artifact_id=None, schema_enforced=True)
    titles = {row.field.name: row.field.title or row.field.name for row in result.rows}
    omitted = bool(payload.get("valuesOmitted"))
    if omitted:
        lines = [heading, "", "| Field | State |", "|---|---|"]
        lines += [f"| {_cell(titles.get(f['name'], f['name']))} | {f['state']} |" for f in payload["fields"]]
        lines += ["", "(Values left out to keep this short; the step's table has them.)"]
        return "\n".join(lines)
    lines = [heading, "", "| Field | Value | State |", "|---|---|---|"]
    for item in payload["fields"]:
        value = item.get("value")
        lines.append(
            f"| {_cell(titles.get(item['name'], item['name']))} | {_cell(value) if value else 'not found'} | {item['state']} |"
        )
    return "\n".join(lines)


def render_answer(results: Sequence[SetResult]) -> str:
    """A checklist step's model-facing answer: one table per set, in the step's order."""
    return "\n\n".join(render_set(result) for result in results)


# --- The model -------------------------------------------------------------------------------------


def build_checklist_model(model_id: str, *, max_tokens: int, read_timeout: int) -> Any:
    """A dedicated ``BedrockModel`` for checklist calls: ``strict_tools`` on, no cache config.

    Its requests only ever carry :data:`TOOL_SPEC`. With no ``cache_config`` Strands places no
    cachePoint of its own; the one after the documents is the only one sent.
    """
    from botocore.config import Config as BotocoreConfig
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=model_id,
        max_tokens=max_tokens,
        strict_tools=True,
        boto_client_config=BotocoreConfig(
            read_timeout=read_timeout,
            connect_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


__all__ = [
    "CALL_ONCE",
    "ERROR_TEXT",
    "SetResult",
    "render_answer",
    "render_set",
    "row_fields",
    "verified_rows",
    "GUIDANCE_HEADING",
    "SYSTEM_PROMPT",
    "TOOL_NAME",
    "TOOL_SPEC",
    "build_checklist_model",
    "coerce_value",
    "field_list",
    "instruction",
    "read_answers",
    "tool_input_of",
    "unsupported_fields",
]
