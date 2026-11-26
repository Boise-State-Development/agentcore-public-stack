"""The prewarm-prepare experiment's arm: a pure function of the conversation id."""

import uuid

import pytest

from apis.shared.feature_flags import session_prewarm_prepare_holdout_percent
from apis.shared.sessions.prepare_arm import ARM_HOLDOUT, ARM_TREATED, prepare_arm


def test_it_is_deterministic() -> None:
    assert {prepare_arm("conv-1", 50) for _ in range(20)} in ({ARM_TREATED}, {ARM_HOLDOUT})


def test_zero_is_all_treated_and_100_all_holdout() -> None:
    ids = [str(uuid.uuid4()) for _ in range(500)]
    assert {prepare_arm(i, 0) for i in ids} == {ARM_TREATED}
    assert {prepare_arm(i, 100) for i in ids} == {ARM_HOLDOUT}


def test_fifty_percent_splits_roughly_in_half_and_varies_by_id() -> None:
    ids = [str(uuid.uuid4()) for _ in range(10_000)]
    held = sum(prepare_arm(i, 50) == ARM_HOLDOUT for i in ids) / len(ids)
    assert 0.47 <= held <= 0.53


def test_a_larger_percent_only_adds_holdouts() -> None:
    ids = [str(uuid.uuid4()) for _ in range(1000)]
    for i in ids:
        if prepare_arm(i, 20) == ARM_HOLDOUT:
            assert prepare_arm(i, 40) == ARM_HOLDOUT


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), ("", 0), ("abc", 0), ("1.5", 0), ("0", 0), ("25", 25), (" 40 ", 40), ("100", 100), ("250", 100), ("-5", 0)],
)
def test_holdout_percent_reader_clamps(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT", raising=False)
    else:
        monkeypatch.setenv("SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT", value)
    assert session_prewarm_prepare_holdout_percent() == expected
