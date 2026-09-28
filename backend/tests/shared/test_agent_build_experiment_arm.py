"""`agent_build_experiment_arm`: default OFF, and a stable per-session split."""

import uuid

import pytest

from apis.shared.feature_flags import (
    AGENT_BUILD_ARMS,
    agent_build_experiment_arm,
    agent_build_off_loop_enabled,
    memory_shared_clients_enabled,
)


@pytest.mark.parametrize("value", [None, "", "off", "false", "true", "nonsense"])
def test_everything_but_ab_or_an_arm_name_is_control(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)
    else:
        monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", value)

    assert agent_build_experiment_arm("s1") == "control"
    assert not memory_shared_clients_enabled("s1")
    assert not agent_build_off_loop_enabled("s1")


@pytest.mark.parametrize("arm", AGENT_BUILD_ARMS)
def test_an_arm_name_forces_that_arm(monkeypatch, arm):
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", f" {arm.upper()} ")
    assert agent_build_experiment_arm("s1") == arm


def test_the_arms_imply_their_changes(monkeypatch):
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "shared_clients")
    assert memory_shared_clients_enabled("s") and not agent_build_off_loop_enabled("s")

    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "shared_clients_off_loop")
    assert memory_shared_clients_enabled("s") and agent_build_off_loop_enabled("s")


def test_ab_is_stable_per_session_and_uses_every_arm(monkeypatch):
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "ab")
    sessions = [str(uuid.UUID(int=i)) for i in range(300)]

    arms = [agent_build_experiment_arm(s) for s in sessions]

    assert arms == [agent_build_experiment_arm(s) for s in sessions]
    counts = {arm: arms.count(arm) for arm in AGENT_BUILD_ARMS}
    assert all(60 <= n <= 140 for n in counts.values()), counts


def test_ab_without_a_session_is_control(monkeypatch):
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "ab")
    assert agent_build_experiment_arm(None) == "control"
