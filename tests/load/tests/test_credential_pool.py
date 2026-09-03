"""Tests for credential assignment.

The behaviour under test is a refusal, not a feature. A pool smaller than the
requested user count used to be handled silently by round-robin, which produced
runs where many simulated users shared one ``user_id`` — and therefore one
DynamoDB partition, one quota counter and one memory namespace. The measured
latency then partly described the test's own key collisions. These tests pin the
refusal so that cannot come back quietly.
"""

from __future__ import annotations

import pytest

from agentcore_load.config import ConfigError, Credential
from agentcore_load.users import (
    CredentialExhausted,
    CredentialPool,
    _worker_partition,
    reset_pool,
)


def _creds(n: int) -> list[Credential]:
    return [Credential(username=f"loadtest-{i:02d}", password="pw") for i in range(n)]


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_pool()
    yield
    reset_pool()


class TestUniqueAssignment:
    def test_each_user_gets_a_distinct_credential(self) -> None:
        pool = CredentialPool(_creds(5))
        issued = [pool.acquire() for _ in range(5)]

        usernames = [c.username for c, _ in issued]
        assert len(set(usernames)) == 5
        assert all(exclusive for _, exclusive in issued)
        assert pool.available == 0

    def test_exhaustion_raises_with_an_actionable_message(self) -> None:
        pool = CredentialPool(_creds(2))
        pool.acquire()
        pool.acquire()

        with pytest.raises(CredentialExhausted) as exc:
            pool.acquire()

        message = str(exc.value)
        # The fix is to provision more accounts, so the error has to say so.
        assert "provision.sh" in message
        assert "AGENTCORE_LOAD_ALLOW_CREDENTIAL_REUSE" in message

    def test_released_credential_is_reissued(self) -> None:
        pool = CredentialPool(_creds(1))
        credential, exclusive = pool.acquire()
        pool.release(credential, exclusive)

        assert pool.available == 1
        again, _ = pool.acquire()
        assert again.username == credential.username

    def test_release_of_a_shared_credential_does_not_grow_the_pool(self) -> None:
        # Guards against a leak where reuse-mode releases inflate availability
        # and a later acquire hands out a credential that is already in use.
        pool = CredentialPool(_creds(1), allow_reuse=True)
        first, exclusive = pool.acquire()
        assert exclusive is True
        shared, exclusive = pool.acquire()
        assert exclusive is False

        pool.release(shared, exclusive)
        assert pool.available == 0

    def test_empty_pool_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            CredentialPool([])


class TestOptInReuse:
    def test_round_robin_cycles_once_opted_in(self) -> None:
        pool = CredentialPool(_creds(3), allow_reuse=True)
        for _ in range(3):
            pool.acquire()

        cycled = [pool.acquire()[0].username for _ in range(4)]
        assert cycled == [
            "loadtest-00",
            "loadtest-01",
            "loadtest-02",
            "loadtest-00",
        ]

    def test_reused_credentials_are_not_marked_exclusive(self) -> None:
        pool = CredentialPool(_creds(1), allow_reuse=True)
        pool.acquire()
        _, exclusive = pool.acquire()
        assert exclusive is False


class TestWorkerPartition:
    """Locust workers are separate processes with separate module state.

    Without a stride every worker deals from the top of the same deck, so
    uniqueness holds within a process and silently breaks across the fleet —
    exactly the distortion the pool exists to prevent, reintroduced by
    distribution.
    """

    def test_single_process_uses_the_whole_pool(self) -> None:
        assert _worker_partition(None) == (0, 1)

    def test_worker_takes_a_stride(self) -> None:
        environment = _FakeEnvironment(worker_index=1, expect_workers=4)
        assert _worker_partition(environment) == (1, 4)

    def test_unknown_fleet_size_falls_back_to_the_whole_pool(self) -> None:
        # Better to over-provision uniqueness within one process than to slice
        # the pool to a guessed width and hand out duplicates.
        environment = _FakeEnvironment(worker_index=2, expect_workers=None)
        assert _worker_partition(environment) == (0, 1)

    def test_stride_partitions_are_disjoint(self) -> None:
        credentials = _creds(10)
        total = 3
        slices = [credentials[i::total] for i in range(total)]

        seen = [c.username for s in slices for c in s]
        assert len(seen) == len(set(seen)) == 10


class _FakeEnvironment:
    def __init__(self, worker_index: int | None, expect_workers: int | None) -> None:
        self.runner = type("Runner", (), {"worker_index": worker_index})()
        self.parsed_options = type(
            "Options", (), {"expect_workers": expect_workers, "processes": None}
        )()
