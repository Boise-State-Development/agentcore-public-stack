"""Unit tests for the per-session single-flight primitive.

Covers the contract documented in
`backend/src/apis/shared/sessions_bff/single_flight.py`:

1. Two concurrent `resolve_once` calls for the same `session_id` share one
   loader invocation; both receive the same result.
2. An exception raised by the loader propagates to every current waiter
   (leader + all followers).
3. After a loader exception the registry entry is removed, so a subsequent
   call starts a fresh leader.
4. Distinct `session_id`s are independent (two different sessions produce two
   loader invocations).
5. Happy path: a single caller's result is returned correctly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

import pytest

from apis.shared.sessions_bff import single_flight
from apis.shared.sessions_bff.models import SessionRecord


def _make_record(session_id: str = "sess-sf-001") -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id=session_id,
        user_id=f"user-for-{session_id}",
        username="alice",
        cognito_access_token="access.token.value",
        cognito_refresh_token="refresh.token.value",
        id_token="id.token.value",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret-deadbeef",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    """Drop any residual in-flight Futures between tests."""
    single_flight._reset_for_tests()
    yield
    single_flight._reset_for_tests()


@pytest.mark.asyncio
async def test_happy_path_single_caller_returns_loader_result():
    """A lone caller receives the loader's exact return value."""
    record = _make_record()
    call_count = 0

    async def loader() -> Tuple[Optional[SessionRecord], bool]:
        nonlocal call_count
        call_count += 1
        return record, False

    result = await single_flight.resolve_once("sess-sf-001", loader)

    assert result == (record, False)
    assert call_count == 1
    # Registry is clean after success.
    assert "sess-sf-001" not in single_flight._inflight


@pytest.mark.asyncio
async def test_concurrent_same_session_share_one_loader_invocation():
    """N concurrent `resolve_once` calls on the same session call loader once."""
    record = _make_record()
    call_count = 0
    gate = asyncio.Event()

    async def loader() -> Tuple[Optional[SessionRecord], bool]:
        nonlocal call_count
        call_count += 1
        # Hold the leader open long enough for followers to attach.
        await gate.wait()
        return record, False

    async def release_after_followers_attach() -> None:
        # Give followers a chance to see the existing Future.
        await asyncio.sleep(0.05)
        gate.set()

    tasks = [
        asyncio.create_task(single_flight.resolve_once("sess-sf-002", loader))
        for _ in range(8)
    ]
    releaser = asyncio.create_task(release_after_followers_attach())

    results = await asyncio.gather(*tasks)
    await releaser

    assert call_count == 1, "loader must be invoked exactly once for shared session"
    for result in results:
        assert result == (record, False)
    assert "sess-sf-002" not in single_flight._inflight


@pytest.mark.asyncio
async def test_loader_exception_propagates_to_all_waiters():
    """An exception from the loader reaches the leader and every follower."""
    call_count = 0
    gate = asyncio.Event()

    class LoaderBoom(RuntimeError):
        pass

    async def loader() -> Tuple[Optional[SessionRecord], bool]:
        nonlocal call_count
        call_count += 1
        await gate.wait()
        raise LoaderBoom("cognito exploded")

    async def release_after_followers_attach() -> None:
        await asyncio.sleep(0.05)
        gate.set()

    tasks = [
        asyncio.create_task(single_flight.resolve_once("sess-sf-003", loader))
        for _ in range(5)
    ]
    releaser = asyncio.create_task(release_after_followers_attach())

    results = await asyncio.gather(*tasks, return_exceptions=True)
    await releaser

    assert call_count == 1
    assert len(results) == 5
    for outcome in results:
        assert isinstance(outcome, LoaderBoom)
        assert str(outcome) == "cognito exploded"


@pytest.mark.asyncio
async def test_registry_entry_removed_after_exception_so_next_call_is_fresh_leader():
    """After a loader failure, the next call must start a new leader."""
    attempts = 0

    async def failing_loader() -> Tuple[Optional[SessionRecord], bool]:
        nonlocal attempts
        attempts += 1
        raise ValueError("transient ddb failure")

    with pytest.raises(ValueError):
        await single_flight.resolve_once("sess-sf-004", failing_loader)

    # Registry entry must be gone so the next call is a new leader.
    assert "sess-sf-004" not in single_flight._inflight

    record = _make_record("sess-sf-004")

    async def succeeding_loader() -> Tuple[Optional[SessionRecord], bool]:
        nonlocal attempts
        attempts += 1
        return record, False

    result = await single_flight.resolve_once("sess-sf-004", succeeding_loader)

    assert result == (record, False)
    assert attempts == 2, "both loaders ran; the failure did not sticky-cache"
    assert "sess-sf-004" not in single_flight._inflight


@pytest.mark.asyncio
async def test_distinct_sessions_are_independent():
    """Two different `session_id`s run two independent loader invocations."""
    calls: list[str] = []
    record_a = _make_record("sess-A")
    record_b = _make_record("sess-B")

    async def loader_for(session_id: str, record: SessionRecord):
        async def _loader() -> Tuple[Optional[SessionRecord], bool]:
            calls.append(session_id)
            # Small sleep to encourage interleaving.
            await asyncio.sleep(0.01)
            return record, False

        return _loader

    loader_a = await loader_for("sess-A", record_a)
    loader_b = await loader_for("sess-B", record_b)

    result_a, result_b = await asyncio.gather(
        single_flight.resolve_once("sess-A", loader_a),
        single_flight.resolve_once("sess-B", loader_b),
    )

    assert result_a == (record_a, False)
    assert result_b == (record_b, False)
    assert sorted(calls) == ["sess-A", "sess-B"], "each session's loader runs exactly once"
    assert "sess-A" not in single_flight._inflight
    assert "sess-B" not in single_flight._inflight


@pytest.mark.asyncio
async def test_clear_cookie_flag_is_preserved():
    """`resolve_once` must faithfully propagate the `clear_cookie` bool."""

    async def loader_none_clear() -> Tuple[Optional[SessionRecord], bool]:
        return None, True

    result = await single_flight.resolve_once("sess-sf-005", loader_none_clear)
    assert result == (None, True)
