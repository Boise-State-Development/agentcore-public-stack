"""Skill scripts the Word tools can run in Code Interpreter (``WORD_SKILL_SCRIPTS_ENABLED``).

A skill may ship a Python program under ``scripts/`` that does a job the model
would otherwise have to type out: a contract-redlining engine that applies a
playbook's tracked changes, say, at ~20K tokens. Without this, the model copies
the whole file into ``modify_word_document.python_code`` on every run, which is
a 20K-token silent generation before the tool even starts. With it, the model
names the file and the tool loads it into the sandbox by reference.

**Where it runs.** Only inside AWS Bedrock Code Interpreter, and in a
dedicated interpreter, not the one model-written python-docx code uses: the
skill-script interpreter is created with ``networkMode: SANDBOX`` (no public
internet), and a run is bounded by :data:`SKILL_SCRIPT_TIMEOUT_SECONDS`. Its id
is service-generated, so it is not in the Runtime's environment (the V2
budget); :func:`resolve_skill_script_interpreter_id` reads it from the SSM
parameter ``/<PROJECT_PREFIX>/agentcore/skill-script-interpreter-id`` on the
first run and keeps it for the process. If it cannot be resolved the tool
refuses: a skill script never falls back to the public interpreter.

Nothing here imports, ``exec``s or ``compile``s the script on the Runtime host:
this module reads its bytes from the skill-resources store and hands them to
the sandbox as a file. That is why a skill script stays inert everywhere else
(``read_skill_file`` still returns it as text with a notice).

**What can be run.** A resource of kind ``script`` with a ``.py`` filename,
from a skill bound to this turn. Not a ``reference``, whatever its text says:

* The kind is the author's declaration, made when the file is added and shown
  in the bundle listing (``scripts/…``), and it matches the agentskills.io
  convention that ``scripts/`` holds executable code. A marker or shebang
  sniffed from the text would be a second, invisible way to declare the same
  thing, and anyone reading the bundle listing would have no way to tell which
  references were runnable.
* It keeps the two tools' contracts apart: references are read, scripts run.

A reference is refused with a message that says how to fix it (re-add the file
under ``scripts/`` as ``.py``), because that is a one-step change for the
skill's author.

**Which skills.** Exactly the turn's effective skill records, the ones
``build_skills_runtime`` already fetched for ``read_skill_file`` and
``document_extract``. Resolution goes through ``_resolve_resource`` on those
records, so the only S3 key ever read is one a bound skill's manifest names;
the model cannot reach a skill outside the turn or a path outside a manifest.

The Word tools are built in the route (they need the request's identity) before
the agent builds its skills runtime, so they receive a :class:`TurnSkillRecords`
holder and :func:`bind_turn_skill_records` fills it from ``ChatAgent`` once the
records exist. A cached agent keeps the holder it was built with; the skills are
part of the agent-cache key, so those records are this configuration's records.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from apis.shared.skills.bundle import slugify_skill_name
from apis.shared.skills.bundle_files import MAX_RESOURCE_BYTES
from apis.shared.skills.resource_store import (
    SkillResourceStoreError,
    get_skill_resource_store,
)
from agents.main_agent.skills.strands_mapping import (
    _manifest_path,
    _manifest_paths,
    _resolve_resource,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_PARAMS_BYTES",
    "SKILL_SCRIPT_INTERPRETER_ENV",
    "SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX",
    "SKILL_SCRIPT_TIMEOUT_SECONDS",
    "SkillScript",
    "SkillScriptError",
    "TurnSkillRecords",
    "attach_turn_skill_records",
    "bind_turn_skill_records",
    "load_skill_script",
    "parse_script_params",
    "resolve_skill_script",
    "resolve_skill_script_interpreter_id",
    "skill_script_interpreter_parameter_name",
]

# ``params`` is written into the sandbox as a JSON file, and it is prompt text
# too: the model writes it, and it sits in the tool call in history for the
# life of the session. Sized for a contract's whole revision set (each edit's
# target, replacement and note, the redline's substance, which the model has to
# write whichever tool applies it); a whole playbook belongs in the skill.
MAX_PARAMS_BYTES = 65_536

# How long one skill-script run may take, from starting its sandbox session to
# reading the document back. On expiry the session is stopped and the tool
# returns an error; the session is also started with a server-side timeout a
# little past this, so it ends even if the stop call fails.
SKILL_SCRIPT_TIMEOUT_SECONDS = 120

# The skill-script interpreter's id. ``SKILL_SCRIPT_INTERPRETER_ENV`` is a
# local-development override only; deployed Runtimes read the SSM parameter,
# whose name is derived from PROJECT_PREFIX (CDK: SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX
# in code-interpreter-construct.ts; the two must match).
SKILL_SCRIPT_INTERPRETER_ENV = "AGENTCORE_SKILL_SCRIPT_INTERPRETER_ID"
SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX = "agentcore/skill-script-interpreter-id"

_interpreter_id_lock = threading.Lock()
_resolved_interpreter_id: Optional[str] = None

# Attribute a script-capable tool carries so ``ChatAgent`` can find its holder.
_HOLDER_ATTR = "_turn_skill_records"


class SkillScriptError(Exception):
    """A skill script could not be resolved or loaded; the message is for the model."""


@dataclass(frozen=True)
class SkillScript:
    """A resolved skill script: what ran (for the tool result) and its bytes."""

    skill_name: str
    path: str
    source: bytes
    # The catalog id of the skill the script came from, for the audit record.
    skill_id: str = ""

    @property
    def label(self) -> str:
        """``<skill>/<path>``, as the tool result names it."""
        return f"{self.skill_name}/{self.path}"


class TurnSkillRecords:
    """The turn's skill records, bound after the Word tools are built.

    Empty until :meth:`bind` runs. A tool that resolves against an unbound
    holder sees no skills, which is the right answer for an agent built
    without a skills runtime (no skills on the turn).
    """

    def __init__(self) -> None:
        self.records: List[Any] = []

    def bind(self, records: Optional[Iterable[Any]]) -> None:
        self.records = list(records or [])


def attach_turn_skill_records(tool_obj: Any, holder: TurnSkillRecords) -> Any:
    """Mark ``tool_obj`` as reading ``holder``; returns the tool."""
    setattr(tool_obj, _HOLDER_ATTR, holder)
    return tool_obj


def bind_turn_skill_records(tools: Iterable[Any], records: Optional[Iterable[Any]]) -> int:
    """Hand the turn's skill records to every tool that asked for them.

    An attribute scan over a list already in memory: no I/O, nothing
    measurable on the agent build. Returns how many holders were bound.
    """
    bound = 0
    materialized = list(records or [])
    for tool_obj in tools or []:
        holder = getattr(tool_obj, _HOLDER_ATTR, None)
        if isinstance(holder, TurnSkillRecords):
            holder.bind(materialized)
            bound += 1
    return bound


def resolve_skill_script(records: Iterable[Any], skill_name: str, path: str) -> Any:
    """The manifest entry for ``path`` in the bound skill ``skill_name``.

    Raises :class:`SkillScriptError` when the skill is not bound on this turn,
    the path is not in its manifest, or the entry is not a runnable script.
    """
    by_slug = {slugify_skill_name(getattr(r, "skill_id", "")): r for r in records or []}
    record = by_slug.get((skill_name or "").strip())
    if record is None:
        available = ", ".join(sorted(by_slug)) or "(none)"
        raise SkillScriptError(
            f"Skill '{skill_name}' is not available in this conversation. "
            f"Available skills: {available}"
        )

    ref = _resolve_resource(record, path)
    if ref is None:
        listing = ", ".join(_manifest_paths(record)) or "(no files)"
        raise SkillScriptError(
            f"No file '{path}' in skill '{skill_name}'. Available paths: {listing}"
        )

    kind = getattr(ref, "kind", "reference")
    shown = _manifest_path(ref)
    if kind != "script":
        raise SkillScriptError(
            f"'{shown}' is a {kind} file, not a script, so it cannot be run. Only "
            "Python files under scripts/ can be run; the skill's author can add "
            f"it again as scripts/{os.path.splitext(ref.filename)[0]}.py."
        )
    if not ref.filename.lower().endswith(".py"):
        raise SkillScriptError(
            f"'{shown}' is not a Python file. Only .py scripts can be run."
        )
    size = getattr(ref, "size", 0) or 0
    if size > MAX_RESOURCE_BYTES:
        raise SkillScriptError(
            f"'{shown}' is {size} bytes; a skill script can be at most "
            f"{MAX_RESOURCE_BYTES} bytes."
        )
    return ref


def load_skill_script(records: Iterable[Any], skill_name: str, path: str) -> SkillScript:
    """Resolve and fetch a skill script's bytes. Blocking (S3): use ``asyncio.to_thread``.

    The size is checked again on the bytes, not only on the manifest row, so a
    stale row cannot smuggle a larger object through.
    """
    ref = resolve_skill_script(records, skill_name, path)
    shown = _manifest_path(ref)
    try:
        source = get_skill_resource_store().get(ref.s3_key)
    except SkillResourceStoreError as exc:
        logger.warning("skill script %s/%s: %s", skill_name, shown, exc)
        raise SkillScriptError(f"Could not read '{shown}' from skill '{skill_name}'.") from exc
    if len(source) > MAX_RESOURCE_BYTES:
        raise SkillScriptError(
            f"'{shown}' is {len(source)} bytes; a skill script can be at most "
            f"{MAX_RESOURCE_BYTES} bytes."
        )
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillScriptError(f"'{shown}' is not UTF-8 text.") from exc
    return SkillScript(
        skill_name=skill_name.strip(),
        path=shown,
        source=source,
        skill_id=_record_skill_id(records, skill_name),
    )


def _record_skill_id(records: Iterable[Any], skill_name: str) -> str:
    """The catalog id of the bound skill ``skill_name`` activates (already resolved)."""
    wanted = (skill_name or "").strip()
    for record in records or []:
        skill_id = getattr(record, "skill_id", "")
        if slugify_skill_name(skill_id) == wanted:
            return skill_id
    return ""


def parse_script_params(params: Any) -> str:
    """``params`` as the JSON text the sandbox entry decodes.

    Must be an object (or absent, which is ``{}``) and at most
    :data:`MAX_PARAMS_BYTES` serialized.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise SkillScriptError("`params` must be an object (or omitted).")
    try:
        text = json.dumps(params, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SkillScriptError(f"`params` is not JSON-serializable: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_PARAMS_BYTES:
        raise SkillScriptError(
            f"`params` is too large (at most {MAX_PARAMS_BYTES} bytes as JSON). "
            "Put large data in the skill and pass a selector instead."
        )
    return text


def skill_script_interpreter_parameter_name() -> Optional[str]:
    """``/<PROJECT_PREFIX>/agentcore/skill-script-interpreter-id``, or ``None`` without a prefix."""
    prefix = (os.getenv("PROJECT_PREFIX") or "").strip()
    if not prefix:
        return None
    return f"/{prefix}/{SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX}"


def _read_interpreter_id_parameter(name: str) -> str:
    """The parameter's value. Blocking (SSM); raises on any failure."""
    import boto3

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    client = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    return client.get_parameter(Name=name)["Parameter"]["Value"]


def resolve_skill_script_interpreter_id() -> Optional[str]:
    """The no-network interpreter's id, or ``None`` when it cannot be found.

    Env override first (local development), then SSM, read once per process
    and kept. A failed read is not cached, so a parameter created after this
    process started (a backend deploy that ran before the platform deploy) is
    picked up on a later call. Blocking: call via ``asyncio.to_thread``. Runs
    inside the tool call, never before the first token.
    """
    global _resolved_interpreter_id
    override = (os.getenv(SKILL_SCRIPT_INTERPRETER_ENV) or "").strip()
    if override:
        return override
    if _resolved_interpreter_id:
        return _resolved_interpreter_id
    with _interpreter_id_lock:
        if _resolved_interpreter_id:
            return _resolved_interpreter_id
        name = skill_script_interpreter_parameter_name()
        if name is None:
            logger.warning("skill-script interpreter: PROJECT_PREFIX is not set")
            return None
        try:
            value = (_read_interpreter_id_parameter(name) or "").strip()
        except Exception as exc:  # noqa: BLE001 - any failure means "refuse"
            logger.warning("skill-script interpreter id not readable from %s: %s", name, exc)
            return None
        if not value:
            logger.warning("skill-script interpreter id parameter %s is empty", name)
            return None
        _resolved_interpreter_id = value
        return value


def _reset_interpreter_id_cache() -> None:
    """Forget the cached id (tests)."""
    global _resolved_interpreter_id
    with _interpreter_id_lock:
        _resolved_interpreter_id = None
