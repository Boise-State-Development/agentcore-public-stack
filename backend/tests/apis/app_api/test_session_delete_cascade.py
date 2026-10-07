"""One delete cascade for the two routes and the retention pruner."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

from apis.app_api.sessions.services.delete_cascade import SessionDeleteCascade

STEPS = ["agentcore_memory", "session_files", "share_snapshots", "artifact_shares", "conversation_archive"]


def _cascade(**overrides):
    service = MagicMock()
    share_service = MagicMock()
    share_service.delete_shares_for_session = AsyncMock(return_value=0)
    artifact_shares = MagicMock()
    archive = MagicMock(return_value=0)
    cascade = SessionDeleteCascade(
        session_service=overrides.get("service", service),
        share_service=share_service,
        artifact_share_service=artifact_shares,
        delete_archive=archive,
    )
    return cascade, service, share_service, artifact_shares, archive


def test_steps_are_the_five_cleanups_with_owner_scoped_arguments():
    cascade, *_ = _cascade()
    steps = cascade.steps("user-1", "sess-1")

    assert [name for name, _, _ in steps] == STEPS
    assert [args for _, _, args in steps] == [
        ("sess-1", "user-1"),
        ("sess-1",),
        ("sess-1",),
        ("sess-1", "user-1"),
        ("user-1", "sess-1"),
    ]


def test_queue_adds_every_step_as_a_background_task():
    cascade, service, share_service, artifact_shares, archive = _cascade()
    background = MagicMock()

    cascade.queue(background, "user-1", "sess-1")

    queued = [c.args[0] for c in background.add_task.call_args_list]
    assert queued == [
        service.delete_agentcore_memory,
        service.delete_session_files,
        share_service.delete_shares_for_session,
        artifact_shares.delete_for_session,
        archive,
    ]


def test_run_awaits_every_step_in_order():
    cascade, service, share_service, artifact_shares, archive = _cascade()

    failed = asyncio.run(cascade.run("user-1", "sess-1"))

    assert failed == []
    service.delete_agentcore_memory.assert_called_once_with("sess-1", "user-1")
    service.delete_session_files.assert_called_once_with("sess-1")
    share_service.delete_shares_for_session.assert_awaited_once_with("sess-1")
    artifact_shares.delete_for_session.assert_called_once_with("sess-1", "user-1")
    archive.assert_called_once_with("user-1", "sess-1")


def test_run_isolates_a_failing_step():
    cascade, service, _, _, archive = _cascade()
    service.delete_session_files.side_effect = RuntimeError("boom")

    failed = asyncio.run(cascade.run("user-1", "sess-1"))

    assert failed == ["session_files"]
    archive.assert_called_once()


def test_sync_steps_run_off_the_event_loop_thread():
    """delete_session_files drives its own event loop; it cannot share the caller's."""
    seen = []
    service = MagicMock()
    service.delete_session_files.side_effect = lambda _sid: seen.append(threading.current_thread())
    cascade, *_ = _cascade(service=service)

    asyncio.run(cascade.run("user-1", "sess-1"))

    assert seen and seen[0] is not threading.main_thread()
