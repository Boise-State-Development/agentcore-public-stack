"""The tools that run on this machine, and the prompt that gates them.

The executor is tested with real subprocesses and a real temporary directory:
a fake shell would test the fake. The caps are the point, so they are exercised
with inputs that cross them.
"""

from __future__ import annotations

import os
import subprocess

import sys
from pathlib import Path

import pytest

from aps_tui.local_tools import ApprovalDecision, LocalToolCall, LocalToolResult, ToolExecutor, describe_call, tool_specs
from aps_tui.local_tools.executor import MAX_DIRECTORY_ENTRIES, MAX_FILE_BYTES, MAX_OUTPUT_CHARS

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


# ---------------------------------------------------------------- workspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first commit")
    return repo


class TestWorkspaceInfo:
    async def test_outside_a_repository(self, tmp_path: Path) -> None:
        result = await ToolExecutor(cwd=tmp_path).execute(call("workspace_info"))
        assert not result.is_error
        assert f"Directory: {tmp_path.resolve()}" in result.output
        assert "Git: not a repository" in result.output
        assert "Project files: none" in result.output

    async def test_a_clean_repository_reports_branch_commits_and_markers(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        result = await ToolExecutor(cwd=repo).execute(call("workspace_info"))
        out = result.output
        assert f"Git root: {repo.resolve()}" in out
        assert "Branch: main" in out
        assert "Changes: clean" in out
        assert "first commit" in out
        assert "Project files: pyproject.toml" in out

    async def test_changes_keep_porcelain_columns_and_never_include_contents(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        (repo / "README.md").write_text("SECRET-CONTENT-SHOULD-NOT-APPEAR\n")
        (repo / "new.txt").write_text("also secret\n")
        result = await ToolExecutor(cwd=repo).execute(call("workspace_info"))
        out = result.output
        assert "Changes: 2 file(s)" in out
        # Column 1 is meaningful: " M" is a worktree-only change, not staged.
        assert "   M README.md" in out
        assert "  ?? new.txt" in out
        assert "SECRET" not in out and "also secret" not in out

    async def test_a_subdirectory_reports_the_repository_root(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        sub = repo / "src"
        sub.mkdir()
        result = await ToolExecutor(cwd=sub).execute(call("workspace_info"))
        assert f"Git root: {repo.resolve()}" in result.output
        assert "Project files: none" in result.output  # markers are looked for in the cwd

    async def test_many_changes_are_capped(self, tmp_path: Path) -> None:
        from aps_tui.local_tools.executor import MAX_CHANGED_FILES

        repo = _repo(tmp_path)
        for i in range(MAX_CHANGED_FILES + 5):
            (repo / f"f{i}.txt").write_text("x")
        result = await ToolExecutor(cwd=repo).execute(call("workspace_info"))
        assert f"Changes: {MAX_CHANGED_FILES + 5} file(s)" in result.output
        assert "… 5 more" in result.output

    async def test_git_missing_is_reported_not_raised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        result = await ToolExecutor(cwd=tmp_path).execute(call("workspace_info"))
        assert not result.is_error
        assert "Git: not installed" in result.output

    def test_the_approval_prompt_says_no_contents_are_shared(self) -> None:
        headline, detail = describe_call(call("workspace_info"))
        assert "summary" in headline
        assert "No file contents" in detail

    def test_it_is_declared_first_and_takes_no_arguments(self) -> None:
        specs = tool_specs()
        assert specs[0]["name"] == "workspace_info"
        assert specs[0]["inputSchema"] == {"type": "object", "properties": {}}
