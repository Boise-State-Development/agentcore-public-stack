"""Tests for ClassroomBurstShape.

The shape encodes a claim about campus traffic: the risk is not 1300 users
arriving gradually but 300 arriving inside thirty seconds. These tests pin the
properties that make it a burst rather than a ramp, because a shape that
silently flattens would produce a reassuring result for the wrong scenario.
"""

from __future__ import annotations

import pytest

from shapes import ClassroomBurstShape

BURST_ENV = (
    "AGENTCORE_LOAD_BURST_BASELINE_USERS",
    "AGENTCORE_LOAD_BURST_USERS",
    "AGENTCORE_LOAD_BURST_SECONDS",
    "AGENTCORE_LOAD_BURST_HOLD_SECONDS",
    "AGENTCORE_LOAD_BURST_DRAIN_SECONDS",
    "AGENTCORE_LOAD_BURST_COUNT",
    "AGENTCORE_LOAD_BURST_WARMUP_SECONDS",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BURST_ENV:
        monkeypatch.delenv(name, raising=False)


def _at(shape: ClassroomBurstShape, seconds: int):
    shape.get_run_time = lambda: seconds  # type: ignore[method-assign]
    return shape.tick()


class TestTimeline:
    def test_starts_at_baseline_so_the_spike_is_attributable(self) -> None:
        # Without a warm-up the cold ALB and empty prompt cache would be
        # charged to the burst.
        shape = ClassroomBurstShape()
        users, _ = _at(shape, 0)
        assert users == shape.baseline_users

    def test_spike_reaches_full_burst_immediately_after_warmup(self) -> None:
        shape = ClassroomBurstShape()
        assert _at(shape, shape.warmup_seconds)[0] == shape.burst_users

    def test_holds_the_burst_then_drains(self) -> None:
        shape = ClassroomBurstShape()
        mid_hold = shape.warmup_seconds + shape.burst_seconds + 1
        assert _at(shape, mid_hold)[0] == shape.burst_users

        draining = (
            shape.warmup_seconds + shape.burst_seconds + shape.hold_seconds + 1
        )
        assert _at(shape, draining)[0] == shape.baseline_users

    def test_second_burst_fires_on_warm_infrastructure(self) -> None:
        # The whole reason for two bursts: surviving a class that follows
        # another class is a different claim from surviving the first class.
        shape = ClassroomBurstShape()
        second = shape.warmup_seconds + shape._cycle_seconds
        assert _at(shape, second)[0] == shape.burst_users

    def test_shape_ends_the_run_itself(self) -> None:
        shape = ClassroomBurstShape()
        assert _at(shape, shape.total_seconds) is None
        assert _at(shape, shape.total_seconds + 1) is None

    def test_total_seconds_accounts_for_every_burst(self) -> None:
        shape = ClassroomBurstShape()
        assert shape.total_seconds == (
            shape.warmup_seconds + shape.bursts * shape._cycle_seconds
        )


class TestSpawnRate:
    def test_spawn_rate_clears_the_gap_within_the_burst_window(self) -> None:
        # This is the number that makes it a burst. If it were low enough that
        # the ramp outlasted the window, the test would be measuring a ramp
        # while claiming to measure a spike.
        shape = ClassroomBurstShape()
        gap = shape.burst_users - shape.baseline_users
        assert shape._spawn_rate * shape.burst_seconds >= gap * 0.95

    def test_spawn_rate_is_never_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_BASELINE_USERS", "10")
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_USERS", "11")
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_SECONDS", "600")
        assert ClassroomBurstShape()._spawn_rate >= 1


class TestValidation:
    def test_burst_below_baseline_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_BASELINE_USERS", "50")
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_USERS", "10")
        with pytest.raises(ValueError, match="must be >="):
            ClassroomBurstShape()

    def test_zero_burst_window_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_SECONDS", "0")
        with pytest.raises(ValueError, match="BURST_SECONDS"):
            ClassroomBurstShape()

    def test_non_integer_env_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_USERS", "lots")
        with pytest.raises(ValueError, match="must be an integer"):
            ClassroomBurstShape()

    def test_env_overrides_are_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_USERS", "250")
        monkeypatch.setenv("AGENTCORE_LOAD_BURST_COUNT", "1")
        shape = ClassroomBurstShape()
        assert shape.burst_users == 250
        assert shape.bursts == 1
