"""Retrieval stats for memory files (Shared Projects 2.6b, ``STATS#{slug}``).

A project harness's ``memory_read`` counts each file it returns: once per file
per turn, never for the index, and never on the tool's return path. The row is
one ``UpdateItem`` whose attributes must match the Runtime's IAM grant
(``MEMORY_STATS_ATTRIBUTES`` in ``inference-api-iam-roles.ts``).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from apis.shared.memory import stats
from apis.shared.memory.repository import MemorySpaceRepository

from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    ITEMS,
    OWNER,
    _team,
    env,
    gateway,
    memory,
    projects,
)

AT = "2026-10-08T15:00:00+00:00"


class TestRepository:
    def test_reads_add_up_and_a_deleted_file_takes_its_counts_with_it(self, projects, memory):
        team = _team(projects)
        sid = team.shared_space_id
        memory.save_entry(sid, EDITOR.user_id, EDITOR.email, "canvas", ITEMS)
        memory.repository.record_retrieval(sid, "canvas", "2026-10-08T14:00:00+00:00")
        memory.repository.record_retrieval(sid, "canvas", AT)
        memory.repository.record_retrieval(sid, "other", AT)

        counts = memory.repository.list_retrieval_stats(sid)
        assert {s: (c.retrieval_count, c.last_retrieved_at) for s, c in counts.items()} == {
            "canvas": (2, AT), "other": (1, AT),
        }
        memory.delete_entry(sid, EDITOR.user_id, EDITOR.email, "canvas")
        assert set(memory.repository.list_retrieval_stats(sid)) == {"other"}

    def test_the_update_touches_only_what_the_runtime_grant_allows(self):
        """Keep ``record_retrieval`` and the CDK condition in step, or the Runtime's counts are denied."""
        root = Path(__file__).resolve().parents[3]
        cdk = (root / "infrastructure/lib/constructs/inference-api/inference-api-iam-roles.ts").read_text()
        block = re.search(r"MEMORY_STATS_ATTRIBUTES: readonly string\[\] = \[(.*?)\];", cdk, re.S).group(1)
        granted = set(re.findall(r"'([^']+)'", block))

        calls: List[dict] = []
        repo = MemorySpaceRepository.__new__(MemorySpaceRepository)
        repo._table = SimpleNamespace(update_item=lambda **kw: calls.append(kw))
        repo.record_retrieval("spc", "canvas", AT)
        [call] = calls
        written = set(call["Key"]) | {
            name for name in re.findall(r"[A-Za-z]+", re.sub(r":\w+", "", call["UpdateExpression"]))
            if name not in ("SET", "ADD")
        }
        assert written == granted
        assert call["ReturnValues"] == "NONE"


class TestRecordRead:
    def test_once_per_turn_and_never_raises(self):
        submitted: List[tuple] = []
        seen: set = set()
        assert stats.record_read("spc", "canvas", seen=seen, submit=lambda fn, *a: submitted.append(a)) is True
        assert stats.record_read("spc", "canvas", seen=seen, submit=lambda fn, *a: submitted.append(a)) is False
        assert stats.record_read("spc", "other", seen=seen, submit=lambda fn, *a: submitted.append(a)) is True
        assert [a[:2] for a in submitted] == [("spc", "canvas"), ("spc", "other")]

        def shut(*_):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        assert stats.record_read("spc", "late", submit=shut) is False

    def test_a_failed_count_is_logged_not_raised(self, monkeypatch, caplog):
        class Broken:
            def record_retrieval(self, *a):
                raise RuntimeError("AccessDenied")

        monkeypatch.setattr(stats, "_repository", lambda: Broken())
        stats._record("spc", "canvas", AT)
        assert "could not count a read" in caplog.text


class TestMemoryReadTool:
    @pytest.fixture()
    def read(self, projects, memory, monkeypatch):
        from agents.builtin_tools.memory_spaces import project_tools
        from agents.builtin_tools.memory_spaces.project_tools import ProjectMemoryScopes, make_project_memory_tools

        team = _team(projects)
        memory.save_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "canvas", ITEMS)
        monkeypatch.setattr(project_tools, "MemorySpaceService", lambda: memory)
        counted: List[tuple] = []
        monkeypatch.setattr(project_tools, "record_read", lambda space_id, slug, *, seen: counted.append((slug, seen)))
        scopes = ProjectMemoryScopes(team.project_id, team.shared_space_id, None, OWNER)
        tools = {t.tool_name: t for t in make_project_memory_tools(scopes)}
        return tools["memory_read"], counted, team

    def test_a_file_read_is_counted_with_the_turns_set_and_the_index_is_not(self, read):
        tool, counted, team = read
        turn = SimpleNamespace(invocation_state={})
        assert asyncio.run(tool(scope="project", slug="canvas", tool_context=turn))["status"] == "success"
        assert asyncio.run(tool(scope="project", slug="MEMORY.md", tool_context=turn))["status"] == "success"
        assert asyncio.run(tool(scope="project", slug="nope", tool_context=turn))["status"] == "error"
        assert [slug for slug, _ in counted] == ["canvas"]
        assert counted[0][1] is turn.invocation_state["memory_stats_seen"]

    def test_the_spec_is_unchanged_by_the_context(self, read):
        tool, _, _ = read
        assert set(tool.tool_spec["inputSchema"]["json"]["properties"]) == {"scope", "slug"}


class TestOffTheReturnPath:
    def test_a_slow_count_never_delays_the_read(self, projects, memory, monkeypatch):
        """The count goes to a background thread: a 500 ms UpdateItem costs the read nothing."""
        from agents.builtin_tools.memory_spaces import project_tools
        from agents.builtin_tools.memory_spaces.project_tools import ProjectMemoryScopes, make_project_memory_tools

        team = _team(projects)
        memory.save_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "canvas", ITEMS)
        monkeypatch.setattr(project_tools, "MemorySpaceService", lambda: memory)
        done = threading.Event()

        class Slow:
            def record_retrieval(self, space_id, slug, at):
                time.sleep(0.5)
                done.set()

        monkeypatch.setattr(stats, "_repository", lambda: Slow())
        scopes = ProjectMemoryScopes(team.project_id, team.shared_space_id, None, OWNER)
        tool = {t.tool_name: t for t in make_project_memory_tools(scopes)}["memory_read"]

        started = time.perf_counter()
        result = asyncio.run(tool(scope="project", slug="canvas", tool_context=SimpleNamespace(invocation_state={})))
        elapsed = time.perf_counter() - started
        assert result["status"] == "success"
        assert elapsed < 0.4 and not done.is_set()
        assert done.wait(2)
