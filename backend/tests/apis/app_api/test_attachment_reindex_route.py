"""Opening a conversation re-queues its evicted large documents (large-document
spec §4E): ``GET /sessions/{id}/messages`` schedules it as a background task,
after the response, on the first page only, and only while the flag is on."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from apis.app_api.sessions import routes


def _call(next_token=None):
    tasks = BackgroundTasks()
    with patch.object(routes, "get_messages", AsyncMock(return_value={"messages": []})):
        asyncio.run(
            routes.get_session_messages_endpoint(
                "sess-1", limit=None, next_token=next_token, background_tasks=tasks,
                current_user=SimpleNamespace(user_id="user-1"),
            )
        )
    return tasks


def test_first_page_schedules_the_re_index(monkeypatch):
    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
    from apis.app_api.files.attachment_reindex import requeue_evicted_attachments

    tasks = _call()
    assert [(t.func, t.args) for t in tasks.tasks] == [(requeue_evicted_attachments, ("sess-1", "user-1"))]


@pytest.mark.parametrize("flag,token", [("true", "page-2"), ("", None), ("false", None)])
def test_later_pages_and_a_flag_off_schedule_nothing(monkeypatch, flag, token):
    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", flag)
    assert _call(next_token=token).tasks == []
