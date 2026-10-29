"""The tools that run on this machine, and the prompt that gates them.

The executor is tested with real subprocesses and a real temporary directory:
a fake shell would test the fake. The caps are the point, so they are exercised
with inputs that cross them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentcore_tui.local_tools import ApprovalDecision, LocalToolCall, LocalToolResult, ToolExecutor, describe_call, tool_specs
from agentcore_tui.local_tools.executor import MAX_DIRECTORY_ENTRIES, MAX_FILE_BYTES, MAX_OUTPUT_CHARS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")


def call(name: str, **arguments) -> LocalToolCall:
    return LocalToolCall(interrupt_id="i1", tool_use_id="tu1", tool_name=name, arguments=arguments)


class TestSpecs:
    def test_every_spec_has_the_server_shape(self) -> None:
        for spec in tool_specs():
            assert set(spec) == {"name", "description", "inputSchema"}
            assert spec["inputSchema"]["type"] == "object"
            assert spec["name"].isidentifier()

    def test_specs_are_stable_between_calls(self) -> None:
        """The server folds a digest of these into its agent cache key."""
        assert tool_specs() == tool_specs()

    def test_descriptions_say_the_user_approves(self) -> None:
        for spec in tool_specs():
            assert "approv" in spec["description"].lower()


class TestRunCommand:
    async def test_captures_output_and_exit_code(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="echo hello; echo err 1>&2"))
        assert not result.is_error
        assert "hello" in result.output and "err" in result.output
        assert result.output.endswith("[exit 0]")

    async def test_nonzero_exit_is_an_error_result(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="exit 3"))
        assert result.is_error
        assert "[exit 3]" in result.output

    async def test_runs_in_the_configured_directory(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("x")
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="ls"))
        assert "marker.txt" in result.output

    async def test_times_out(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path, timeout=0.2).execute(call("run_command", command="sleep 5"))
        assert result.is_error
        assert "timed out" in result.output

    async def test_output_is_capped_keeping_head_and_tail(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="seq 1 20000"))
        assert len(result.output) < MAX_OUTPUT_CHARS + 200
        assert result.output.startswith("1\n2\n")
        assert "20000" in result.output
        assert "omitted" in result.output

    async def test_empty_command_is_an_error(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="  "))
        assert result.is_error

    async def test_pagers_and_colour_are_off(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("run_command", command="echo $PAGER $NO_COLOR"))
        assert "cat 1" in result.output


class TestFiles:
    async def test_read_file_returns_contents_with_its_path(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("line1\nline2\n")
        result = await ToolExecutor(cwd=tmp_path).execute(call("read_file", path="a.txt"))
        assert not result.is_error
        assert result.output.startswith(str(target))
        assert "line1\nline2" in result.output

    async def test_read_window_is_numbered(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("\n".join(f"L{i}" for i in range(1, 51)))
        result = await ToolExecutor(cwd=tmp_path).execute(call("read_file", path="a.txt", start_line=10, end_line=12))
        assert "lines 10-12 of 50" in result.output
        assert "10  L10" in result.output and "L13" not in result.output

    async def test_large_file_needs_a_window(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        result = await ToolExecutor(cwd=tmp_path).execute(call("read_file", path="big.txt"))
        assert result.is_error and "start_line" in result.output

    async def test_missing_file_is_an_error_not_an_exception(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("read_file", path="nope.txt"))
        assert result.is_error and "not a file" in result.output

    async def test_list_directory_marks_directories(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "f.py").write_text("")
        result = await ToolExecutor(cwd=tmp_path).execute(call("list_directory"))
        lines = result.output.splitlines()
        assert lines[0] == str(tmp_path)
        assert "sub/" in lines and "f.py" in lines
        assert lines.index("sub/") < lines.index("f.py")

    async def test_list_directory_is_capped(self, tmp_path: Path) -> None:
        for i in range(MAX_DIRECTORY_ENTRIES + 5):
            (tmp_path / f"f{i:04}").write_text("")
        result = await ToolExecutor(cwd=tmp_path).execute(call("list_directory"))
        assert "5 more" in result.output

    async def test_write_file_creates_parents_and_reports(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("write_file", path="deep/dir/new.txt", content="hi\n"))
        assert not result.is_error
        assert (tmp_path / "deep/dir/new.txt").read_text() == "hi\n"
        assert result.output.startswith("wrote")
        again = await ToolExecutor(cwd=tmp_path).execute(call("write_file", path="deep/dir/new.txt", content="bye"))
        assert again.output.startswith("overwrote")

    async def test_write_to_a_directory_is_an_error(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("write_file", path=".", content="x"))
        assert result.is_error

    async def test_tilde_expands(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "h.txt").write_text("home")
        result = await ToolExecutor(cwd=tmp_path / "elsewhere").execute(call("read_file", path="~/h.txt"))
        assert "home" in result.output


class TestExecutorMisc:
    async def test_unknown_tool_is_an_error(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("format_disk"))
        assert result.is_error and "unknown" in result.output

    def test_always_approvals_are_remembered_and_cleared(self, tmp_path: Path) -> None:
        executor = ToolExecutor(cwd=tmp_path)
        assert not executor.is_pre_approved("run_command")
        executor.remember("run_command")
        assert executor.is_pre_approved("run_command")
        executor.forget_all()
        assert not executor.is_pre_approved("run_command")

    def test_wire_shapes(self) -> None:
        assert LocalToolResult(output="x", is_error=True).to_wire() == {"output": "x", "isError": True}
        assert LocalToolResult(declined=True).to_wire() == {"declined": True}

    def test_decisions_are_distinct(self) -> None:
        assert len({d.value for d in ApprovalDecision}) == 3


class TestDescribeCall:
    def test_command_headline_is_the_command_itself(self) -> None:
        headline, detail = describe_call(call("run_command", command="rm -rf build", reason="Clean before rebuilding."))
        assert headline == "rm -rf build"
        assert detail == "Clean before rebuilding."

    def test_write_shows_size_and_preview(self) -> None:
        headline, detail = describe_call(call("write_file", path="x.py", content="print(1)\n" * 200))
        assert headline.startswith("write x.py")
        assert "200 lines" in headline
        assert detail.endswith("…")

    def test_read_window(self) -> None:
        headline, detail = describe_call(call("read_file", path="a.py", start_line=5, end_line=9))
        assert headline == "read a.py"
        assert "5–9" in detail

    def test_unknown_tool_still_describes_itself(self) -> None:
        headline, _ = describe_call(call("weird", x=1))
        assert "weird" in headline
