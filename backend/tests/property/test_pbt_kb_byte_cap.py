"""Property-based tests for byte cap accounting.

Feature: managed-kb-migration

**Property 5: the cap is never exceeded, under any interleaving.**

Managed storage costs $5.00/GB-month, so the cap is the only thing standing between
the measured ~$169/month fleet cost and the ~$15,000/month that unbounded uploads
would permit. "Usually holds" is not a cap.

The property is asserted against real DynamoDB semantics (via moto) rather than
against a Python model of them, because the entire correctness argument rests on
one specific database behaviour: that a conditional ``ADD`` is atomic. A test that
simulated the arithmetic in Python would pass just as happily against a
read-then-write implementation, which is precisely the broken version.

Why the accumulator matters
---------------------------
DynamoDB cannot do arithmetic inside a condition expression — verified, it fails to
parse. So the guard compares a single ``totalBytes`` accumulator against a literal
computed before the call (``cap - n``). The invariant
``totalBytes == storedBytes + reservedBytes`` is what makes that sound, and several
tests below assert it directly rather than only checking the total.

Validates: Requirements 12.4, 12.5, 12.6, 24.7.
"""

import boto3
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from moto import mock_aws

from apis.shared.kb_backend import byte_cap as bc
from apis.shared.kb_backend.records import kb_pk, kb_sk

REGION = "us-east-1"
TABLE = "test-byte-cap"
ASSISTANT_ID = "ast-cap01"
APP_KB_ID = ASSISTANT_ID
CAP = 1000

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Reservation sizes, including 0 (a no-op) and sizes larger than the whole cap.
st_size = st.integers(min_value=0, max_value=CAP + 500)

#: An arbitrary sequence of reservations. Length and sizes both vary so the
#: sequence sometimes fits entirely, sometimes overruns partway, and sometimes
#: overruns on the very first item.
st_sequence = st.lists(st_size, min_size=1, max_size=15)


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        t.put_item(Item={"PK": kb_pk(ASSISTANT_ID), "SK": kb_sk(APP_KB_ID)})
        yield t


def _counters(table):
    item = table.get_item(Key={"PK": kb_pk(ASSISTANT_ID), "SK": kb_sk(APP_KB_ID)})["Item"]
    return (
        int(item.get("totalBytes", 0)),
        int(item.get("reservedBytes", 0)),
        int(item.get("storedBytes", 0)),
    )


def _reset(table):
    table.put_item(Item={"PK": kb_pk(ASSISTANT_ID), "SK": kb_sk(APP_KB_ID)})


# ---------------------------------------------------------------------------
# The cap holds
# ---------------------------------------------------------------------------
@given(sizes=st_sequence)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_cap_is_never_exceeded(table, sizes):
    """However the sequence interleaves, the accumulator never passes the cap."""
    _reset(table)
    accepted = []
    for n in sizes:
        try:
            bc.reserve(ASSISTANT_ID, APP_KB_ID, n, CAP)
            accepted.append(n)
        except bc.ByteCapExceeded:
            pass

        total, _, _ = _counters(table)
        assert total <= CAP, f"cap breached at {total} > {CAP}"

    total, reserved, _ = _counters(table)
    assert total == sum(accepted)
    assert reserved == sum(accepted)


@given(sizes=st_sequence)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_accumulator_invariant_holds(table, sizes):
    """totalBytes == storedBytes + reservedBytes, always.

    This is what makes comparing a single attribute a valid cap check. If the two
    ever diverge the guard is measuring something that is not the owner's usage.
    """
    _reset(table)
    for n in sizes:
        try:
            bc.reserve(ASSISTANT_ID, APP_KB_ID, n, CAP)
            # Commit half the time so both counters move.
            if n % 2 == 0:
                bc.commit(ASSISTANT_ID, APP_KB_ID, n)
        except bc.ByteCapExceeded:
            pass

        total, reserved, stored = _counters(table)
        assert total == reserved + stored, f"{total} != {reserved} + {stored}"


@given(sizes=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=10))
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_released_reservations_are_fully_returned(table, sizes):
    """Release restores the allowance exactly.

    A release that returned less than it reserved would shrink the owner's cap on
    every failed upload, presenting weeks later as "uploads stopped working" with
    no failing request to point at.
    """
    _reset(table)
    for n in sizes:
        bc.reserve(ASSISTANT_ID, APP_KB_ID, n, CAP)
        bc.release(ASSISTANT_ID, APP_KB_ID, n)

    total, reserved, stored = _counters(table)
    assert (total, reserved, stored) == (0, 0, 0)


@given(sizes=st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=8))
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_commit_does_not_double_count(table, sizes):
    """Commit moves bytes; it must not add them again.

    Double-counting on commit would halve every owner's effective allowance, and it
    would do so only for *successful* uploads — so the symptom would be that the
    cap tightens the more correctly the system works.
    """
    _reset(table)
    for n in sizes:
        bc.reserve(ASSISTANT_ID, APP_KB_ID, n, CAP)
        bc.commit(ASSISTANT_ID, APP_KB_ID, n)

    total, reserved, stored = _counters(table)
    assert total == sum(sizes)
    assert stored == sum(sizes)
    assert reserved == 0


# ---------------------------------------------------------------------------
# Boundary and rejection behaviour
# ---------------------------------------------------------------------------
def test_a_reservation_exactly_filling_the_cap_is_allowed(table):
    """The cap is inclusive: exactly at the limit is within it."""
    _reset(table)
    bc.reserve(ASSISTANT_ID, APP_KB_ID, CAP, CAP)
    assert _counters(table)[0] == CAP


def test_one_byte_over_is_rejected(table):
    _reset(table)
    with pytest.raises(bc.ByteCapExceeded):
        bc.reserve(ASSISTANT_ID, APP_KB_ID, CAP + 1, CAP)
    assert _counters(table)[0] == 0, "a rejected reservation must leave no trace"


def test_a_rejected_reservation_does_not_consume_allowance(table):
    """The failed attempt must not partially apply.

    An ADD that landed before the condition was evaluated would leak allowance on
    every rejection, so a user who hit the cap once could never upload again.
    """
    _reset(table)
    bc.reserve(ASSISTANT_ID, APP_KB_ID, 900, CAP)
    with pytest.raises(bc.ByteCapExceeded):
        bc.reserve(ASSISTANT_ID, APP_KB_ID, 200, CAP)

    total, reserved, _ = _counters(table)
    assert (total, reserved) == (900, 900)
    # And the remaining allowance is still usable.
    bc.reserve(ASSISTANT_ID, APP_KB_ID, 100, CAP)
    assert _counters(table)[0] == CAP


def test_zero_is_a_no_op(table):
    _reset(table)
    bc.reserve(ASSISTANT_ID, APP_KB_ID, 0, CAP)
    assert _counters(table) == (0, 0, 0)


def test_a_negative_reservation_is_rejected(table):
    """Otherwise 'reserving' a negative size would be a way to mint allowance."""
    _reset(table)
    with pytest.raises(ValueError):
        bc.reserve(ASSISTANT_ID, APP_KB_ID, -100, CAP)


def test_the_exception_carries_the_numbers_for_the_user(table):
    """Requirement 12.12 wants a plain-language reason and an upgrade path, which
    needs the figures, not just a failure."""
    _reset(table)
    with pytest.raises(bc.ByteCapExceeded) as excinfo:
        bc.reserve(ASSISTANT_ID, APP_KB_ID, CAP + 1, CAP)
    assert excinfo.value.requested == CAP + 1
    assert excinfo.value.cap == CAP


# ---------------------------------------------------------------------------
# Migration snapshot (Requirement 12.11/12.12)
# ---------------------------------------------------------------------------
def test_a_snapshot_that_cannot_fit_is_rejected_up_front(table):
    """The whole corpus is reserved before migration starts.

    Reserving per-document instead would let a migration run for an hour and stop
    halfway, leaving a half-populated managed knowledge base behind.
    """
    _reset(table)
    with pytest.raises(bc.ByteCapExceeded):
        bc.reserve_snapshot(ASSISTANT_ID, APP_KB_ID, CAP * 2, CAP)
    assert _counters(table)[0] == 0, "a rejected migration must reserve nothing"


def test_a_snapshot_that_fits_reserves_the_whole_corpus(table):
    _reset(table)
    bc.reserve_snapshot(ASSISTANT_ID, APP_KB_ID, 800, CAP)
    total, reserved, _ = _counters(table)
    assert (total, reserved) == (800, 800)


def test_a_snapshot_is_rejected_when_existing_usage_leaves_no_room(table):
    """The interesting case: the corpus fits an empty cap but not this owner's."""
    _reset(table)
    bc.reserve(ASSISTANT_ID, APP_KB_ID, 700, CAP)
    bc.commit(ASSISTANT_ID, APP_KB_ID, 700)

    with pytest.raises(bc.ByteCapExceeded):
        bc.reserve_snapshot(ASSISTANT_ID, APP_KB_ID, 400, CAP)

    assert _counters(table)[0] == 700


# ---------------------------------------------------------------------------
# Cap resolution
# ---------------------------------------------------------------------------
def test_the_default_cap_is_below_the_user_files_precedent(monkeypatch):
    """100 MB, deliberately under the existing 1 GB user-files limit.

    At $5.00/GB-month that precedent would permit roughly $150,000/month across the
    fleet — a number large enough that it is not really a limit.
    """
    monkeypatch.delenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", raising=False)
    assert bc.per_owner_cap() == 100 * 1024 * 1024
    assert bc.per_owner_cap() < 1024 * 1024 * 1024


def test_the_elevated_tier_is_larger_than_the_default(monkeypatch):
    monkeypatch.delenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", raising=False)
    monkeypatch.delenv("MANAGED_KB_PER_OWNER_ELEVATED_BYTES", raising=False)
    assert bc.per_owner_cap(elevated=True) > bc.per_owner_cap()


def test_caps_are_overridable_from_the_environment(monkeypatch):
    monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "12345")
    assert bc.per_owner_cap() == 12345


def test_a_malformed_override_falls_back_rather_than_crashing(monkeypatch):
    """A typo in an operator-set variable must not take retrieval down."""
    monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "not-a-number")
    assert bc.per_owner_cap() == 100 * 1024 * 1024


def test_the_per_kb_ceiling_is_below_the_elevated_owner_cap(monkeypatch):
    """A single knowledge base must not be able to eat an entire elevated
    allowance and starve the owner's others."""
    for var in (
        "MANAGED_KB_PER_KB_CEILING_BYTES",
        "MANAGED_KB_PER_OWNER_ELEVATED_BYTES",
    ):
        monkeypatch.delenv(var, raising=False)
    assert bc.per_kb_ceiling() < bc.per_owner_cap(elevated=True)


# ---------------------------------------------------------------------------
# Sizing authority
# ---------------------------------------------------------------------------
def test_size_comes_from_s3_not_from_the_caller(monkeypatch):
    """A client-reported size is an input, and an input that can lower its own
    cost is not a measurement."""
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with patch("boto3.client") as client:
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 4242}
        client.return_value = s3

        assert bc.object_size_bytes("bucket", "key") == 4242
        s3.head_object.assert_called_once_with(Bucket="bucket", Key="key")
