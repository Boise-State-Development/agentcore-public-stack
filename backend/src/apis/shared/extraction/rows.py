"""Turning a typed answer into rows that carry their own verdict.

``docs/specs/structured-extraction.md`` §7, §5.1, D4, D7, D8, D9. Every
extracted field is a claim in the source-provenance sense, and that module
answers it. **We add nothing to it.**

WHAT THIS MODULE MAY NOT DO
---------------------------
``source_provenance``'s own decisions constrain this file, and the architecture
test enforces the shape of it:

* **D1 — no inventing a verdict.** There is no public function taking the two
  underlying booleans, and we do not want one. Nothing here writes a state,
  constructs a ``SourceVerification``, or branches toward a stronger state than
  it was handed. States arrive from ``verify_claims`` and are passed on.
* **D9 — nothing inherits the strongest badge.** A field returned without a
  quote is ``unverified``/``no_quote``. A null or not-found value is
  ``unassessed``/``empty_value``. An ``enum`` or ``boolean`` answering a
  judgement about the prose is ``unassessed``/``enum_field``. None of those is
  a blemish to be smoothed over, and twenty ``supported`` rows in a row should
  make a reviewer suspicious rather than comfortable.

ONE IMPROVEMENT OVER ``verify_sources``, AND IT IS FREE (D7)
------------------------------------------------------------
That tool takes ``from_fixed_set`` from the *model*, which is the model
self-certifying that the value question does not apply to its own answer. We do
not have to trust that: the field set declares ``type: enum``, so
``enum_field`` is derived from the schema an office authored. A model cannot
escape the value check here by claiming its answer was a judgement call.

THE WEAKNESS IS NOT PAPERED OVER
--------------------------------
The value check asks whether the value appears *somewhere* in the passage, not
whether the passage *assigns* it to this field — so a ``direct_costs`` value is
``supported`` by a quote labelling that same figure indirect. In a prose answer
that is a caveat. In a twenty-row table of ``supported`` badges it is a
stronger invitation to over-read, because the table's structure implies the
assignment was checked. So the artifact states the claim exactly once, in the
source module's own terms, and no column header ever reads "verified".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from apis.shared.files.source_provenance import (
    Claim,
    DocumentUnits,
    SourceVerification,
    render_locator,
    verify_claims,
)

from .field_sets import Field, FieldSet

#: §5.1. Per-value cap on what reaches the model. A longer value is truncated
#: with an ellipsis; the full text stays in the artifact and the stored row.
MAX_VALUE_CHARS = 200

#: §5.1. Cap on the whole model-facing result. Over it, the result drops to
#: names and states with a line saying to read the table — the names and states
#: are what the model needs to act, and the values are on screen either way.
MAX_RESULT_CHARS = 2000

#: Clamp on a quote carried on the event and the row. The store clamps again.
MAX_QUOTE_CHARS = 1000


@dataclass(frozen=True)
class Row:
    """One extracted field: what was found, and what the check made of it."""

    field: Field
    value: Any
    quote: Optional[str]
    verification: SourceVerification

    @property
    def state(self) -> str:
        return self.verification.state

    @property
    def locator_text(self) -> Optional[str]:
        """``"p. 12"`` / ``"¶ 34"`` / ``"line 88"``, hedged when estimated.

        Rendered by the source module, never formatted here: a locator that
        looked measured when it was estimated is the defect that feature exists
        to prevent, and a second renderer is a second chance to drop the hedge.
        """
        return render_locator(self.verification.locator)


def _display_value(value: Any) -> str:
    """One value as a single line of text, for the table and the workbook."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item is not None)
    return str(value)


def build_claims(field_set: FieldSet, extracted: Dict[str, Dict[str, Any]]) -> List[Claim]:
    """One :class:`Claim` per field, in the field set's authored order.

    ``enum_field`` comes from the field's declared type (D7). ``value`` is
    passed through as the model typed it — not stringified — because the value
    check parses numbers, currency and dates itself and a premature ``str()``
    would hand it text to re-parse.
    """
    claims: List[Claim] = []
    for field in field_set.fields:
        answer = extracted.get(field.name) or {}
        quote = answer.get("quote")
        claims.append(
            Claim(
                claim_id=field.name,
                value=answer.get("value"),
                quote=quote[:MAX_QUOTE_CHARS] if isinstance(quote, str) else None,
                enum_field=field.is_judgement,
            )
        )
    return claims


def build_rows(
    field_set: FieldSet,
    extracted: Dict[str, Dict[str, Any]],
    document: Optional[DocumentUnits],
) -> List[Row]:
    """Verify every extracted value and pair it with its verdict.

    One ``verify_claims`` call for the whole set, because ``DocumentUnits``
    normalizes on construction — so forty fields against one document pay
    normalization once rather than forty times.
    """
    claims = build_claims(field_set, extracted)
    verdicts = verify_claims(claims, document)
    return [
        Row(
            field=field,
            value=claim.value,
            quote=claim.quote,
            verification=verdict,
        )
        for field, claim, verdict in zip(field_set.fields, claims, verdicts)
    ]


def model_facing_result(
    rows: Sequence[Row],
    *,
    field_set: FieldSet,
    artifact_id: Optional[str],
    schema_enforced: bool,
) -> Dict[str, Any]:
    """What goes back into the conversation (§5.1, D8).

    **The values go in, and that is consistent with the cost tenet rather than
    an exception to it.** The source-provenance rule is that *display* metadata
    must not enter the Converse payload, because it is paid at model rates on
    every later turn forever. Extraction values are not display metadata — they
    are the answer the user asked for, and "so what was the budget cap?" is the
    normal next question. A model that cannot see the values has to re-run a
    document-sized call to answer it.

    What stays out: the quote, the locator, ``precision``, ``method``, the
    rendered table and the workbook bytes. Those travel on the SSE event and
    the stored row.
    """
    fields: List[Dict[str, Any]] = []
    for row in rows:
        text = _display_value(row.value)
        if len(text) > MAX_VALUE_CHARS:
            text = text[: MAX_VALUE_CHARS - 1].rstrip() + "…"
        fields.append(
            {
                "name": row.field.name,
                "value": text or None,
                "state": row.state,
            }
        )

    payload: Dict[str, Any] = {
        "fieldSet": field_set.name,
        "fields": fields,
    }
    if artifact_id:
        payload["artifactId"] = artifact_id
    if not schema_enforced:
        payload["schemaEnforced"] = False

    # Second line on §5.1's bound: if the values alone are still too big for a
    # payload that is paid on every later turn, drop to names and states. The
    # states are what the model needs in order to act; the values are on screen
    # in the table either way.
    if _rough_size(payload) > MAX_RESULT_CHARS:
        payload["fields"] = [
            {"name": row.field.name, "state": row.state} for row in rows
        ]
        payload["valuesOmitted"] = True
    return payload


def _rough_size(payload: Dict[str, Any]) -> int:
    import json

    return len(json.dumps(payload, ensure_ascii=False))


def event_fields(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    """The rich per-field payload for the SSE event and the stored row (§9).

    Everything the model is deliberately not told. ``precision`` always travels
    inside the locator, because a reader who cannot tell an estimated position
    from a measured one has been given false confidence.
    """
    return [
        {
            "name": row.field.name,
            "title": row.field.title,
            "type": row.field.type,
            "value": _display_value(row.value),
            "state": row.state,
            "method": row.verification.method,
            "quote": (row.quote or "")[:MAX_QUOTE_CHARS] or None,
            "locator": (
                row.verification.locator.to_payload()
                if row.verification.locator
                else None
            ),
        }
        for row in rows
    ]


def state_counts(rows: Sequence[Row]) -> Dict[str, int]:
    """Per-state counts, for the content-free EMF record (§14).

    The split is the honesty readout: a run whose every row is ``supported``
    means the check is not working, not that the extraction was perfect. Built
    from the states the verdicts carry — never from a comparison this module
    makes itself.
    """
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


__all__ = [
    "MAX_QUOTE_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_VALUE_CHARS",
    "Row",
    "build_claims",
    "build_rows",
    "event_fields",
    "model_facing_result",
    "state_counts",
]
