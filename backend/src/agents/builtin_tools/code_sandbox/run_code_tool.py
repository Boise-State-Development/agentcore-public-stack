"""``run_code``: a skill's scripts, or Python the model writes, in the isolated sandbox.

Spec: ``docs/specs/skill-code-sandbox.md``. Behind ``CODE_SANDBOX_ENABLED`` and its
own catalog row (``run_code``, ``enabledByDefault: false``).

**Where it runs.** Only in the stack's Code Interpreter, and only once that
interpreter is confirmed to be in ``VPC`` network mode (an isolated VPC with no
route, no security-group egress and DNS off, ``code-interpreter-construct.ts``;
with ``CODE_SANDBOX_OFFICE_ENABLED``, DNS behind a firewall that answers one name
and NFS egress to a read-only LibreOffice mount, ``office_mount.py``).
The check is one control-plane read per process; a deployment still on a
``PUBLIC`` or ``SANDBOX`` interpreter gets a refusal, never a run. Nothing is
imported, compiled or executed on the Runtime host.

**One sandbox per turn.** The call shares the turn's pooled sandbox with
``analyze_spreadsheet`` (``spreadsheet_analysis/sandbox_pool.py``): keyed on
``(session, user)``, registered and stopped by ``AnalyzeSandboxHook``, so the
files a call writes are there for the turn's next call and gone after it.
Without a registered turn (or with the pool's kill switch off) the call gets
a private sandbox that it stops itself.

**Each run is its own process** (``executeCommand``), so a script's
``sys.exit``, ``chdir`` or module state cannot reach the next call
(``sandbox_programs.py``).

**Whose scripts.** A ``scripts/*.py`` file of a skill bound to the turn, by the
same strict lookup the Word tools use (``resolve_skill_script``). Until the
approval card for other authors' scripts exists (spec D4, PR-2), a script runs
only from a skill the user owns or an admin-catalog skill; anything else is
refused with a message that says why.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from strands import tool

from agents.builtin_tools.code_sandbox import office_mount
from agents.builtin_tools.code_sandbox import sandbox_programs as programs
from agents.builtin_tools.office._storage import (
    StorageQuotaExceededError,
    _download_card,
    _download_s3_bytes,
    _get_code_interpreter_id,
    _safe_document_name,
    _store_document,
)
from agents.builtin_tools.spreadsheet_analysis import sandbox_pool
from agents.main_agent.skills.skill_scripts import (
    SkillScriptError,
    TurnSkillRecords,
    attach_turn_skill_records,
    resolve_skill_script,
)
from agents.main_agent.skills.strands_mapping import _manifest_path
from apis.shared.files.models import ALLOWED_EXTENSIONS as _SAVEABLE_EXTENSIONS
from apis.shared.skills.bundle import slugify_skill_name

logger = logging.getLogger(__name__)

TOOL_NAME = "run_code"

# ---------------------------------------------------------------------------
# Limits (spec §4.4)
# ---------------------------------------------------------------------------

#: How long one run may take inside the sandbox before ``timeout`` ends it.
CALL_TIMEOUT_SECONDS = 120
#: The Runtime's own wait on the command, past which the sandbox is stopped.
#: Covers staging, the run and ``finish.py``.
CALL_GUARD_SECONDS = CALL_TIMEOUT_SECONDS + 45
MAX_CODE_BYTES = 64 * 1024
MAX_ARGV_BYTES = 4 * 1024
MAX_CALLS_PER_TURN = 20
MAX_INPUT_FILES_PER_TURN = 10
MAX_INPUT_BYTES_PER_TURN = 100 * 1024 * 1024
MAX_SKILL_BUNDLE_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_CHARS = 4_000
MAX_SAVED_FILES = 5
MAX_SAVED_FILE_BYTES = 25 * 1024 * 1024
MAX_IMAGES = 4
IMAGE_MAX_PIXELS = 1_150_000
IMAGE_MAX_EDGE = 1_568
#: base64 text per ``writeFiles`` call. 16 MB payloads worked on dev; this
#: leaves room.
WRITE_CHUNK_CHARS = 6 * 1024 * 1024

# An output can be saved as any type the app already stores and serves
# (``apis.shared.files.models.ALLOWED_EXTENSIONS``).

# ---------------------------------------------------------------------------
# Messages the model reads
# ---------------------------------------------------------------------------

NO_NETWORK_NOTE = (
    "The sandbox has no network access and cannot install packages, so code "
    "that downloads, calls an API or runs pip will fail or wait until it is "
    "stopped. Do not retry it."
)

_NOT_ISOLATED_MESSAGE = (
    "run_code is not available in this deployment: its Code Interpreter is not "
    "the isolated (no-network) one, so nothing was run. Tell the user an "
    "administrator needs to deploy the platform update that isolates it."
)

_CANNOT_CONFIRM_MESSAGE = (
    "run_code could not confirm that its sandbox is isolated, so nothing was "
    "run. This is usually temporary; try again in a moment."
)

_NO_CI_MESSAGE = (
    "run_code is not available: no Code Interpreter is configured for this "
    "deployment, so nothing was run."
)


class RunCodeError(Exception):
    """A problem the model can act on; the message is for the model."""


# ---------------------------------------------------------------------------
# The isolation check
# ---------------------------------------------------------------------------

_isolation_lock = threading.Lock()
_isolation_by_id: Dict[str, bool] = {}


def _network_mode(interpreter_id: str) -> str:
    """The interpreter's network mode, from the control plane. Blocking."""
    import boto3

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    response = client.get_code_interpreter(codeInterpreterId=interpreter_id)
    return str((response.get("networkConfiguration") or {}).get("networkMode") or "")


def _isolated_interpreter_id() -> Tuple[Optional[str], Optional[str]]:
    """``(interpreter id, None)`` when it is VPC-isolated, else ``(None, message)``.

    Blocking (env, maybe SSM, one control-plane read per process per id); call
    via ``asyncio.to_thread``. A confirmed answer is kept for the process; a
    failed read is not, so a transient error does not stick.
    """
    interpreter_id = _get_code_interpreter_id()
    if not interpreter_id:
        return None, _NO_CI_MESSAGE
    with _isolation_lock:
        known = _isolation_by_id.get(interpreter_id)
    if known is None:
        try:
            mode = _network_mode(interpreter_id)
        except Exception as exc:  # noqa: BLE001 - any failure means "don't run"
            logger.warning("run_code: interpreter network mode not readable: %s", exc)
            return None, _CANNOT_CONFIRM_MESSAGE
        known = mode == "VPC"
        if not known:
            logger.error("run_code: interpreter %s is in %s mode, not VPC; refusing", interpreter_id, mode or "?")
        with _isolation_lock:
            _isolation_by_id[interpreter_id] = known
    if not known:
        return None, _NOT_ISOLATED_MESSAGE
    return interpreter_id, None


def _reset_isolation_cache() -> None:
    """Forget confirmed interpreters (tests)."""
    with _isolation_lock:
        _isolation_by_id.clear()


# ---------------------------------------------------------------------------
# Per-sandbox state
# ---------------------------------------------------------------------------


@dataclass
class _SandboxState:
    """What ``run_code`` has put in one sandbox, kept on its pool entry."""

    calls: int = 0
    programs_written: bool = False
    #: skill slug -> sandbox folder its bundle was written to
    skills: Dict[str, str] = field(default_factory=dict)
    #: sandbox path of each staged skill file -> sha256 of its bytes
    hashes: Dict[str, str] = field(default_factory=dict)
    #: upload id -> sandbox path
    inputs: Dict[str, str] = field(default_factory=dict)
    input_bytes: int = 0

    def forget_staging(self) -> None:
        """Forget what was staged (the session was replaced); the call count stays."""
        self.programs_written = False
        self.skills.clear()
        self.hashes.clear()
        self.inputs.clear()
        self.input_bytes = 0


def _state_of(entry: Any) -> _SandboxState:
    state = getattr(entry, "_run_code_state", None)
    if state is None:
        state = _SandboxState()
        setattr(entry, "_run_code_state", state)
    return state


# ---------------------------------------------------------------------------
# Talking to the sandbox
# ---------------------------------------------------------------------------


def _stream_result(response: Dict[str, Any]) -> Tuple[bool, str, str, Optional[int]]:
    """``(is_error, stdout, stderr, exit_code)`` of one invoke response."""
    is_error = False
    stdout = ""
    stderr = ""
    exit_code: Optional[int] = None
    for event in response.get("stream", []) or []:
        result = event.get("result", {}) or {}
        is_error = is_error or bool(result.get("isError"))
        structured = result.get("structuredContent") or {}
        stdout += structured.get("stdout") or ""
        stderr += structured.get("stderr") or ""
        if structured.get("exitCode") is not None:
            exit_code = int(structured["exitCode"])
    return is_error, stdout, stderr, exit_code


def _file_bytes(response: Dict[str, Any]) -> Optional[bytes]:
    """The bytes of a ``readFiles`` response (raw or base64), or ``None``."""
    import base64

    for event in response.get("stream", []) or []:
        for block in (event.get("result", {}) or {}).get("content", []) or []:
            data = block.get("data")
            if data is None and isinstance(block.get("resource"), dict):
                data = block["resource"].get("blob")
            if data is None:
                continue
            return base64.b64decode(data) if isinstance(data, str) else bytes(data)
    return None


async def _write_text(entry: Any, files: List[Tuple[str, str]]) -> None:
    """Write UTF-8 text files into the sandbox in one call."""
    if not files:
        return
    response = await entry.invoke(
        "writeFiles", {"content": [{"path": path, "text": text} for path, text in files]}
    )
    if _stream_result(response)[0]:
        raise RunCodeError("Could not write files into the sandbox.")


async def _write_bytes(entry: Any, files: List[Tuple[str, bytes]]) -> None:
    """Write binary files: base64 text in chunks, then one decode in the sandbox."""
    import base64

    if not files:
        return
    pairs: List[List[str]] = []
    chunk: List[Tuple[str, str]] = []
    chunk_chars = 0
    for index, (dest, data) in enumerate(files):
        text = base64.b64encode(data).decode("ascii")
        staging = f"{programs.CONTROL_DIR}/stage/{index}.b64"
        if chunk and chunk_chars + len(text) > WRITE_CHUNK_CHARS:
            await _write_text(entry, chunk)
            chunk, chunk_chars = [], 0
        chunk.append((staging, text))
        chunk_chars += len(text)
        pairs.append([staging, dest])
    await _write_text(entry, chunk)
    await _write_text(entry, [(f"{programs.CONTROL_DIR}/decode.json", json.dumps(pairs))])
    response = await entry.invoke(
        "executeCommand", {"command": f"python3 {programs.CONTROL_DIR}/decode.py"}
    )
    if _stream_result(response)[0]:
        raise RunCodeError("Could not unpack files in the sandbox.")


async def _read_file(entry: Any, path: str) -> Optional[bytes]:
    return _file_bytes(await entry.invoke("readFiles", {"paths": [path]}))


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _check_arguments(
    code: Optional[str],
    skill_name: Optional[str],
    path: Optional[str],
    argv: Optional[List[str]],
    files: Optional[List[str]],
    show_images: Optional[List[str]],
) -> Tuple[List[str], List[str], List[str]]:
    """Validate and normalize; raises :class:`RunCodeError` with a fix."""
    has_code = bool(code and code.strip())
    has_script = bool(skill_name or path)
    if has_code == has_script:
        raise RunCodeError(
            "Pass exactly one of `code` (Python you wrote) or `skill_name` + `path` "
            "(a script that ships with an active skill)."
        )
    if has_script and not (skill_name and path):
        raise RunCodeError("A skill script needs both `skill_name` and `path`.")
    if has_code and len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RunCodeError(f"`code` is over {MAX_CODE_BYTES // 1024} KB. Put long programs in a skill's scripts/ folder.")
    argv_list = [str(a) for a in (argv or [])]
    if argv_list and not has_script:
        raise RunCodeError("`argv` is for a skill script; with `code`, read your inputs directly.")
    if len(json.dumps(argv_list).encode("utf-8")) > MAX_ARGV_BYTES:
        raise RunCodeError(f"`argv` is over {MAX_ARGV_BYTES // 1024} KB. Write long inputs to a file and pass its path.")
    file_list = [str(f).strip() for f in (files or []) if str(f).strip()]
    if len(file_list) > MAX_INPUT_FILES_PER_TURN:
        raise RunCodeError(f"At most {MAX_INPUT_FILES_PER_TURN} files can be copied in per turn.")
    image_list = [str(p).strip() for p in (show_images or []) if str(p).strip()]
    if len(image_list) > MAX_IMAGES:
        raise RunCodeError(f"`show_images` takes at most {MAX_IMAGES} paths. Ask only for the pages you need to see.")
    return argv_list, file_list, image_list


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _record_for(records: Iterable[Any], skill_name: str) -> Any:
    wanted = (skill_name or "").strip()
    for record in records or []:
        if slugify_skill_name(getattr(record, "skill_id", "")) == wanted:
            return record
    return None


def _may_run_scripts_of(record: Any, user_id: str) -> bool:
    """Whether a script of ``record`` may run for ``user_id`` before PR-2.

    The user's own skills and admin-catalog skills (``owner_id == "system"``,
    curated by the deployment's administrators) run. A skill another user
    wrote, reaching this turn through a shared Agent or project, waits for the
    approval card (spec D4).
    """
    owner = getattr(record, "owner_id", "system") or "system"
    return owner in (user_id, "system")


async def _stage_skill(entry: Any, state: _SandboxState, record: Any, slug: str) -> str:
    """Write a skill's whole bundle once per sandbox; returns its folder."""
    folder = state.skills.get(slug)
    if folder:
        return folder
    from apis.shared.skills.resource_store import get_skill_resource_store

    refs = list(getattr(record, "resources", None) or [])
    total = sum(int(getattr(r, "size", 0) or 0) for r in refs)
    if total > MAX_SKILL_BUNDLE_BYTES:
        raise RunCodeError(
            f"Skill '{slug}' is {total:,} bytes, over the {MAX_SKILL_BUNDLE_BYTES // (1024 * 1024)} MB a sandbox takes."
        )
    store = get_skill_resource_store()
    folder = f"{programs.SKILLS_DIR}/{slug}"
    fetched = await asyncio.gather(*(asyncio.to_thread(store.get, r.s3_key) for r in refs))
    files = [(f"{folder}/{_manifest_path(r)}", data) for r, data in zip(refs, fetched)]
    await _write_bytes(entry, files)
    for dest, data in files:
        state.hashes[dest] = hashlib.sha256(data).hexdigest()
    state.skills[slug] = folder
    return folder


async def _session_files(session_id: str, user_id: str) -> List[Any]:
    from apis.shared.files import FileStatus, get_file_upload_repository

    rows = await get_file_upload_repository().list_session_files(session_id, status=FileStatus.READY)
    return [r for r in rows if getattr(r, "user_id", None) == user_id]


async def _stage_inputs(
    entry: Any, state: _SandboxState, session_id: str, user_id: str, wanted: List[str]
) -> List[str]:
    """Copy the named conversation files into ``inputs/``; returns their paths."""
    if not wanted:
        return []
    from apis.shared.files.linked_files import readable_view

    rows = await _session_files(session_id, user_id)
    to_write: List[Tuple[str, bytes]] = []
    paths: List[str] = []
    taken = set(state.inputs.values())
    for name in wanted:
        lowered = name.lower()
        match = next((r for r in rows if r.upload_id == name), None) or next(
            (r for r in rows if r.filename.lower() == lowered), None
        )
        if match is None:
            available = ", ".join(sorted({r.filename for r in rows})[:20]) or "(none)"
            raise RunCodeError(f"No file '{name}' in this conversation. Files here: {available}")
        if match.upload_id in state.inputs:
            paths.append(state.inputs[match.upload_id])
            continue
        if len(state.inputs) + len(to_write) >= MAX_INPUT_FILES_PER_TURN:
            raise RunCodeError(f"At most {MAX_INPUT_FILES_PER_TURN} files can be copied in per turn.")
        view = await readable_view(match, reader=TOOL_NAME)
        if view is None:
            raise RunCodeError(f"'{match.filename}' is no longer available.")
        size = int(getattr(view, "size_bytes", 0) or 0)
        if state.input_bytes + size > MAX_INPUT_BYTES_PER_TURN:
            raise RunCodeError(f"Copying '{match.filename}' would pass {MAX_INPUT_BYTES_PER_TURN // (1024 * 1024)} MB of inputs this turn.")
        data = await asyncio.to_thread(_download_s3_bytes, view.s3_bucket, view.s3_key)
        safe = _input_name(match.filename)
        dest = f"{programs.INPUTS_DIR}/{safe}"
        if dest in taken:
            dest = f"{programs.INPUTS_DIR}/{match.upload_id[:8]}-{safe}"
        taken.add(dest)
        to_write.append((dest, data))
        state.inputs[match.upload_id] = dest
        state.input_bytes += len(data)
        paths.append(dest)
    await _write_bytes(entry, to_write)
    return paths


def _input_name(filename: str) -> str:
    """A filename safe as one path segment, keeping its extension."""
    base, ext = os.path.splitext(os.path.basename(filename or "file"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext)[:12]
    return f"{base[:100]}{ext}"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


async def _save_outputs(
    entry: Any, session_id: str, user_id: str, written: List[List[Any]]
) -> Tuple[List[str], List[str], List[str]]:
    """Save what the run wrote under ``outputs/``.

    Returns ``(cards, saved_lines, skipped_lines)``: download-card JSON per saved
    file, and one line each for the model.
    """
    cards: List[str] = []
    saved: List[str] = []
    skipped: List[str] = []
    for path, size in written:
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if len(saved) >= MAX_SAVED_FILES:
            skipped.append(f"{path}: over the {MAX_SAVED_FILES}-file limit per call")
            continue
        mime = _SAVEABLE_EXTENSIONS.get(ext)
        if not mime:
            skipped.append(f"{path}: {ext or 'no extension'} files can't be saved to the chat")
            continue
        if int(size) > MAX_SAVED_FILE_BYTES:
            skipped.append(f"{path}: {int(size):,} bytes, over the {MAX_SAVED_FILE_BYTES // (1024 * 1024)} MB limit")
            continue
        data = await _read_file(entry, path)
        if data is None:
            skipped.append(f"{path}: could not be read back")
            continue
        base = os.path.splitext(name)[0]
        filename = f"{_safe_document_name(base) or 'output'}{ext}"
        try:
            upload_id, size_kb = await _store_document(user_id, session_id, filename, data, mime)
        except StorageQuotaExceededError as exc:
            skipped.append(f"{path}: {exc}")
            continue
        cards.append(_download_card(filename, upload_id, size_kb, "Saved"))
        saved.append(f"{path} → {filename} ({size_kb}, upload id {upload_id})")
    return cards, saved, skipped


async def _preview_blocks(entry: Any, previews: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    blocks: List[Dict[str, Any]] = []
    lines: List[str] = []
    for item in previews[:MAX_IMAGES]:
        if item.get("error"):
            lines.append(f"{item.get('path')}: not shown ({item['error']})")
            continue
        data = await _read_file(entry, item["preview"])
        if data is None:
            lines.append(f"{item.get('path')}: not shown (could not be read back)")
            continue
        blocks.append({"image": {"format": "png", "source": {"bytes": data}}})
        lines.append(f"{item.get('path')}: shown below ({item.get('width')}×{item.get('height')})")
    return blocks, lines


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit_script_run(user_id: str, record: Any, script_path: str, sha256: str, outcome: str) -> None:
    """``skill.script_run``, as the Word tools write it. Blocking; never raises."""
    try:
        from apis.shared.audit.models import TARGET_SKILL, AuditAction
        from apis.shared.audit.service import get_audit_service
        from apis.shared.auth.models import User

        get_audit_service().record(
            action=AuditAction.SKILL_SCRIPT_RUN,
            actor=User(email="", user_id=user_id, name="", roles=[]),
            target_id=getattr(record, "skill_id", "") or "",
            target_type=TARGET_SKILL,
            changes=["scriptRun"],
            after={"scriptPath": script_path, "scriptSha256": sha256, "outcome": outcome, "tool": TOOL_NAME},
        )
    except Exception:  # noqa: BLE001 - an audit gap never fails the tool
        logger.exception("run_code: skill.script_run audit record could not be written")


def _audit_code_run(user_id: str, session_id: str, code: str, exit_code: Optional[int], duration_ms: int) -> None:
    """``sandbox.code_run``: ids, sizes and a hash; never the code. Blocking; never raises."""
    try:
        from apis.shared.audit.models import TARGET_CONVERSATION, AuditAction
        from apis.shared.audit.service import get_audit_service
        from apis.shared.auth.models import User

        data = code.encode("utf-8")
        get_audit_service().record(
            action=AuditAction.SANDBOX_CODE_RUN,
            actor=User(email="", user_id=user_id, name="", roles=[]),
            target_id=session_id,
            target_type=TARGET_CONVERSATION,
            changes=["codeRun"],
            after={
                "codeSha256": hashlib.sha256(data).hexdigest(),
                "codeBytes": len(data),
                "exitCode": exit_code,
                "durationMs": duration_ms,
            },
        )
    except Exception:  # noqa: BLE001 - an audit gap never fails the tool
        logger.exception("run_code: sandbox.code_run audit record could not be written")


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _error(text: str) -> Dict[str, Any]:
    return {"status": "error", "content": [{"text": text}]}


@dataclass
class _RunRecord:
    """What one call did, for its audit record."""

    attempted: bool = False
    exit_code: Optional[int] = None
    outcome: str = "error"
    sha256: str = ""


async def _run(
    session_id: str,
    user_id: str,
    skill_records: TurnSkillRecords,
    code: Optional[str],
    skill_name: Optional[str],
    path: Optional[str],
    argv: Optional[List[str]],
    files: Optional[List[str]],
    show_images: Optional[List[str]],
) -> Dict[str, Any]:
    try:
        argv_list, file_list, image_list = _check_arguments(code, skill_name, path, argv, files, show_images)
    except RunCodeError as exc:
        return _error(str(exc))

    record = None
    script_ref = None
    if skill_name:
        slug = skill_name.strip()
        try:
            script_ref = resolve_skill_script(skill_records.records, slug, path or "")
        except SkillScriptError as exc:
            return _error(str(exc))
        record = _record_for(skill_records.records, slug)
        if record is None or not _may_run_scripts_of(record, user_id):
            return _error(
                f"'{slug}' was written by another user, and run_code doesn't run other "
                "users' scripts yet, so nothing was run. Tell the user the script "
                "can run once they make their own copy of the skill."
            )

    interpreter_id, problem = await asyncio.to_thread(_isolated_interpreter_id)
    if problem:
        return _error(problem)

    entry = sandbox_pool.acquire(session_id, user_id)
    pooled = entry is not None
    if entry is None:
        entry = sandbox_pool.new_transient_entry(session_id, user_id)

    run = _RunRecord()
    started = time.monotonic()
    try:
        async with entry.lock:
            state = _state_of(entry)
            if pooled and state.calls >= MAX_CALLS_PER_TURN:
                return _error(
                    f"run_code has run {MAX_CALLS_PER_TURN} times this turn, its limit. "
                    "Finish with what you have, or ask the user to continue in a new message."
                )
            state.calls += 1
            try:
                return await asyncio.wait_for(
                    _run_in_sandbox(
                        entry, state, run, interpreter_id, session_id, user_id, record, script_ref,
                        code, argv_list, file_list, image_list,
                    ),
                    timeout=CALL_GUARD_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("run_code: guard expired after %ss; stopping the sandbox", CALL_GUARD_SECONDS)
                if pooled:
                    await sandbox_pool.evict(entry)
                else:
                    await entry.stop()
                return _error(
                    f"The run was stopped after {CALL_GUARD_SECONDS} seconds and its sandbox "
                    f"was reset, so files from earlier calls this turn are gone. {NO_NETWORK_NOTE}"
                )
            except RunCodeError as exc:
                return _error(str(exc))
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        if not pooled:
            await entry.stop()
        if run.attempted and script_ref is not None:
            await asyncio.to_thread(
                _audit_script_run, user_id, record, _manifest_path(script_ref), run.sha256, run.outcome
            )
        elif run.attempted and code:
            await asyncio.to_thread(_audit_code_run, user_id, session_id, code, run.exit_code, duration_ms)


async def _start_sandbox(entry: Any, state: _SandboxState, interpreter_id: str) -> bool:
    """Start the call's sandbox, with LibreOffice mounted when it is on; ``True`` if mounted.

    A session this turn's ``analyze_spreadsheet`` already started without the
    mount is replaced, since a mount can only be made at session start. A
    mount that fails is not fatal: the call runs without LibreOffice, and the
    mount is skipped for a few minutes (``office_mount.mark_failed``).
    """
    mount = await asyncio.to_thread(office_mount.session_mount)
    if not mount:
        await entry.start(interpreter_id)
        return bool(getattr(entry, "mounted", False))
    if getattr(entry, "started", False):
        if getattr(entry, "mounted", False):
            return True
        await entry.reset()
        state.forget_staging()
    try:
        await entry.start(interpreter_id, filesystem_configurations=mount)
        return True
    except Exception as exc:  # noqa: BLE001 - run without LibreOffice instead
        logger.warning("run_code: office mount failed, running without LibreOffice: %s", exc)
        office_mount.mark_failed()
        await entry.start(interpreter_id)
        return False


async def _run_in_sandbox(
    entry: Any,
    state: _SandboxState,
    run: _RunRecord,
    interpreter_id: str,
    session_id: str,
    user_id: str,
    record: Any,
    script_ref: Any,
    code: Optional[str],
    argv_list: List[str],
    file_list: List[str],
    image_list: List[str],
) -> Dict[str, Any]:
    office = await _start_sandbox(entry, state, interpreter_id)

    if not state.programs_written:
        await _write_text(entry, [
            (f"{programs.CONTROL_DIR}/snapshot.py", programs.SNAPSHOT_PY),
            (f"{programs.CONTROL_DIR}/entry.py", programs.ENTRY_PY),
            (f"{programs.CONTROL_DIR}/finish.py", programs.FINISH_PY),
            (f"{programs.CONTROL_DIR}/decode.py", programs.DECODE_PY),
        ])
        state.programs_written = True

    staged_inputs = await _stage_inputs(entry, state, session_id, user_id, file_list)

    if script_ref is not None:
        slug = slugify_skill_name(getattr(record, "skill_id", ""))
        folder = await _stage_skill(entry, state, record, slug)
        target = f"{folder}/{_manifest_path(script_ref)}"
        run.sha256 = state.hashes.get(target, "")
        ran = f"Ran {target}" + (f" with argv {json.dumps(argv_list)}" if argv_list else "")
    else:
        target = f"{programs.CONTROL_DIR}/snippet.py"
        await _write_text(entry, [(target, code or "")])
        ran = "Ran your code"

    request = {
        "target": target,
        "argv": argv_list,
        "show_images": image_list,
        "output_cap": MAX_OUTPUT_CHARS,
        "image_max_pixels": IMAGE_MAX_PIXELS,
        "image_max_edge": IMAGE_MAX_EDGE,
    }
    await _write_text(entry, [(f"{programs.CONTROL_DIR}/request.json", json.dumps(request))])

    run.attempted = True
    command = programs.run_command(
        CALL_TIMEOUT_SECONDS,
        office_mount.PROGRAM_DIR if office else None,
        office_mount.NODE_MODULES_DIR if office else None,
    )
    response = await entry.invoke("executeCommand", {"command": command})
    _, stdout, stderr, _ = _stream_result(response)
    result = _parse_result(stdout + "\n" + stderr)
    if result is None:
        raise RunCodeError("The sandbox did not report a result for this run.")

    exit_code = int(result.get("exit_code", -1))
    timed_out = exit_code in (124, 137)
    run.exit_code = exit_code
    run.outcome = "ok" if exit_code == 0 else ("timeout" if timed_out else "error")

    cards, saved, skipped = await _save_outputs(entry, session_id, user_id, result.get("written") or [])
    images, image_lines = await _preview_blocks(entry, result.get("previews") or [])

    lines = [
        f"{ran}: {'finished' if exit_code == 0 else 'failed'} (exit code {exit_code}).",
    ]
    if timed_out:
        lines.append(f"It was stopped after {CALL_TIMEOUT_SECONDS} seconds. {NO_NETWORK_NOTE}")
    if staged_inputs:
        lines.append("Inputs in the sandbox: " + ", ".join(staged_inputs))
    output = (result.get("output") or "").strip()
    if output:
        label = f"Output (last {MAX_OUTPUT_CHARS:,} characters)" if result.get("output_truncated") else "Output"
        lines.append(f"{label}:\n{output}")
    else:
        lines.append("It printed nothing.")
    if saved:
        lines.append("Saved to this chat's Files (a download card is already shown for each; do not write links):\n" + "\n".join(saved))
    if skipped:
        lines.append("Written but not saved:\n" + "\n".join(skipped))
    if image_lines:
        lines.append("Images:\n" + "\n".join(image_lines))

    content: List[Dict[str, Any]] = [{"text": "\n\n".join(lines)}]
    content.extend({"text": card} for card in cards)
    content.extend(images)
    return {"status": "success" if exit_code == 0 else "error", "content": content}


def _parse_result(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed(text.replace("\r\n", "\n").splitlines()):
        if programs.RESULT_MARKER in line:
            try:
                return json.loads(line.split(programs.RESULT_MARKER, 1)[1])
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def make_run_code_tool(session_id: str, user_id: str, skill_records: TurnSkillRecords) -> Any:
    """The ``run_code`` tool bound to one conversation and user.

    ``skill_records`` is filled by ``ChatAgent`` once the turn's skills are
    fetched (``bind_turn_skill_records``), as for the Word tools.
    """

    @tool(name=TOOL_NAME)
    async def run_code(
        code: Optional[str] = None,
        skill_name: Optional[str] = None,
        path: Optional[str] = None,
        argv: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        show_images: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run Python in an isolated sandbox: a script that ships with an active skill, or code you write.

        Use it when a skill's instructions say to run one of its scripts (for
        example `python scripts/fill_fillable_fields.py in.pdf fields.json out.pdf`)
        or to write a short program (pypdf, pdfplumber, reportlab, pandas,
        openpyxl, python-docx, python-pptx, Pillow, matplotlib and more are
        installed). Run a skill's script only with `skill_name` + `path` +
        `argv`: never copy it into `code`, and never start it from `code`
        (subprocess, runpy, import).

        The sandbox has NO network access and cannot install packages. Python
        3.12; poppler's pdftoppm/pdftotext are available, qpdf/pdftk/tesseract
        are not. Each call is a fresh process; files persist for the rest of
        this turn only. Working folder layout:
          inputs/          the conversation files you name in `files`
          skills/<name>/   a skill's whole folder (scripts/, references/, assets/),
                           copied in only when you run one of its scripts by
                           `skill_name` + `path`; until then it is not there
          outputs/         write results here; each new file is saved to the chat
                           with a download card (pdf, docx, xlsx, pptx, csv, txt,
                           md, html and images)

        Args:
            code: Python you wrote, run as a script from the working folder.
                Exactly one of `code` or `skill_name` + `path`.
            skill_name: An active skill's name (as in <available_skills>), to
                run one of its scripts.
            path: The script's path in that skill, e.g. "scripts/fill_fillable_fields.py".
                Only Python files under scripts/ can run.
            argv: Command-line arguments for the script, as the skill's
                instructions give them, with paths relative to the working
                folder, e.g. ["inputs/form.pdf", "outputs/fields.json"].
            files: Names (or upload ids) of this conversation's files to copy
                into inputs/ before the run.
            show_images: Up to 4 image paths (e.g. "outputs/page_1.png") to see
                after the run, downscaled. Each costs about 1,600 tokens, so ask
                only for what you need to check.

        Returns:
            Whether the run finished, its exit code and printed output (last
            4,000 characters), the files saved from outputs/, and any images
            asked for.
        """
        return await _run(session_id, user_id, skill_records, code, skill_name, path, argv, files, show_images)

    return attach_turn_skill_records(run_code, skill_records)
