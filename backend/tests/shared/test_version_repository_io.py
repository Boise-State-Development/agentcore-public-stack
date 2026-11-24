"""How the version store reaches DynamoDB: one cached resource, and never on app-api's event loop.

A fresh ``boto3.resource`` per call opened a new TLS connection on every version read, which a
pinned workflow step pays on every turn and every non-owner pays on every turn of a published
Agent (workflow-designer §23.4). ``run_blocking`` keeps app-api's loop free (the 2026-11-16
outage) and stays inline on the Runtime, where it is set to.
"""

import asyncio
import threading
from typing import Any, Dict, List

import pytest

from apis.shared import aws_clients, blocking_io
from apis.shared.assistants import version_repository


class _Table:
    def __init__(self, calls: List[str]) -> None:
        self.calls = calls

    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(threading.current_thread().name)
        return {}


@pytest.fixture()
def table_env(monkeypatch):
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "test-assistants")
    calls: List[str] = []
    built: List[str] = []

    class _Resource:
        def Table(self, name: str) -> _Table:  # noqa: N802 - boto3's name
            return _Table(calls)

    def fake_boto3_resource(service: str, *args: Any, **kwargs: Any) -> _Resource:
        built.append(service)
        return _Resource()

    import boto3

    monkeypatch.setattr(boto3, "resource", fake_boto3_resource)
    aws_clients.reset_cached_clients()
    yield calls, built
    aws_clients.reset_cached_clients()


def test_reads_share_one_resource(table_env):
    calls, built = table_env

    async def run() -> None:
        for _ in range(3):
            assert await version_repository.get_version("ast-1", 1) is None

    asyncio.run(run())
    assert len(calls) == 3
    assert built == ["dynamodb"]


def test_on_app_api_the_read_leaves_the_event_loop(table_env, monkeypatch):
    calls, _ = table_env
    monkeypatch.setattr(blocking_io, "_inline", False)

    async def run() -> str:
        await version_repository.get_version("ast-1", 1)
        return threading.current_thread().name

    loop_thread = asyncio.run(run())
    assert calls and calls[0] != loop_thread


def test_on_the_runtime_the_read_stays_inline(table_env, monkeypatch):
    calls, _ = table_env
    monkeypatch.setattr(blocking_io, "_inline", True)

    async def run() -> str:
        await version_repository.get_version("ast-1", 1)
        return threading.current_thread().name

    assert calls == [asyncio.run(run())]
