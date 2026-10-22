"""Property-based tests for engine resolution by absence.

Feature: managed-kb-migration

**Property 1: absence means legacy.**

This is the invariant the whole migration rests on. Every knowledge base that
existed before this feature carries no ``retrievalEngine`` attribute, and must
resolve to the legacy backend on that basis alone. Two consequences follow, and
both are why this file exists:

* **No backfill.** 1,692 ``DOC#`` records and their knowledge bases are already
  correct without being touched. A migration that had to stamp a value on each
  one would be a data migration in its own right, with its own failure modes.
* **Rollback is a pointer flip.** Rolling back ``REMOVE``s the attribute,
  restoring the original shape exactly. A rolled-back record is
  indistinguishable from one that never migrated.

Both consequences evaporate the moment any code path writes the literal
``"s3vectors"`` onto a record that did not already carry it. That write would
look harmless, pass a naive test, and convert every future rollback into a
rewrite. The second half of this file exists to make that specific mistake fail
loudly.

Validates: Requirements 1.6, 1.7, 6.6.
"""

import json
from typing import Any, Dict, List

import pytest
from hypothesis import given, settings, strategies as st

from apis.shared.kb_backend import records as r

# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

st_attribute_name = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=24,
)

st_attribute_value = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-1000, max_value=10**9),
    st.booleans(),
    st.none(),
    st.lists(st.text(max_size=10), max_size=4),
    st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=3),
)

#: An arbitrary stored item that carries no opinion about its engine. Extra keys
#: are deliberately unconstrained: real records accumulate attributes over time
#: and resolution must not depend on which ones happen to be present.
st_item_without_engine = st.dictionaries(
    st_attribute_name, st_attribute_value, max_size=12
).map(lambda d: {k: v for k, v in d.items() if k != "retrievalEngine"})

#: Anything that is not the one value we accept. Includes the legacy literal
#: itself: even if some historical record somehow carried "s3vectors", it must
#: resolve to legacy, which it does — but it must never be *written*.
st_non_managed_engine = st.one_of(
    st.just(r.ENGINE_LEGACY),
    st.just(""),
    st.just("Managed"),
    st.just("MANAGED"),
    st.just("managed "),
    st.text(max_size=20).filter(lambda s: s != r.ENGINE_MANAGED),
)


# ---------------------------------------------------------------------------
# Property 1: absence means legacy
# ---------------------------------------------------------------------------
@given(item=st_item_without_engine)
@settings(max_examples=200)
def test_any_record_without_the_attribute_resolves_to_legacy(item):
    """No matter what else the record contains, a missing engine means legacy."""
    assert "retrievalEngine" not in item
    assert r.resolve_engine(item) == r.ENGINE_LEGACY


@given(item=st_item_without_engine, engine=st_non_managed_engine)
@settings(max_examples=200)
def test_only_the_exact_managed_literal_selects_the_managed_backend(item, engine):
    """Resolution is exact-match, so a typo or casing slip fails safe.

    Failing safe matters asymmetrically here: resolving to legacy when it should
    be managed serves slightly worse answers, while resolving to managed when the
    record is not really migrated queries a knowledge base that may not exist.
    """
    item["retrievalEngine"] = engine
    assert r.resolve_engine(item) == r.ENGINE_LEGACY


@given(item=st_item_without_engine)
@settings(max_examples=100)
def test_the_managed_literal_selects_managed(item):
    """The positive case, so the tests above cannot pass by always returning legacy."""
    item["retrievalEngine"] = r.ENGINE_MANAGED
    assert r.resolve_engine(item) == r.ENGINE_MANAGED


@pytest.mark.parametrize("empty", [None, {}])
def test_a_missing_record_resolves_to_legacy(empty):
    """Absence of the whole record is an answer too, not an error.

    A knowledge base with no KB_Record is every knowledge base today.
    """
    assert r.resolve_engine(empty) == r.ENGINE_LEGACY


@given(item=st_item_without_engine)
@settings(max_examples=100)
def test_resolution_does_not_mutate_the_item(item):
    """Resolution is a read. A resolver that defaulted the attribute *in place*
    would silently create the backfill this design exists to avoid."""
    before = json.dumps(item, sort_keys=True, default=str)
    r.resolve_engine(item)
    assert json.dumps(item, sort_keys=True, default=str) == before


# ---------------------------------------------------------------------------
# Property 1, second half: the legacy literal is never written
# ---------------------------------------------------------------------------
class _RecordingTable:
    """Captures write payloads instead of performing them.

    Used rather than moto because the assertion here is about what the module
    *sends*, not about what DynamoDB does with it — and because it lets a single
    test observe every transition without needing each one's preconditions to
    hold.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        return {}

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return {}

    def serialized(self) -> str:
        return json.dumps(self.calls, sort_keys=True, default=str)


@pytest.fixture()
def recorder(monkeypatch):
    table = _RecordingTable()
    monkeypatch.setattr(r, "_table", lambda: table)
    return table


def _drive_every_write(table_unused) -> None:
    """Invoke every write path in the module once."""
    r.create_provisioning(
        "ast-1", r.KbRecord(app_kb_id="ast-1", owner_user_id="opaque-owner")
    )
    r.attach_aws_ids("ast-1", "ast-1", "kb-1", "ds-1", "2026-08-24T12:00:00Z")
    r.promote_engine("ast-1", "ast-1", 0, "2026-08-24T12:00:00Z")
    r.rollback_engine("ast-1", "ast-1", "2026-08-24T12:00:00Z")
    r.acquire_lease("ast-1", "ast-1", "2026-08-24T13:00:00Z", "2026-08-24T12:00:00Z")
    for state in (r.SHADOW, r.VERIFY, r.PROMOTE):
        r.set_migration_state("ast-1", "ast-1", state, 0, due_at="2026-08-24T12:00:00Z")
    for state in (r.RETAIN, r.MIGRATION_FAILED):
        r.set_migration_state("ast-1", "ast-1", state, 0, error="a reason")


def test_no_write_path_ever_persists_the_legacy_literal(recorder):
    """The load-bearing negative. Every write in the module, inspected.

    If this fails, someone has made legacy an explicitly stored value. The
    feature would still appear to work, and the next rollback would stop being a
    pointer flip.
    """
    _drive_every_write(recorder)
    assert recorder.calls, "no writes captured; the fixture is not wired"

    payload = recorder.serialized()
    assert r.ENGINE_LEGACY not in payload, (
        f"a write path persists the legacy literal {r.ENGINE_LEGACY!r}; "
        "absence must remain the only representation of legacy"
    )


def test_rollback_removes_the_attribute_rather_than_setting_it(recorder):
    """Rollback must restore the original shape, not write a value."""
    r.rollback_engine("ast-1", "ast-1", "2026-08-24T12:00:00Z")
    expression = recorder.calls[0]["UpdateExpression"]
    assert "REMOVE retrievalEngine" in expression
    assert "retrievalEngine = " not in expression


def test_the_only_engine_value_ever_written_is_managed(recorder):
    """Complements the negative test: promotion writes exactly one engine value."""
    r.promote_engine("ast-1", "ast-1", 0, "2026-08-24T12:00:00Z")
    values = recorder.calls[0]["ExpressionAttributeValues"]
    engine_values = [v for v in values.values() if v in (r.ENGINE_MANAGED, r.ENGINE_LEGACY)]
    assert engine_values == [r.ENGINE_MANAGED]
