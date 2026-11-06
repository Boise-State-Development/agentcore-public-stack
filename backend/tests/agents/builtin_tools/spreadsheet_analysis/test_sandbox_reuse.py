"""Turn-scoped sandbox reuse for analyze_spreadsheet (#259).

A simulated sandbox (``SandboxSim``) answers the tool's executeCode calls the
way the real one would: the XLSX bootstrap, the schema probe, the stat/cleanup
call that guards reuse, and the user's code. Each ``CodeInterpreter()`` the
tool constructs is a fresh ``FakeCodeInterpreter`` with its own simulated
filesystem, so tests can tell a reused sandbox from a new one.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import threading
import time
from typing import Callable, Optional
from unittest.mock import MagicMock, patch

import pytest

from agents.builtin_tools.spreadsheet_analysis import analyze_tool, sandbox_pool
from agents.builtin_tools.spreadsheet_analysis.analyze_tool import (
    _STAT_MARKER,
    make_analyze_tool,
)

from .conftest import (
    FakeCodeInterpreter,
    SESSIONS_BUCKET,
    _stream_response,
    _unwrap,
    build_bootstrap_stdout,
    build_schema_stdout,
)

SESSION = "s1"
USER = "u1"


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch):
    monkeypatch.delenv("ANALYZE_SANDBOX_REUSE_ENABLED", raising=False)
    sandbox_pool._reset_for_tests()
    yield
    sandbox_pool._reset_for_tests()


class SandboxSim:
    """One simulated sandbox: a tiny filesystem plus canned tool outputs."""

    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self.fs: dict[str, tuple[int, int]] = {}
        self.charts: set[str] = set()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._clock = 0

    def _touch(self, path: str) -> None:
        self._clock += 1
        self.fs[path] = (100 + self._clock, 1_000 + self._clock)

    def reply(self, name: str, payload: dict) -> dict:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.01)  # widen the window in which calls could overlap
            return self._reply(name, payload)
        finally:
            with self._lock:
                self.active -= 1

    def _reply(self, name: str, payload: dict) -> dict:
        if name == "writeFiles":
            for item in payload["content"]:
                if item["path"] != "_encoded.b64":
                    self._touch(item["path"])
            return _stream_response()
        if name == "readFiles":
            path = payload["paths"][0]
            if path in self.charts:
                return {"stream": [{"result": {"content": [{"data": b"PNG-" + path.encode()}]}}]}
            return {"stream": [{"result": {"content": []}}]}
        if name != "executeCode":
            return _stream_response()

        code = payload["code"]
        stat_out = ""
        if _STAT_MARKER in code:
            paths = ast.literal_eval(re.search(r"^_paths = (.*)$", code, re.M).group(1))
            remove = ast.literal_eval(re.search(r"^_remove = (.*)$", code, re.M).group(1))
            if remove:
                self.charts.discard(remove)
            out = {p: list(self.fs[p]) if p in self.fs else None for p in paths}
            stat_out = f"{_STAT_MARKER}\n{json.dumps(out)}\n{_STAT_MARKER}\n"
        if "_FNAME" in code and "_score" in code:
            # Schema probe, with the stat/cleanup appended for a pooled sandbox.
            self.harness.probes += 1
            fname = ast.literal_eval(re.search(r"^_FNAME = (.*)$", code, re.M).group(1))
            return _stream_response(build_schema_stdout(file=fname) + stat_out)
        if stat_out:
            return _stream_response(stat_out)
        if "load_workbook" in code:
            self.harness.bootstraps += 1
            for rec in self.harness.workbook_sheets:
                self._touch(rec[1])
                if rec[4]:
                    self._touch(rec[4])
            return _stream_response(
                build_bootstrap_stdout(total=len(self.harness.workbook_sheets), sheets=self.harness.workbook_sheets)
            )
        # The user's code.
        self.harness.user_runs.append(code)
        if self.harness.on_user_code:
            self.harness.on_user_code(self, code)
        return _stream_response("result\n")


class Harness:
    """Hands out a fresh simulated sandbox per ``CodeInterpreter()`` call."""

    def __init__(self) -> None:
        self.created: list[FakeCodeInterpreter] = []
        self.sims: list[SandboxSim] = []
        self.downloads = 0
        self.bootstraps = 0
        self.probes = 0
        self.user_runs: list[str] = []
        self.workbook_sheets: list[tuple[str, str, int, bool, str]] = []
        self.on_user_code: Optional[Callable[[SandboxSim, str], None]] = None

    def factory(self, *_a, **_k) -> FakeCodeInterpreter:
        sim = SandboxSim(self)
        fake = FakeCodeInterpreter(reply_for=sim.reply)
        self.created.append(fake)
        self.sims.append(sim)
        return fake


@pytest.fixture
def harness(code_interpreter_id, sessions_bucket, seed_s3_object, file_sources):
    h = Harness()
    set_kb, set_session = file_sources
    h.set_files = set_session  # type: ignore[attr-defined]
    h.seed = seed_s3_object  # type: ignore[attr-defined]

    real_download = analyze_tool._download_file

    async def counting_download(file_info):
        h.downloads += 1
        return await real_download(file_info)

    with patch(
        "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
        side_effect=h.factory,
    ), patch.object(analyze_tool, "_download_file", counting_download):
        yield h


def _csv(name="data.csv", key=None, size=1024):
    return {
        "filename": name,
        "source": "chat_attachment",
        "content_type": "text/csv",
        "size_bytes": size,
        "document_id": f"upload-{name}",
        "s3_key": key or f"sessions/{name}",
        "s3_bucket": SESSIONS_BUCKET,
    }


def _xlsx(name="Budget.xlsx", key=None, size=2048):
    return {
        "filename": name,
        "source": "chat_attachment",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size_bytes": size,
        "document_id": f"upload-{name}",
        "s3_key": key or f"sessions/{name}",
        "s3_bucket": SESSIONS_BUCKET,
    }


def _tool(session=SESSION, user=USER):
    return _unwrap(make_analyze_tool(None, session, user))


def _run(coro):
    return asyncio.run(coro)


async def _call(fn, filename="data.csv", code="print(1)", output_filename=None):
    return await fn(filename=filename, python_code=code, output_filename=output_filename)


def _exec_codes(fake: FakeCodeInterpreter) -> list[str]:
    return fake.executed_codes()


class TestReuseWithinTurn:
    def test_second_call_on_same_csv_skips_download_start_upload_and_probe(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a,b\n1,2\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            assert token
            fn = _tool()
            first = await _call(fn)
            second = await _call(fn, code="print(2)")
            await sandbox_pool.end_turn(SESSION, USER, token)
            return first, second

        first, second = _run(scenario())

        assert first["status"] == second["status"] == "success"
        assert len(harness.created) == 1
        assert harness.downloads == 1
        assert harness.probes == 1
        fake = harness.created[0]
        assert [r.name for r in fake.invocations].count("writeFiles") == 1
        # The reused call still carries the schema footer.
        assert "Dataset: file: data.csv" in second["content"][0]["text"]
        assert harness.user_runs == ["print(1)", "print(2)"]
        # Only a (cheap) stat check ran before the second user code.
        assert fake.stopped

    @pytest.mark.parametrize("pooled", [False, True])
    def test_first_call_costs_no_extra_round_trip(self, harness, pooled):
        """Most turns call the tool once: pooling must not add a sandbox call
        to that case. The stat/cleanup rides in the schema-probe exec."""
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER) if pooled else None
            await _call(_tool(), output_filename="chart.png")
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

        names = [r.name for r in harness.created[0].invocations]
        assert names == ["writeFiles", "executeCode", "executeCode", "readFiles"]
        probe = harness.created[0].executed_codes()[0]
        assert (_STAT_MARKER in probe) is pooled

    def test_second_call_on_multi_sheet_xlsx_keeps_sheet_note(self, harness):
        harness.set_files([_xlsx()])
        harness.seed("sessions/Budget.xlsx", b"xlsx-bytes")
        harness.workbook_sheets = [
            ("Summary", "Budget.summary.csv", 3, False, "Budget.csv"),
            ("Notes", "Budget.notes.csv", 2, False, ""),
        ]

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            first = await _call(fn, "Budget.xlsx")
            second = await _call(fn, "Budget.xlsx", "print(2)")
            await sandbox_pool.end_turn(SESSION, USER, token)
            return first, second

        first, second = _run(scenario())

        assert harness.bootstraps == 1
        assert harness.probes == 1
        assert harness.downloads == 1
        assert len(harness.created) == 1
        assert "Available sheets" in second["content"][0]["text"]
        assert second["content"][0]["text"] == first["content"][0]["text"]

    def test_different_file_reuses_sandbox_and_stages_only_the_new_file(self, harness):
        harness.set_files([_csv("a.csv"), _csv("b.csv")])
        harness.seed("sessions/a.csv", b"x\n1\n")
        harness.seed("sessions/b.csv", b"y\n2\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            await _call(fn, "a.csv")
            await _call(fn, "b.csv")
            await _call(fn, "a.csv")
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

        assert len(harness.created) == 1
        assert harness.downloads == 2  # a once, b once, third call is a hit
        assert harness.probes == 2

    def test_changed_source_restages_that_file_only(self, harness):
        harness.set_files([_csv(key="sessions/v1.csv", size=10)])
        harness.seed("sessions/v1.csv", b"x\n1\n")
        harness.seed("sessions/v2.csv", b"x\n1\n2\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            await _call(fn)
            harness.set_files([_csv(key="sessions/v2.csv", size=12)])
            await _call(fn)
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

        assert len(harness.created) == 1
        assert harness.downloads == 2
        assert harness.probes == 2

    @pytest.mark.parametrize("mutation", ["rewrite", "delete"])
    def test_sandbox_csv_changed_by_user_code_restages(self, harness, mutation):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        def on_user_code(sim: SandboxSim, code: str) -> None:
            if "MUTATE" in code:
                if mutation == "rewrite":
                    sim._touch("data.csv")
                else:
                    sim.fs.pop("data.csv")

        harness.on_user_code = on_user_code

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            await _call(fn, code="# MUTATE")
            await _call(fn)
            await _call(fn)  # staged again by the previous call, so a hit
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

        assert harness.downloads == 2
        assert harness.probes == 2
        assert len(harness.created) == 1

    def test_expired_session_is_replaced_and_call_retried_once(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            await _call(fn)
            harness.created[0].expire()
            second = await _call(fn, code="print(2)")
            await sandbox_pool.end_turn(SESSION, USER, token)
            return second

        second = _run(scenario())

        assert second["status"] == "success"
        assert len(harness.created) == 2
        assert harness.created[0].stopped
        assert harness.created[1].stopped
        # The replacement was staged from scratch.
        assert harness.downloads == 2
        assert harness.user_runs == ["print(1)", "print(2)"]

    def test_expired_fresh_session_is_not_retried(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        original = harness.factory

        def expiring_factory(*a, **k):
            fake = original(*a, **k)
            fake.expire()
            return fake

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            try:
                with patch(
                    "bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter",
                    side_effect=expiring_factory,
                ):
                    await _call(_tool())
            finally:
                await sandbox_pool.end_turn(SESSION, USER, token)

        with pytest.raises(Exception, match="not found"):
            _run(scenario())
        assert len(harness.created) == 1

    def test_concurrent_calls_on_one_sandbox_are_serialized(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            results = await asyncio.gather(*[_call(fn, code=f"print({i})") for i in range(4)])
            await sandbox_pool.end_turn(SESSION, USER, token)
            return results

        results = _run(scenario())

        assert all(r["status"] == "success" for r in results)
        assert len(harness.created) == 1
        assert harness.sims[0].max_active == 1
        assert harness.downloads == 1

    def test_stale_chart_is_never_returned(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        def on_user_code(sim: SandboxSim, code: str) -> None:
            if "SAVE" in code:
                sim.charts.add("chart.png")

        harness.on_user_code = on_user_code

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            fn = _tool()
            first = await _call(fn, code="# SAVE", output_filename="chart.png")
            second = await _call(fn, code="print('no chart this time')", output_filename="chart.png")
            await sandbox_pool.end_turn(SESSION, USER, token)
            return first, second

        first, second = _run(scenario())

        assert any("image" in b for b in first["content"])
        assert not any("image" in b for b in second["content"])


class TestPoolLifecycleAndIsolation:
    def test_end_turn_stops_sandbox_and_is_idempotent(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            await _call(_tool())
            assert not harness.created[0].stopped
            await sandbox_pool.end_turn(SESSION, USER, token)
            assert harness.created[0].stopped
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

    def test_stale_token_release_is_a_noop(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            old = await sandbox_pool.begin_turn(SESSION, USER)
            new = await sandbox_pool.begin_turn(SESSION, USER)
            assert old != new
            await _call(_tool())
            await sandbox_pool.end_turn(SESSION, USER, old)
            assert not harness.created[0].stopped
            await sandbox_pool.end_turn(SESSION, USER, new)
            assert harness.created[0].stopped

        _run(scenario())

    def test_new_turn_stops_a_sandbox_left_by_the_previous_turn(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            await sandbox_pool.begin_turn(SESSION, USER)
            await _call(_tool())
            await sandbox_pool.begin_turn(SESSION, USER)  # previous turn never released
            assert harness.created[0].stopped
            await _call(_tool())
            assert len(harness.created) == 2
            assert harness.downloads == 2  # nothing carried over from the old turn

        _run(scenario())

    def test_sessions_and_users_never_share_a_sandbox(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            keys = [("s1", "u1"), ("s1", "u2"), ("s2", "u1")]
            tokens = {k: await sandbox_pool.begin_turn(*k) for k in keys}
            for k in keys:
                await _call(_tool(*k))
            created = len(harness.created)
            entries = {k: sandbox_pool.acquire(*k) for k in keys}
            for k in keys:
                await sandbox_pool.end_turn(*k, tokens[k])
            return created, entries

        created, entries = _run(scenario())

        assert created == 3
        assert len({id(e) for e in entries.values()}) == 3
        assert len({f.session_id for f in harness.created}) == 3
        assert harness.downloads == 3

    def test_without_a_registered_turn_each_call_is_private_and_stopped(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            fn = _tool()
            await _call(fn)
            await _call(fn)

        _run(scenario())

        assert len(harness.created) == 2
        assert all(f.stopped for f in harness.created)
        assert harness.downloads == 2
        # Per-call sandboxes keep the SDK's default session timeout.
        assert "session_timeout_seconds" not in harness.created[0].start_kwargs

    def test_kill_switch_restores_per_call_start_and_stop(self, harness, monkeypatch):
        monkeypatch.setenv("ANALYZE_SANDBOX_REUSE_ENABLED", "false")
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            assert token is None
            fn = _tool()
            await _call(fn)
            await _call(fn)

        _run(scenario())

        assert len(harness.created) == 2
        assert all(f.stopped for f in harness.created)
        assert harness.downloads == 2
        # No stat/cleanup calls: the sequence is exactly the pre-#259 one.
        assert not any(_STAT_MARKER in c for f in harness.created for c in f.executed_codes())

    def test_pooled_session_gets_an_explicit_timeout(self, harness, monkeypatch):
        monkeypatch.setenv("ANALYZE_SANDBOX_TIMEOUT_SECONDS", "120")
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")

        async def scenario():
            token = await sandbox_pool.begin_turn(SESSION, USER)
            await _call(_tool())
            await sandbox_pool.end_turn(SESSION, USER, token)

        _run(scenario())

        assert harness.created[0].start_kwargs["session_timeout_seconds"] == 120

    @pytest.mark.parametrize(
        "raw,expected", [("", 900), ("abc", 900), ("5", 60), ("300", 300)]
    )
    def test_timeout_env_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("ANALYZE_SANDBOX_TIMEOUT_SECONDS", raw)
        assert sandbox_pool.sandbox_timeout_seconds() == expected


class TestEventLoopOffload:
    def test_blocking_calls_run_off_the_event_loop_thread(self, harness):
        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")
        dispatched: list[str] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(func, /, *args, **kwargs):
            dispatched.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        loop_thread = {}

        async def scenario():
            loop_thread["id"] = threading.get_ident()
            fn = _tool()
            await _call(fn)

        with patch("asyncio.to_thread", recording_to_thread):
            _run(scenario())

        fake = harness.created[0]
        assert fake.thread_ids, "the fake interpreter was never called"
        assert loop_thread["id"] not in fake.thread_ids
        assert "_get" in dispatched  # S3 get_object + read
        assert "start" in dispatched and "stop" in dispatched
        assert "_call" in dispatched  # each invoke, stream drained in the worker


class TestAnalyzeSandboxHook:
    def _registry(self, hook):
        from strands.hooks import HookRegistry

        registry = HookRegistry()
        hook.register_hooks(registry)
        return registry

    def test_after_invocation_releases_the_turn_sandbox(self, harness):
        from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent

        from agents.main_agent.session.hooks import AnalyzeSandboxHook

        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")
        registry = self._registry(AnalyzeSandboxHook(SESSION, USER))

        async def scenario():
            await registry.invoke_callbacks_async(BeforeInvocationEvent(agent=MagicMock()))
            fn = _tool()
            await _call(fn)
            await _call(fn)
            assert len(harness.created) == 1 and not harness.created[0].stopped
            await registry.invoke_callbacks_async(AfterInvocationEvent(agent=MagicMock()))
            assert harness.created[0].stopped
            assert sandbox_pool.acquire(SESSION, USER) is None

        _run(scenario())

    def test_release_runs_when_the_turn_ends_in_an_error_or_interrupt(self, harness):
        """Strands fires AfterInvocationEvent from a ``finally``; the hook must
        release whatever the turn's outcome carried (``result=None`` on an
        exception) and tolerate being released twice (coordinator ``finally``).
        """
        from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent

        from agents.main_agent.session.hooks import AnalyzeSandboxHook

        harness.set_files([_csv()])
        harness.seed("sessions/data.csv", b"a\n1\n")
        hook = AnalyzeSandboxHook(SESSION, USER)
        registry = self._registry(hook)

        async def scenario():
            await registry.invoke_callbacks_async(BeforeInvocationEvent(agent=MagicMock()))
            await _call(_tool())
            await registry.invoke_callbacks_async(AfterInvocationEvent(agent=MagicMock(), result=None))
            await hook.release()  # the stream coordinator's finally

        _run(scenario())
        assert harness.created[0].stopped

    def test_hook_is_inert_when_reuse_is_disabled(self, harness, monkeypatch):
        from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent

        from agents.main_agent.session.hooks import AnalyzeSandboxHook

        monkeypatch.setenv("ANALYZE_SANDBOX_REUSE_ENABLED", "false")
        registry = self._registry(AnalyzeSandboxHook(SESSION, USER))

        async def scenario():
            await registry.invoke_callbacks_async(BeforeInvocationEvent(agent=MagicMock()))
            assert sandbox_pool.acquire(SESSION, USER) is None
            await registry.invoke_callbacks_async(AfterInvocationEvent(agent=MagicMock()))

        _run(scenario())
