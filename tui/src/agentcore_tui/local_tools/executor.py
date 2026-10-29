"""Runs the declared local tools, after someone has said yes.

:class:`ToolExecutor` is transport-free and UI-free. It takes a
:class:`LocalToolCall`, runs it, and returns a :class:`LocalToolResult` whose
``to_wire()`` is the ``response`` the server expects on resume. The approval
decision is made by the caller (the chat screen renders the prompt) and passed
in, so this module can be tested without an app and the screen can be tested
without a subprocess.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .specs import LOCAL_TOOL_NAMES

logger = logging.getLogger(__name__)

#: Caps. The output cap protects the prompt: every character returned is a
#: token on the next model call, and the server truncates at 32k anyway.
MAX_OUTPUT_CHARS = 6_000
MAX_FILE_BYTES = 64 * 1024
MAX_WRITE_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 60.0
#: Entries listed before a directory listing is cut.
MAX_DIRECTORY_ENTRIES = 400


class ApprovalDecision(enum.Enum):
    """What the user said at the prompt."""

    APPROVE = "approve"
    #: Approve this and every later call to the same tool for the rest of the session.
    ALWAYS = "always"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class LocalToolCall:
    """One request from the server to run a local tool."""

    interrupt_id: str
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.tool_name in LOCAL_TOOL_NAMES


@dataclass(slots=True)
class LocalToolResult:
    """What the tool produced, in the shape the server's ``coerce_result`` reads."""

    output: str = ""
    is_error: bool = False
    declined: bool = False

    def to_wire(self) -> dict[str, Any]:
        if self.declined:
            return {"declined": True}
        return {"output": self.output, "isError": self.is_error}


def describe_call(call: LocalToolCall) -> tuple[str, str]:
    """A one-line headline and a detail block for the approval prompt.

    The headline is what the user must read before saying yes, so it is the
    command itself, or the path, never a paraphrase.
    """
    args = call.arguments
    match call.tool_name:
        case "run_command":
            headline = str(args.get("command", "")).strip() or "(empty command)"
            detail = str(args.get("reason", "")).strip()
        case "read_file":
            headline = f"read {args.get('path', '?')}"
            window = ""
            if args.get("start_line") or args.get("end_line"):
                window = f"lines {args.get('start_line', 1)}–{args.get('end_line', 'end')}"
            detail = window
        case "list_directory":
            headline = f"list {args.get('path') or '.'}"
            detail = ""
        case "write_file":
            content = str(args.get("content", ""))
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            headline = f"write {args.get('path', '?')}  ({len(content):,} chars, {lines} lines)"
            preview = content[:600]
            detail = preview + ("\n…" if len(content) > 600 else "")
        case _:
            headline = f"{call.tool_name} {args}"
            detail = ""
    return headline, detail


def _cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    return f"{head}\n… [{len(text) - limit:,} characters omitted] …\n{tail}"


class ToolExecutor:
    """Runs local tools. One per session; remembers "always" approvals."""

    def __init__(self, *, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._cwd = (cwd or Path.cwd()).resolve()
        self._timeout = timeout
        self._always: set[str] = set()

    @property
    def cwd(self) -> Path:
        return self._cwd

    def is_pre_approved(self, tool_name: str) -> bool:
        return tool_name in self._always

    def remember(self, tool_name: str) -> None:
        self._always.add(tool_name)

    def forget_all(self) -> None:
        self._always.clear()

    async def execute(self, call: LocalToolCall) -> LocalToolResult:
        """Run the call. Never raises: a failure is an error result the model can read."""
        try:
            match call.tool_name:
                case "run_command":
                    return await self._run_command(call.arguments)
                case "read_file":
                    return self._read_file(call.arguments)
                case "list_directory":
                    return self._list_directory(call.arguments)
                case "write_file":
                    return self._write_file(call.arguments)
                case _:
                    return LocalToolResult(output=f"unknown local tool: {call.tool_name}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - the result must reach the model
            logger.exception("local tool %s failed", call.tool_name)
            return LocalToolResult(output=f"{type(exc).__name__}: {exc}", is_error=True)

    # -- tools ---------------------------------------------------------------

    async def _run_command(self, args: dict[str, Any]) -> LocalToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return LocalToolResult(output="no command given", is_error=True)
        shell = os.environ.get("SHELL") or "/bin/sh"
        process = await asyncio.create_subprocess_exec(
            shell,
            "-c",
            command,
            cwd=self._cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat"},
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return LocalToolResult(output=f"timed out after {self._timeout:.0f}s: {shlex.quote(command)}", is_error=True)
        text = raw.decode("utf-8", errors="replace")
        code = process.returncode or 0
        body = _cap(text.rstrip("\n"))
        output = f"{body}\n[exit {code}]" if body else f"[exit {code}]"
        return LocalToolResult(output=output, is_error=code != 0)

    def _resolve(self, raw: Any) -> Path:
        path = Path(str(raw or ".")).expanduser()
        return (path if path.is_absolute() else self._cwd / path).resolve()

    def _read_file(self, args: dict[str, Any]) -> LocalToolResult:
        path = self._resolve(args.get("path"))
        if not path.is_file():
            return LocalToolResult(output=f"not a file: {path}", is_error=True)
        size = path.stat().st_size
        start = args.get("start_line")
        end = args.get("end_line")
        if size > MAX_FILE_BYTES and not (start or end):
            return LocalToolResult(
                output=f"{path} is {size:,} bytes, above the {MAX_FILE_BYTES:,}-byte cap; pass start_line/end_line to read a window",
                is_error=True,
            )
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES if not (start or end) else -1)
        text = data.decode("utf-8", errors="replace")
        if start or end:
            lines = text.splitlines()
            first = max(int(start or 1), 1)
            last = min(int(end or len(lines)), len(lines))
            window = lines[first - 1 : last]
            numbered = "\n".join(f"{first + i:>6}  {line}" for i, line in enumerate(window))
            return LocalToolResult(output=f"{path} lines {first}-{last} of {len(lines)}\n{_cap(numbered, MAX_FILE_BYTES)}")
        return LocalToolResult(output=f"{path}\n{text}")

    def _list_directory(self, args: dict[str, Any]) -> LocalToolResult:
        path = self._resolve(args.get("path"))
        if not path.is_dir():
            return LocalToolResult(output=f"not a directory: {path}", is_error=True)
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries[:MAX_DIRECTORY_ENTRIES]]
        if len(entries) > MAX_DIRECTORY_ENTRIES:
            lines.append(f"… {len(entries) - MAX_DIRECTORY_ENTRIES} more")
        return LocalToolResult(output=f"{path}\n" + "\n".join(lines))

    def _write_file(self, args: dict[str, Any]) -> LocalToolResult:
        path = self._resolve(args.get("path"))
        content = str(args.get("content", ""))
        if len(content.encode()) > MAX_WRITE_BYTES:
            return LocalToolResult(output=f"content exceeds the {MAX_WRITE_BYTES:,}-byte write cap", is_error=True)
        if path.is_dir():
            return LocalToolResult(output=f"is a directory: {path}", is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "wrote"
        return LocalToolResult(output=f"{verb} {path} ({len(content):,} chars)")
