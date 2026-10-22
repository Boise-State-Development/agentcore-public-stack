"""The admin cost surface is content-free by construction — proven, not promised.

Three things are pinned here, in cost-of-getting-it-wrong order:

1. **No response model on the admin cost surface has a content-bearing field**,
   with exactly one named exemption (`TopSessionCost.title`, kept by decision).
   A new field named `summary`, `content`, `displayText` … fails this test until
   someone either renames it or adds it to the exemption set on purpose.
2. **Every storage projection is a subset of the allowlist**, except the one
   path a reader may request-but-never-return (`compaction.summary`, measured
   into a length).
3. The helpers behave: reserved words are aliased, nested and list content is
   stripped, the summary is measured then dropped.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, List, Set, Tuple

import pytest
from pydantic import BaseModel

from apis.app_api.admin.costs import models as cost_models
from apis.shared.observability import content_policy as cp

#: The single decided exemption on the admin cost surface. The "most expensive
#: conversations" table shows the LLM-generated session title; the per-user
#: list and the session profile deliberately do not inherit it. Widening this
#: set is a product decision, not a refactor.
EXEMPT: Set[Tuple[str, str]] = {("TopSessionCost", "title")}


def _unwrap(annotation: Any) -> List[type]:
    """Every pydantic model class reachable from a field annotation."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return [annotation] if inspect.isclass(annotation) and issubclass(annotation, BaseModel) else []
    found: List[type] = []
    for arg in typing.get_args(annotation):
        found.extend(_unwrap(arg))
    return found


def _declared_fields(model: type, seen: Set[type] | None = None) -> List[Tuple[str, str]]:
    """``(owning model name, wire alias)`` for every field reachable from
    ``model``, attributing each field to the model that declares it — so a
    nested model's field is reported once, under its own name, however many
    parents embed it."""
    seen = seen or set()
    if model in seen:
        return []
    seen = seen | {model}
    fields: List[Tuple[str, str]] = []
    for name, field in model.model_fields.items():
        alias = field.alias or name
        fields.append((model.__name__, alias))
        for nested in _unwrap(field.annotation):
            fields.extend(_declared_fields(nested, seen))
    return fields


def _all_models() -> List[type]:
    return [
        obj for _, obj in inspect.getmembers(cost_models, inspect.isclass)
        if issubclass(obj, BaseModel) and obj is not BaseModel
        and obj.__module__ == cost_models.__name__
    ]


def test_response_models_are_content_free_except_the_one_decided_exemption():
    offenders: Set[Tuple[str, str]] = set()
    for model in _all_models():
        for owner, alias in _declared_fields(model):
            if cp.is_content_bearing(alias):
                offenders.add((owner, alias))
    assert offenders == EXEMPT, (
        f"content-bearing field(s) on the admin cost surface: {sorted(offenders - EXEMPT)}. "
        "Rename the field, or — if the product decision is to show content — add it to "
        "EXEMPT with a reason."
    )


def test_the_exemption_still_exists_so_it_cannot_rot_silently():
    # If someone removes `title` from TopSessionCost, the exemption set must be
    # trimmed too — otherwise a future re-addition would pass unnoticed.
    assert "title" in cost_models.TopSessionCost.model_fields


def test_every_projection_is_allowlisted_or_measured_only():
    for name, projection in cp.ALL_PROJECTIONS.items():
        for path in projection:
            if cp.is_content_bearing(path):
                assert path in cp.MEASURED_ONLY, (
                    f"{name} requests content-bearing path {path!r} that is not measured-only"
                )


def test_a_denied_prefix_denies_everything_beneath_it():
    assert cp.is_content_bearing("pausedTurn.modelId")
    assert cp.is_content_bearing("compaction.summary")
    assert not cp.is_content_bearing("compaction.checkpoint")
    assert not cp.is_content_bearing("preferences.lastModel")
    assert cp.is_content_bearing("preferences.customPromptText")


def test_a_denied_path_is_caught_wherever_a_row_is_embedded():
    # Rows travel under arbitrary keys (`calls[]`, `sessions[]`); the matcher
    # must find the row-relative denylist entry inside the longer path.
    assert cp.is_content_bearing("calls.citations.text")
    assert cp.is_content_bearing("sessions.title")
    assert cp.is_content_bearing("session.compaction.summary")
    assert not cp.is_content_bearing("session.compaction.summaryChars")
    # Segment matching is exact — a name that merely *contains* a denied word is fine.
    assert not cp.is_content_bearing("contextWindow")
    assert not cp.is_content_bearing("evidence.summaryApproxTokens")


def test_build_projection_aliases_every_segment_and_shares_them():
    expr, names = cp.build_projection(("status", "preferences.lastModel", "preferences.enabledTools"))
    # No bare reserved word survives in the expression.
    assert "status" not in expr and "preferences" not in expr
    assert set(names.values()) == {"status", "preferences", "lastModel", "enabledTools"}
    # `preferences` appears once in the names map even though two paths use it.
    assert list(names.values()).count("preferences") == 1
    assert expr.count(".") == 2


def test_strip_content_removes_nested_and_list_content():
    item = {
        "sessionId": "s1",
        "title": "SECRET",
        "compaction": {"checkpoint": 3, "summary": "SECRET"},
        "calls": [{"cost": 1.0, "citations": [{"text": "SECRET"}]}],
        "preferences": {"lastModel": "m", "customPromptText": "SECRET"},
    }
    out = cp.strip_content(item)
    assert cp.content_bearing_paths(out) == []
    assert out == {
        "sessionId": "s1",
        "compaction": {"checkpoint": 3},
        "calls": [{"cost": 1.0}],
        "preferences": {"lastModel": "m"},
    }
    # The input is untouched — strip returns a copy.
    assert item["title"] == "SECRET"


def test_measure_compaction_summary_replaces_the_string_with_its_length():
    item = {"compaction": {"checkpoint": 1, "summary": "x" * 1234}}
    cp.measure_compaction_summary(item)
    assert item["compaction"] == {"checkpoint": 1, "summaryChars": 1234}


def test_measure_compaction_summary_distinguishes_missing_from_empty():
    assert "summaryChars" not in cp.measure_compaction_summary({"compaction": {}})["compaction"]
    assert cp.measure_compaction_summary({"compaction": {"summary": ""}})["compaction"] == {"summaryChars": 0}
    assert cp.measure_compaction_summary({}) == {}


def test_content_bearing_paths_reports_list_members_without_indices():
    rows = [{"cost": 1}, {"citations": [{"text": "x"}], "displayText": "y"}]
    assert sorted(cp.content_bearing_paths(rows)) == ["citations", "displayText"]


def test_allowlisted_keys_returns_direct_children_only():
    keys = cp.allowlisted_keys(cp.SESSION_ROW_PROJECTION, "preferences")
    assert keys == {"lastModel", "enabledTools", "assistantId", "agentType"}
    assert "customPromptText" not in keys


@pytest.mark.parametrize("model", _all_models(), ids=lambda m: m.__name__)
def test_each_model_serializes_by_alias_without_content(model):
    # Belt and braces for the walker: instantiate nothing, just confirm every
    # model opts into alias population so the wire names are the ones checked.
    assert model.model_config.get("populate_by_name") is True
