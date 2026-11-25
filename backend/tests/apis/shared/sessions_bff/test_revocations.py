"""The revocation feed: an ended session leaves every task's cache in one poll.

The dev readout after #2421 showed a signed-out session answering 200 for about
21 s, because only the task that served the logout dropped its cached copy.
These tests pin the fix against moto: `revoke` deletes and publishes, and a
sweep run by a *different* task (its own cache) drops that task's copy.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.repository import REVOCATION_PK
from apis.shared.sessions_bff.revocations import (
    POLL_INTERVAL_SECONDS,
    lookback_seconds,
    run_revocation_watcher,
    sweep_once,
)

from .test_session_refresh_middleware import _enabled_config


@pytest.mark.asyncio
async def test_revoke_deletes_the_row_and_publishes_it(repository, sample_record) -> None:
    record = sample_record()
    await repository.put(record)

    await repository.revoke(record.session_id)

    assert await repository.get(record.session_id) is None
    items = repository._table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": REVOCATION_PK}
    )["Items"]
    assert [i["session_id"] for i in items] == [record.session_id]
    assert int(items[0]["ttl"]) > time.time()


@pytest.mark.asyncio
async def test_recent_revocations_reads_only_the_lookback(repository) -> None:
    now = time.time()
    await repository.record_revocation("old", now=now - 600)
    await repository.record_revocation("recent", now=now - 30)
    await repository.record_revocation("just-now", now=now)

    assert sorted(await repository.recent_revocations(since=now - 60)) == ["just-now", "recent"]


@pytest.mark.asyncio
async def test_a_sweep_on_another_task_drops_its_cached_copy(repository, sample_record) -> None:
    """Two caches stand in for two app-api tasks behind the load balancer."""
    record = sample_record()
    await repository.put(record)
    serving_task, other_task = SessionCache(ttl_seconds=60), SessionCache(ttl_seconds=60)
    serving_task.set(record)
    other_task.set(record)
    bystander = sample_record(session_id="someone-else")
    other_task.set(bystander)

    await repository.revoke(record.session_id)
    serving_task.invalidate(record.session_id)  # what logout does locally
    assert other_task.get(record.session_id) is not None  # the bug, before a poll

    swept = await sweep_once(repository, other_task, lookback=lookback_seconds(_enabled_config()))

    assert swept == 1
    assert other_task.get(record.session_id) is None
    assert other_task.get(bystander.session_id) is not None


def test_the_lookback_covers_a_whole_cache_ttl() -> None:
    """A copy seeded just before logout lives one TTL; every poll in that time
    must still see the revocation."""
    config = _enabled_config()
    assert lookback_seconds(config) >= config.refresh_leeway_seconds + POLL_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_the_watcher_keeps_polling_through_a_failed_read(caplog) -> None:
    repository = AsyncMock()
    calls = 0

    async def flaky(*, since: float) -> list[str]:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("dynamo down")
        return ["sess-x"]

    repository.recent_revocations.side_effect = flaky
    cache = SessionCache(ttl_seconds=60)
    task = asyncio.create_task(
        run_revocation_watcher(
            config=_enabled_config(), repository=repository, cache=cache, interval=0.01
        )
    )
    try:
        for _ in range(200):
            if calls >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls >= 3
    # Logged once for the run of failures, not per poll.
    assert sum("revocation feed read failed" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_the_watcher_is_a_no_op_without_the_bff() -> None:
    from apis.shared.sessions_bff.config import BFFConfig

    disabled = BFFConfig(
        sessions_table_name=None,
        cookie_signing_key_arn=None,
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id=None,
        cognito_bff_app_client_secret_arn=None,
        inference_api_url=None,
    )
    repository = AsyncMock()
    await asyncio.wait_for(run_revocation_watcher(config=disabled, repository=repository), 1)
    repository.recent_revocations.assert_not_called()
