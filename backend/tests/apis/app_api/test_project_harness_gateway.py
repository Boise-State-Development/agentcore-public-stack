"""Purging a project's harness: the managed knowledge base goes with it.

Found validating Shared Projects on dev: ``DELETE /projects/{id}`` removed every
``AST#`` row of the harness except ``KB#``, and the harness's born-managed Bedrock
knowledge base stayed ``ACTIVE`` and billing. The gateway now queues it for the
migration worker's teardown (``tests/lambdas/test_kb_teardown.py`` covers what the
queue and the worker do); these tests pin when it is queued.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.projects.harness_gateway import AppApiHarnessGateway

MODULE = "apis.app_api.projects.harness_gateway"
HARNESS_ID = "ast-a1b2c3d4-0000-4000-8000-000000000010"


def _harness(kind: str = "project"):
    return SimpleNamespace(assistant_id=HARNESS_ID, owner_id="u-creator", kind=kind)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "test-assistants")


@pytest.fixture()
def calls():
    """Every collaborator, recording the order it was reached in."""
    order = []

    def recorder(name, result=None):
        async def _call(*args, **kwargs):
            order.append(name)
            return result

        return AsyncMock(side_effect=_call)

    with patch(f"{MODULE}._get_assistant_cloud_without_ownership_check",
               new_callable=AsyncMock, return_value=_harness()) as get, \
            patch(f"{MODULE}.queue_teardown", recorder("teardown", True)) as teardown, \
            patch(f"{MODULE}.list_assistant_documents",
                  recorder("list", ([SimpleNamespace(document_id="doc-1")], None))), \
            patch(f"{MODULE}.batch_soft_delete_documents", recorder("soft_delete")), \
            patch(f"{MODULE}.delete_sync_policies_for_assistant", recorder("sync_policies")), \
            patch(f"{MODULE}.delete_project_harness", recorder("delete_harness", True)) as delete, \
            patch(f"{MODULE}.delete_agent_icons", recorder("icons", 1)) as icons, \
            patch(f"{MODULE}.cleanup_assistant_documents", new_callable=AsyncMock), \
            patch(f"{MODULE}.asyncio.ensure_future"):
        yield SimpleNamespace(order=order, get=get, teardown=teardown, delete=delete, icons=icons)


def test_the_knowledge_base_is_queued_before_anything_is_destroyed(calls):
    asyncio.run(AppApiHarnessGateway().delete(HARNESS_ID))

    calls.teardown.assert_awaited_once_with(HARNESS_ID)
    assert calls.order == ["teardown", "list", "soft_delete", "sync_policies", "delete_harness", "icons"]
    calls.icons.assert_awaited_once_with(HARNESS_ID)


def test_a_retried_purge_still_queues_the_knowledge_base(calls):
    """A purge that failed after deleting the harness is retried with the KB# record
    still there, and the harness gone. This is the only place that queues it."""
    calls.get.return_value = None

    asyncio.run(AppApiHarnessGateway().delete(HARNESS_ID))

    # The icons too: the first attempt may have stopped right after the record delete.
    assert calls.order == ["teardown", "icons"]


def test_a_failure_to_queue_leaves_the_project_retryable(calls):
    calls.teardown.side_effect = RuntimeError("dynamodb unavailable")

    with pytest.raises(RuntimeError):
        asyncio.run(AppApiHarnessGateway().delete(HARNESS_ID))

    assert calls.order == []
    calls.delete.assert_not_awaited()
    calls.icons.assert_not_awaited()


def test_an_ordinary_agent_is_refused_before_its_knowledge_base_is_touched(calls):
    calls.get.return_value = _harness(kind=None)

    with pytest.raises(ValueError, match="not a project harness"):
        asyncio.run(AppApiHarnessGateway().delete(HARNESS_ID))

    calls.teardown.assert_not_awaited()
    calls.icons.assert_not_awaited()
