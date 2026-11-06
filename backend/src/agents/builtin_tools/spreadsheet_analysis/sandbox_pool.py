"""Turn-scoped Code Interpreter sandboxes for ``analyze_spreadsheet`` (#259).

Without this module every ``analyze_spreadsheet`` call starts an AgentCore
Code Interpreter session, stages the file (upload, XLSX -> CSV bootstrap,
schema probe), runs the user's code and stops the session. When the agent
analyzes one file in several steps inside a single turn, every call after the
first repeats that setup.

This pool keeps one sandbox per ``(session_id, user_id)`` alive for the
duration of one agent turn and remembers which files are already staged in it.

Lifecycle
---------
* ``AnalyzeSandboxHook`` (``agents/main_agent/session/hooks``) calls
  :func:`begin_turn` on ``BeforeInvocationEvent``. That registers a fresh
  per-turn token and stops any entry left over from an earlier turn.
* The tool calls :func:`acquire`. It returns ``None`` (the tool then falls back
  to a private, per-call sandbox) unless a turn is registered for the key, so
  nothing outside a hooked turn can leak a session.
* The hook calls :func:`end_turn` on ``AfterInvocationEvent``, which Strands
  fires from a ``finally`` (completion, exception, stream closed, interrupt).
  The stream coordinator calls it again from its own ``finally``. Both are
  idempotent and a call carrying a stale token is a no-op.
* The remote session timeout (``ANALYZE_SANDBOX_TIMEOUT_SECONDS``) is the last
  line of defense when in-process teardown never runs.

Isolation
---------
An entry is keyed by ``(session_id, user_id)``, never by session alone, is only
handed out while its token is the key's current turn token, and is stopped when
the turn ends. Each entry is its own Code Interpreter session, which is
AgentCore's isolation boundary, so Python state kept in the interpreter
(``clearContext=False``) cannot reach another conversation, another user, or a
later turn.

All blocking boto3 calls run in worker threads (``asyncio.to_thread``) so the
inference container's event loop stays free, as #260 did for the DynamoDB
lookups.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.feature_flags import analyze_sandbox_reuse_enabled
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300
_MIN_TIMEOUT_SECONDS = 60

PoolKey = Tuple[str, str]


def sandbox_timeout_seconds() -> int:
    """Remote session timeout for pooled sandboxes.

    Short on purpose: an orphaned session (process killed, disconnect teardown
    missed) expires on its own instead of lingering for the SDK's 900 s
    default. Invalid or tiny values fall back to the default / minimum.
    """
    raw = os.environ.get("ANALYZE_SANDBOX_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return max(int(raw), _MIN_TIMEOUT_SECONDS)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


_SESSION_GONE_PATTERN = re.compile(
    r"session.{0,40}(not found|expired|terminated|stopped|invalid|closed|does not exist)",
    re.IGNORECASE,
)


def is_session_gone(exc: BaseException) -> bool:
    """Whether ``exc`` means the remote Code Interpreter session no longer exists."""
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
    if code == "ResourceNotFoundException":
        return True
    return bool(_SESSION_GONE_PATTERN.search(str(exc)))


@dataclass
class StagedFile:
    """A source file already converted/uploaded into a sandbox."""

    source: str
    s3_key: str
    size_bytes: int
    is_xlsx: bool
    csv_filename: str
    multi_sheet_note: str
    schema_preview: str
    #: Sandbox paths of every CSV the staging produced.
    paths: List[str] = field(default_factory=list)
    #: ``path -> (size, mtime_ns)`` of ``paths`` as observed in the sandbox
    #: right after staging.
    files: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    def same_source(self, source: str, s3_key: str, size_bytes: int) -> bool:
        return (self.source, self.s3_key, self.size_bytes) == (source, s3_key, size_bytes)


class SandboxEntry:
    """One Code Interpreter session plus what is staged in it.

    ``pooled`` entries live in the module pool for a turn; a non-pooled entry
    is a private per-call sandbox that the tool stops itself, which is the
    pre-#259 behaviour (also used when the kill switch is off).
    """

    def __init__(self, key: PoolKey, token: Optional[str], *, pooled: bool) -> None:
        self.key = key
        self.token = token
        self.pooled = pooled
        #: Serializes calls on this sandbox: Strands can run two tool calls
        #: from one model response concurrently.
        self.lock = asyncio.Lock()
        self.staged: Dict[str, StagedFile] = {}
        self.closed = False
        #: Completed tool calls served by this session. A "session gone" error
        #: is only retried when the session had a previous life.
        self.calls = 0
        self._client: Any = None

    async def start(self, ci_id: str) -> None:
        if self._client is not None:
            return
        from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

        region = os.getenv("AWS_REGION", "us-west-2")
        client = CodeInterpreter(region)
        kwargs: Dict[str, Any] = {"identifier": ci_id}
        if self.pooled:
            kwargs["session_timeout_seconds"] = sandbox_timeout_seconds()
        await asyncio.to_thread(client.start, **kwargs)
        self._client = client

    async def invoke(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call the sandbox and drain the response stream, all off the loop."""
        client = self._client
        if client is None:
            raise RuntimeError("Code Interpreter session is not started")

        def _call() -> Dict[str, Any]:
            resp = client.invoke(method, params)
            return {**resp, "stream": list(resp.get("stream", []))}

        return await asyncio.to_thread(_call)

    async def stop(self) -> None:
        """Stop the remote session. Idempotent; never raises."""
        self.closed = True
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.to_thread(client.stop)
        except Exception:  # noqa: BLE001 - best effort, the remote timeout covers it
            logger.debug("analyze_sandbox stop failed", exc_info=True)


def new_transient_entry(session_id: str, user_id: str) -> SandboxEntry:
    """A private sandbox for one call (kill switch off, or no turn registered)."""
    return SandboxEntry((session_id, user_id), None, pooled=False)


# key -> token of the turn currently running for that conversation
_turns: Dict[PoolKey, str] = {}
# key -> live pooled sandbox
_entries: Dict[PoolKey, SandboxEntry] = {}


async def begin_turn(session_id: str, user_id: str) -> Optional[str]:
    """Register a new turn for the conversation and return its token.

    Returns ``None`` when sandbox reuse is disabled. Any sandbox left behind
    by an earlier turn that never got released is stopped here.
    """
    if not analyze_sandbox_reuse_enabled():
        return None
    key = (session_id, user_id)
    token = uuid.uuid4().hex
    _turns[key] = token
    stale = _entries.pop(key, None)
    if stale is not None:
        await stale.stop()
    return token


async def end_turn(session_id: str, user_id: str, token: Optional[str]) -> None:
    """Release the turn's sandbox. No-op unless ``token`` is the current one."""
    if not token:
        return
    key = (session_id, user_id)
    if _turns.get(key) != token:
        return
    del _turns[key]
    entry = _entries.pop(key, None)
    if entry is not None:
        await entry.stop()


def acquire(session_id: str, user_id: str) -> Optional[SandboxEntry]:
    """The conversation's sandbox for the running turn, created on demand.

    ``None`` means "no turn is registered or reuse is off": the caller uses a
    private per-call sandbox instead. Creating the entry is synchronous (the
    remote session starts lazily under the entry lock), so two concurrent
    callers always get the same entry.
    """
    if not analyze_sandbox_reuse_enabled():
        return None
    key = (session_id, user_id)
    token = _turns.get(key)
    if token is None:
        return None
    entry = _entries.get(key)
    if entry is None or entry.token != token or entry.closed:
        entry = SandboxEntry(key, token, pooled=True)
        _entries[key] = entry
    return entry


async def evict(entry: SandboxEntry) -> None:
    """Drop ``entry`` (e.g. its remote session expired) and stop it."""
    if _entries.get(entry.key) is entry:
        del _entries[entry.key]
    await entry.stop()


def log_outcome(outcome: str, session_id: str, filename: str) -> None:
    logger.info(
        "analyze_sandbox outcome=%s session=%s file=%s",
        outcome,
        scrub_log(session_id),
        scrub_log(filename),
    )


def _reset_for_tests() -> None:
    _turns.clear()
    _entries.clear()
