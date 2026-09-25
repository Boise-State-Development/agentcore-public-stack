"""Tests for the runtime log retention sweep.

Run with: uv run --with boto3 --with pytest pytest infrastructure/lambda-assets/runtime-log-retention-sweep/
The logs client is a fake, so nothing here touches AWS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_HANDLER_PATH = Path(__file__).parent / "handler.py"
_spec = importlib.util.spec_from_file_location("runtime_log_retention_sweep_handler", _HANDLER_PATH)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

PREFIX = "/aws/bedrock-agentcore/runtimes/example_agentcore_runtime-"


def _group(suffix: str, retention: Optional[int]) -> Dict[str, Any]:
    group: Dict[str, Any] = {"logGroupName": f"{PREFIX}{suffix}"}
    if retention is not None:
        group["retentionInDays"] = retention
    return group


class FakePaginator:
    def __init__(self, pages: List[List[Dict[str, Any]]]):
        self._pages = pages
        self.kwargs: Dict[str, Any] = {}

    def paginate(self, **kwargs: Any):
        self.kwargs = kwargs
        for groups in self._pages:
            yield {"logGroups": groups}


class FakeLogs:
    def __init__(self, pages: List[List[Dict[str, Any]]], fail_on: Optional[str] = None):
        self.paginator = FakePaginator(pages)
        self.puts: List[Dict[str, Any]] = []
        self._fail_on = fail_on

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "describe_log_groups"
        return self.paginator

    def put_retention_policy(self, **kwargs: Any) -> None:
        if self._fail_on and kwargs["logGroupName"].endswith(self._fail_on):
            raise RuntimeError("AccessDenied")
        self.puts.append(kwargs)


def test_sets_retention_on_unset_and_longer_groups_only() -> None:
    client = FakeLogs([
        [_group("aaaaaaaaaa-DEFAULT", None), _group("bbbbbbbbbb-DEFAULT", 3653)],
        [_group("cccccccccc-DEFAULT", 30), _group("dddddddddd-DEFAULT", 7)],
    ])

    result = mod.sweep(client, PREFIX, 30)

    assert client.paginator.kwargs == {"logGroupNamePrefix": PREFIX}
    assert [p["logGroupName"][-18:] for p in client.puts] == [
        "aaaaaaaaaa-DEFAULT",
        "bbbbbbbbbb-DEFAULT",
    ]
    assert all(p["retentionInDays"] == 30 for p in client.puts)
    assert result["matched"] == 4
    assert len(result["updated"]) == 2


def test_dry_run_changes_nothing() -> None:
    client = FakeLogs([[_group("aaaaaaaaaa-DEFAULT", None)]])

    result = mod.sweep(client, PREFIX, 30, dry_run=True)

    assert client.puts == []
    assert len(result["updated"]) == 1


def test_one_failure_does_not_stop_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeLogs(
        [[_group("aaaaaaaaaa-DEFAULT", None), _group("bbbbbbbbbb-DEFAULT", None)]],
        fail_on="aaaaaaaaaa-DEFAULT",
    )
    monkeypatch.setattr(mod, "_client", client)
    monkeypatch.setenv("LOG_GROUP_PREFIX", PREFIX)
    monkeypatch.setenv("RETENTION_IN_DAYS", "30")

    with pytest.raises(RuntimeError, match="1 log group"):
        mod.handler({}, None)
    assert [p["logGroupName"][-18:] for p in client.puts] == ["bbbbbbbbbb-DEFAULT"]


@pytest.mark.parametrize(
    "prefix",
    [
        "/aws/bedrock-agentcore/runtimes/",
        "/aws/bedrock-agentcore/runtimes/-",
        "/aws/bedrock-agentcore/runtimes/example_agentcore_runtime",
        "/aws/lambda/example-",
    ],
)
def test_refuses_an_unscoped_prefix(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setattr(mod, "_client", FakeLogs([]))
    monkeypatch.setenv("LOG_GROUP_PREFIX", prefix)
    monkeypatch.setenv("RETENTION_IN_DAYS", "30")

    with pytest.raises(ValueError, match="unscoped"):
        mod.handler({}, None)
