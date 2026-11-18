"""Tests for pipe mode (``aps-tui -p``).

The turn machinery is the chat screen's, so these assert only what pipe mode
adds: what reaches stdout and what reaches stderr, the exit code for each way a
turn ends, and that abandoning a turn (Ctrl+C, a closed stdout) stops it on the
server too.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aps_tui import cli, pipe
from aps_tui.client import ApiConverseClient
from aps_tui.client.agent_events import (
    Done,
    ErrorEvent,
    MessageStart,
    MessageStop,
    OAuthRequired,
    QuotaExceeded,
    TextDelta,
    ToolApprovalRequired,
    ToolResult,
    ToolUse,
)
from aps_tui.config import Config
from aps_tui.errors import UpstreamError

from .conftest import make_config, sse_response, text_stream
from .test_agent_turn import FakeAgentClient, answer


class BrokenOut(io.StringIO):
    """A stdout whose reader went away after the first write, as with `| head -c1`."""

    def write(self, text: str) -> int:
        raise BrokenPipeError


class HangingAgentClient(FakeAgentClient):
    """Sends some text, then goes quiet as a slow tool would, until cancelled."""

    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.stream_calls.append(kwargs)

        async def gen() -> AsyncIterator[Any]:
            yield MessageStart()
            yield TextDelta(index=0, text="partial")
            self.started.set()
            await asyncio.Event().wait()
            yield Done()  # pragma: no cover - never reached

        return gen()


async def run(
    events: list[Any] | None = None,
    *,
    client: FakeAgentClient | None = None,
    out: io.StringIO | None = None,
    prompt: str = "hello",
) -> tuple[int, str, str, FakeAgentClient]:
    agent = client or FakeAgentClient(events or [])
    stdout = out if out is not None else io.StringIO()
    stderr = io.StringIO()
    code = await pipe.run_pipe(make_config(), prompt, out=stdout, err=stderr, agent_client_supplier=lambda: agent)
    return code, stdout.getvalue(), stderr.getvalue(), agent


class TestComposePrompt:
    def test_argument_alone(self) -> None:
        assert pipe.compose_prompt("hi", None) == "hi"

    def test_stdin_alone(self) -> None:
        assert pipe.compose_prompt("", "from a pipe\n") == "from a pipe"

    def test_argument_comes_before_the_piped_material(self) -> None:
        assert pipe.compose_prompt("why is this failing?", "Traceback ...\n") == "why is this failing?\n\nTraceback ..."

    def test_blank_input_is_no_prompt(self) -> None:
        assert pipe.compose_prompt("  ", "\n\n") == ""
        assert pipe.compose_prompt(None, None) == ""


class TestAnswer:
    async def test_only_the_answer_reaches_stdout(self) -> None:
        code, out, err, _ = await run(answer("Hello there"))
        assert code == pipe.EXIT_OK
        assert out == "Hello there\n"
        assert err == ""

    async def test_no_extra_newline_when_the_answer_ends_with_one(self) -> None:
        _, out, _, _ = await run(answer("line\n"))
        assert out == "line\n"

    async def test_sends_the_prompt_with_no_local_tools(self) -> None:
        _, _, _, client = await run(answer("ok"), prompt="what can you do")
        (call,) = client.stream_calls
        assert call["message"] == "what can you do"
        assert call["client_tools"] is None

    async def test_server_tools_are_reported_on_stderr_once_each(self) -> None:
        events = [
            MessageStart(),
            ToolUse(tool_use_id="t1", name="calculator"),
            ToolResult(tool_use_id="t1", text="4"),
            *answer("4"),
        ]
        code, out, err, _ = await run(events)
        assert code == pipe.EXIT_OK
        assert out == "4\n"
        assert err.count("tool: calculator") == 1

    async def test_truncated_answer_still_succeeds_with_a_note(self) -> None:
        events = [MessageStart(), TextDelta(index=0, text="Part one"), MessageStop(stop_reason="max_tokens"), Done()]
        code, out, err, _ = await run(events)
        assert code == pipe.EXIT_OK
        assert out == "Part one\n"
        assert "token limit" in err

    async def test_api_key_uses_the_converse_dialect(self) -> None:
        config = make_config()

        def supplier() -> ApiConverseClient:
            transport = httpx.MockTransport(lambda _: sse_response(text_stream(["Hel", "lo"])))
            return ApiConverseClient(config, client=httpx.AsyncClient(transport=transport))

        out, err = io.StringIO(), io.StringIO()
        code = await pipe.run_pipe(config, "hi", out=out, err=err, client_supplier=supplier)
        assert code == pipe.EXIT_OK
        assert out.getvalue() == "Hello\n"


class TestFailures:
    async def test_quota_block_fails(self) -> None:
        code, out, err, _ = await run([QuotaExceeded(message="Monthly limit reached"), Done()])
        assert code == pipe.EXIT_FAILED
        assert out == ""
        assert "quota" in err

    async def test_server_error_event_fails(self) -> None:
        code, _, err, _ = await run([MessageStart(), ErrorEvent(message="the agent crashed"), Done()])
        assert code == pipe.EXIT_FAILED
        assert "error: the agent crashed" in err

    async def test_transport_error_fails_with_its_hint(self) -> None:
        client = FakeAgentClient([], raises=UpstreamError("Server error (HTTP 502)"))
        code, out, err, _ = await run(client=client)
        assert code == pipe.EXIT_FAILED
        assert out == ""
        assert "HTTP 502" in err and "Retrying usually helps" in err


class TestPaused:
    async def test_oauth_pause_exits_3_and_names_the_conversation(self) -> None:
        events = [
            MessageStart(),
            OAuthRequired(provider_id="google", authorization_url="https://consent.example/authorize", interrupt_id="i1"),
            Done(),
        ]
        code, _, err, client = await run(events)
        assert code == pipe.EXIT_PAUSED
        assert "connect your google account" in err
        assert "/conversations" in err
        (call,) = client.stream_calls
        assert call["session_id"] in err
        # Paused, not generating: the server keeps the turn for a later answer.
        assert client.interrupts == []

    async def test_approval_pause_names_the_tool(self) -> None:
        events = [MessageStart(), ToolApprovalRequired(interrupt_id="i1", tool_use_id="t1", tool_name="send_email"), Done()]
        code, _, err, _ = await run(events)
        assert code == pipe.EXIT_PAUSED
        assert "approval to run send_email" in err


class TestAbandoning:
    async def test_closed_stdout_cancels_the_turn_on_the_server(self) -> None:
        client = HangingAgentClient()
        code, _, _, _ = await asyncio.wait_for(run(client=client, out=BrokenOut()), timeout=5)
        assert code == pipe.EXIT_BROKEN_PIPE
        assert len(client.interrupts) == 1

    async def test_ctrl_c_cancels_the_turn_on_the_server(self) -> None:
        client = HangingAgentClient()
        task = asyncio.create_task(run(client=client))
        await asyncio.wait_for(client.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client.interrupts) == 1


class TestCommandLine:
    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stub the turn and the config, so these cover only argument handling."""
        seen: dict[str, Any] = {}

        async def fake_run_pipe(config: Config, prompt: str, **_: Any) -> int:
            seen["config"], seen["prompt"] = config, prompt
            return pipe.EXIT_OK

        def fake_resolve(**kwargs: Any) -> Config:
            seen["resolve"] = kwargs
            return make_config()

        monkeypatch.setattr(pipe, "run_pipe", fake_run_pipe)
        monkeypatch.setattr(cli, "resolve_config", fake_resolve)
        monkeypatch.setattr(cli, "_read_piped_stdin", lambda: None)
        return seen

    def test_flag_parses_with_and_without_a_prompt(self) -> None:
        parse = cli._build_parser().parse_args
        assert cli._normalise(parse([])).print_prompt is None
        assert cli._normalise(parse(["-p", "hi"])).print_prompt == "hi"
        assert cli._normalise(parse(["--print"])).print_prompt == ""
        assert cli._normalise(parse(["-p", "hi", "--model", "m"])).model_id == "m"

    def test_runs_the_turn_with_local_tools_off(self, captured: dict[str, Any]) -> None:
        assert cli.main(["-p", "hi what can you do"]) == pipe.EXIT_OK
        assert captured["prompt"] == "hi what can you do"
        assert captured["resolve"]["local_tools"] is False

    def test_piped_stdin_follows_the_argument(self, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_read_piped_stdin", lambda: "the log\n")
        assert cli.main(["-p", "summarise"]) == pipe.EXIT_OK
        assert captured["prompt"] == "summarise\n\nthe log"

    def test_no_prompt_is_a_usage_error(self, captured: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["-p"]) == pipe.EXIT_USAGE
        assert "no prompt" in capsys.readouterr().err
        assert "prompt" not in captured

    def test_local_tools_flag_is_refused(self, captured: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--local-tools", "-p", "hi"]) == pipe.EXIT_USAGE
        assert "--local-tools" in capsys.readouterr().err
        assert "prompt" not in captured

    def test_subcommand_is_refused(self, captured: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["-p", "hi", "status"]) == pipe.EXIT_USAGE
        assert "cannot be combined" in capsys.readouterr().err

    def test_not_configured_is_a_usage_error(
        self, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "resolve_config", lambda **_: Config())
        assert cli.main(["-p", "hi"]) == pipe.EXIT_USAGE
        err = capsys.readouterr().err
        assert "not configured" in err and "login --sso" in err
        assert "prompt" not in captured
