"""The audit record itself — keys, TTL, serialization, and the diff.

The two failures worth pinning down here are silent ones: a key built one way on
write and another way on read (the console returns nothing and nobody can tell
why), and a diff that reports changes that did not happen (the role form posts
every field on every save, so a naive record claims ten edits on a description
change).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.audit.models import (
    ALL_ACTIONS,
    RETENTION_DAYS,
    TARGET_APP_ROLE,
    AuditAction,
    AuditOutcome,
    AuditRecord,
)
from apis.shared.audit.service import diff_fields


def make_record(**overrides) -> AuditRecord:
    defaults = dict(
        action=AuditAction.ROLE_UPDATED,
        actor_user_id="admin-1",
        actor_email="admin@example.com",
        target_type=TARGET_APP_ROLE,
        target_id="analyst",
    )
    defaults.update(overrides)
    return AuditRecord(**defaults)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_target_partition_groups_history_for_one_role() -> None:
    assert make_record().pk == "AUDIT#app_role#analyst"


def test_sort_key_is_timestamp_first_so_ranges_are_chronological() -> None:
    record = make_record()
    assert record.sk.startswith(record.timestamp)
    assert record.sk.endswith(record.audit_id)


def test_two_records_in_the_same_instant_stay_distinct() -> None:
    a = make_record()
    b = make_record()
    b.timestamp = a.timestamp
    assert a.sk != b.sk


def test_recent_partition_is_month_sharded() -> None:
    """A single constant partition would collect every write forever."""
    record = make_record()
    record.timestamp = "2026-07-31T03:36:49.190504+00:00"
    assert record.recent_pk == "AUDIT#2026-07"


def test_actor_partition_answers_what_did_this_admin_do() -> None:
    assert make_record().actor_pk == "ACTOR#admin-1"


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_ttl_is_retention_days_past_the_write() -> None:
    record = make_record()
    written = datetime.now(timezone.utc)
    expected = int((written + timedelta(days=RETENTION_DAYS)).timestamp())
    # A couple of seconds of slack for clock movement between the two calls.
    assert abs(record.expires_at() - expected) < 5


def test_ttl_survives_an_unparseable_timestamp() -> None:
    """Rather than raising and taking the mutation's audit write with it."""
    record = make_record()
    record.timestamp = "not-a-date"
    assert record.expires_at() > datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_item_carries_every_index_key() -> None:
    item = make_record().to_item()
    for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"):
        assert item[key], key


def test_empty_payloads_are_omitted_rather_than_stored_as_empty_maps() -> None:
    item = make_record().to_item()
    assert "before" not in item
    assert "after" not in item
    assert "reason" not in item


def test_round_trip_preserves_identity_and_payload() -> None:
    original = make_record(
        changes=["granted_admin_scopes"],
        before={"granted_admin_scopes": []},
        after={"granted_admin_scopes": ["admin.costs"]},
    )
    restored = AuditRecord.from_item(original.to_item())

    assert restored.audit_id == original.audit_id
    assert restored.timestamp == original.timestamp
    assert restored.action == original.action
    assert restored.actor_user_id == original.actor_user_id
    assert restored.before == original.before
    assert restored.after == original.after
    # And the restored record rebuilds the same keys it was stored under —
    # otherwise a read by target would miss rows written moments earlier.
    assert restored.pk == original.pk
    assert restored.sk == original.sk


def test_response_projection_is_camel_cased() -> None:
    payload = make_record().to_response()
    assert "actorUserId" in payload
    assert "actor_user_id" not in payload


# ---------------------------------------------------------------------------
# The action registry
# ---------------------------------------------------------------------------


def test_every_action_is_namespaced() -> None:
    for action in ALL_ACTIONS:
        assert action.startswith("app_role."), action


def test_no_action_exists_for_tool_or_skill_grants() -> None:
    """Those funnel through `update_role`; a second record would double-count.

    See the `AuditAction` docstring. If someone adds `app_role.tool_granted`
    they need to also stop `update_role` recording the same mutation.
    """
    assert not any("tool" in a or "skill" in a for a in ALL_ACTIONS)


# ---------------------------------------------------------------------------
# diff_fields
# ---------------------------------------------------------------------------


class Thing:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_diff_reports_only_fields_that_actually_changed() -> None:
    before = Thing(name="a", priority=0, tools=["x"])
    after = Thing(name="b", priority=0, tools=["x"])

    changed, old, new = diff_fields(before, after, ["name", "priority", "tools"])

    assert changed == ["name"]
    assert old == {"name": "a"}
    assert new == {"name": "b"}


def test_diff_ignores_pure_list_reordering() -> None:
    """Grant lists are normalized/sorted on write — a reorder is not an edit."""
    before = Thing(tools=["a", "b"])
    after = Thing(tools=["b", "a"])

    changed, _, _ = diff_fields(before, after, ["tools"])

    assert changed == []


def test_diff_still_catches_a_real_list_change() -> None:
    before = Thing(tools=["a"])
    after = Thing(tools=["a", "b"])

    changed, old, new = diff_fields(before, after, ["tools"])

    assert changed == ["tools"]
    assert old == {"tools": ["a"]}
    assert new == {"tools": ["a", "b"]}


def test_diff_of_identical_objects_is_empty() -> None:
    before = Thing(name="a", tools=["x"])
    after = Thing(name="a", tools=["x"])

    changed, old, new = diff_fields(before, after, ["name", "tools"])

    assert (changed, old, new) == ([], {}, {})


@pytest.mark.parametrize("outcome", [AuditOutcome.ALLOWED, AuditOutcome.DENIED])
def test_outcome_round_trips(outcome: str) -> None:
    record = make_record(outcome=outcome, reason="nope")
    assert AuditRecord.from_item(record.to_item()).outcome == outcome
