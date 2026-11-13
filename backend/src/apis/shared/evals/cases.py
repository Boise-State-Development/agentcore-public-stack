"""Load and validate golden-set case files (eval-harness spec §5).

One JSON file per family under ``backend/evals/cases/``, each an array of
cases. Parsing is strict: an unknown key is an error, not a silently ignored
typo, because a misspelt ``must_not_call`` would otherwise turn a guard into a
case that can never fail.

A case id is stable forever — baselines key on it — and is prefixed with its
family (``tool_selection.arithmetic_uses_calculator``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apis.shared.evals.judges import CASE_EVALUATORS

#: Families in report order. ``general_chat`` is judged, so it never gates
#: (spec D5); every other family is scored by a pure function and does.
FAMILIES: Tuple[str, ...] = (
    "tool_selection",
    "document_qa",
    "attachment_tools",
    "attachment_delivery",
    "project_kb",
    "agent_harness",
    "project_memory",
    "skill_outcome",
    "terminal",
    "usage_patterns",
    "retention",
    "instruction_following",
    "refusal",
    "skill_selection",
    "general_chat",
)
JUDGED_FAMILIES = frozenset({"general_chat"})
#: Families that report but never fail a run: the judged ones, and a
#: deterministic family under D18's ten-case floor (``terminal``), which gates
#: once it reaches ten.
#: ``usage_patterns`` (spec §18) measures what the costliest production usage
#: patterns cost to answer; it reports, and gates once its baselines have
#: settled across models.
ADVISORY_FAMILIES = JUDGED_FAMILIES | frozenset({"terminal", "attachment_delivery", "usage_patterns"})

#: The delivery measurement family (unified-attachment-delivery spec §8.4).
#: Advisory: its gating set is the cells where two arms are both legal for a
#: file.
DELIVERY_FAMILY = "attachment_delivery"
#: Families a run includes only when ``--family`` names them. The delivery
#: family attaches documents up to ~300k tokens, so a release or nightly pass
#: over "every family" must not start paying for it unannounced.
OPT_IN_FAMILIES = frozenset({DELIVERY_FAMILY})
#: What a run with no ``--family`` covers.
DEFAULT_FAMILIES: Tuple[str, ...] = tuple(f for f in FAMILIES if f not in OPT_IN_FAMILIES)

#: Delivery arms a case may name. ``auto`` is the turn path as it stands:
#: whatever the deployment decides for the file. Each other arm is the
#: planner's fixture-only ``delivery`` value, written on the file row by the
#: registrar (``fixtures.register_upload``), never sent on the request.
#: ``search`` (phase 5) indexes each file into the attachments KB whatever its
#: size and steers the conversation to ``document_read``'s ``query``; it needs
#: the ``token`` profile and the KB on the target, and the run waits for each
#: file to be searchable before the first turn (``fixtures.wait_for_searchable``).
DELIVERY_ARMS: Tuple[str, ...] = ("auto", "native", "text", "on_demand", "search")
#: Arms a case may not name yet. None since phase 5.
RESERVED_ARMS: Tuple[str, ...] = ()
#: The compaction ceiling a case runs under (§8.4): ``default`` is the
#: deployment's (a 100k cap) and ``200k`` a 200k cap. (Phase 0's third arm,
#: the 100k cap with §5.2's document subtraction, was deleted with the switch
#: once the readout dropped §5.2.) A
#: ceiling is a property of the Runtime, not of a request, so a run takes one
#: (``eval_harness.py run --ceiling``) and the target must be started with
#: :data:`CEILING_ENV`'s variables.
CEILINGS: Tuple[str, ...] = ("default", "200k")
RESERVED_CEILINGS: Tuple[str, ...] = ()
#: The Runtime environment each ceiling needs (``compaction_models.CompactionConfig``).
CEILING_ENV: Dict[str, Dict[str, str]] = {
    "default": {},
    "200k": {"AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS": "200000"},
}
#: Longest pause a turn may ask for before it is sent: past the 5-minute
#: prompt-cache TTL, short of the Runtime session's idle timeout.
MAX_PAUSE_SECONDS = 600

#: The strands-evals evaluators a case may name: exactly the ones the judge adapter maps.
JUDGE_EVALUATORS = CASE_EVALUATORS

_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_FIXTURE_RE = re.compile(r"^[a-z0-9_]+$")

#: Tools a case may require that are not catalog ids, and the catalog id that
#: provisions each (``apis/shared/tools/injected.py``). ``None`` means the turn
#: provisions it itself: ``document_read`` is injected whenever a turn carries
#: a document. Without this a case requiring ``read_powerpoint_presentation``
#: would fail the reachability check and ask the eval role for a grant no
#: catalog row has.
PROVISIONED_TOOLS: Dict[str, Optional[str]] = {
    "document_read": None,
    "project_files": None,
    "knowledge_files": None,
    # A project harness's memory tools (agents/builtin_tools/memory_spaces/project_tools.py).
    "memory_list": None,
    "memory_read": None,
    "memory_query": None,
    "memory_save": None,
    "memory_propose": None,
    # One catalog id provisions both artifact tools (``apis/shared/tools/injected.py``).
    "update_artifact": "create_artifact",
    "list_powerpoint_presentations": "create_powerpoint_presentation",
    "read_powerpoint_presentation": "create_powerpoint_presentation",
}


#: The prefix a client-declared tool carries on the wire (``apis.shared.client_tools.TOOL_NAME_PREFIX``).
CLIENT_TOOL_PREFIX = "local_"


def grant_for_tool(name: str) -> Optional[str]:
    """The catalog id that makes ``name`` reachable, or ``None`` when no grant is needed.

    A client tool (``local_<name>``) is declared by the request itself.
    """
    if name.startswith(CLIENT_TOOL_PREFIX):
        return None
    return PROVISIONED_TOOLS[name] if name in PROVISIONED_TOOLS else name


class CaseError(ValueError):
    """A case file that cannot be trusted to score anything."""


@dataclass(frozen=True)
class ToolExpect:
    must_call: Tuple[str, ...] = ()
    must_not_call: Tuple[str, ...] = ()
    in_order: Optional[Tuple[str, ...]] = None
    #: The most tool calls the last turn may make, which catches a search spree.
    max_calls: Optional[int] = None


@dataclass(frozen=True)
class RegexExpect:
    fullmatch: Optional[str] = None
    search: Tuple[str, ...] = ()
    absent: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillExpect:
    loads: Tuple[str, ...] = ()
    not_loads: Tuple[str, ...] = ()


#: The project memory scopes a check can read: the project's shared space and
#: the eval user's own space in that project (spec D15).
MEMORY_SCOPES = ("project", "mine")


@dataclass(frozen=True)
class MemoryExpect:
    """What one memory scope holds after the sample, read back through app-api."""

    scope: str
    contains_any: Tuple[str, ...] = ()
    contains_all: Tuple[str, ...] = ()
    absent: Tuple[str, ...] = ()
    #: The scope is exactly as the sample found it (``run.project_memory``'s seed, or empty).
    unchanged: bool = False


@dataclass(frozen=True)
class Expect:
    contains_any: Tuple[str, ...] = ()
    contains_all: Tuple[str, ...] = ()
    forbidden: Tuple[str, ...] = ()
    #: Like ``forbidden``, but matched only inside fenced code blocks: for a
    #: case whose failure is the model *writing* something (code it should
    #: refuse to write), where naming the same term in prose, as a refusal may,
    #: is not the failure.
    forbidden_in_code: Tuple[str, ...] = ()
    tools: Optional[ToolExpect] = None
    refuses: Optional[bool] = None
    regex: Optional[RegexExpect] = None
    skill: Optional[SkillExpect] = None
    memory: Tuple[MemoryExpect, ...] = ()
    #: How many proposals are pending on the project after the sample.
    proposals: Optional[int] = None

    def is_empty(self) -> bool:
        return not (
            self.contains_any
            or self.contains_all
            or self.forbidden
            or self.forbidden_in_code
            or self.tools
            or self.refuses is not None
            or self.regex
            or self.skill
            or self.memory
            or self.proposals is not None
        )


@dataclass(frozen=True)
class RunConfig:
    """Per-case turn configuration, a subset of ``InvocationRequest``.

    ``None`` means "the eval user's defaults" (every RBAC-granted tool or
    skill); an empty tuple means none. There is deliberately no model here:
    the model is a run parameter and the baseline is per model (spec D10).
    """

    enabled_tools: Optional[Tuple[str, ...]] = None
    enabled_skills: Optional[Tuple[str, ...]] = None
    #: A seeded fixture project or agent the turns run against (spec D13, D15),
    #: by its short name; the harness resolves it to the id sent as
    #: ``rag_assistant_id``. At most one of the two.
    project: Optional[str] = None
    agent: Optional[str] = None
    #: With ``project == EPHEMERAL_PROJECT``: memory to seed before the first turn,
    #: ``{scope: {slug: {"body": "- item\\n…", "description": "…"}}}``.
    project_memory: Optional[Mapping[str, Mapping[str, Mapping[str, str]]]] = None
    #: With ``project == EPHEMERAL_PROJECT``: the second eval member's role, which
    #: a turn ``"as": "member2"`` needs (spec D16).
    member2_role: Optional[str] = None
    #: ``InvocationRequest.client_surface`` (``"terminal"`` for the terminal client).
    client_surface: Optional[str] = None
    #: Tools the "client" declares, each with the answer the harness gives when
    #: the model calls it: ``{name, description, input_schema, respond}``,
    #: ``respond`` being ``{"output": str, "is_error": bool}`` or ``{"declined": true}``.
    client_tools: Optional[Tuple[Mapping[str, Any], ...]] = None
    #: ``attachment_delivery`` only: the delivery arm (``DELIVERY_ARMS``) and the
    #: compaction ceiling (``CEILINGS``) the case runs under.
    arm: str = "auto"
    ceiling: str = "default"


#: A project of the sample's own, created before its first turn and purged after
#: it (spec D14): what the memory cases run in, so no sample sees another's saves.
EPHEMERAL_PROJECT = "@ephemeral"
MEMBER_ROLES = ("editor", "viewer")
TURN_USERS = ("eval", "member2")


@dataclass(frozen=True)
class TurnMeta:
    """Where one turn runs: which numbered session, as which eval user."""

    session: int = 1
    as_user: str = "eval"
    #: Seconds to wait before sending this turn (``attachment_delivery``'s
    #: paced cases: a pause past the prompt-cache TTL).
    pause_seconds: int = 0
    #: Fixture files attached on this turn (``attachment_delivery`` only), as a
    #: user attaching documents partway through a conversation would; the
    #: case's own ``files`` arrive with the first turn.
    files: Tuple[str, ...] = ()


#: The generated reference documents a case may attach (``large_documents``).
#: Built deterministically by ``scripts/large_document_quality/fixtures.py``
#: (``BUILDERS``; a test pins the two lists together), each over the 4 MB
#: inline limit, so a turn reaches them through ``document_read`` the way it
#: reaches a user's large upload.
LARGE_DOCUMENTS: Tuple[str, ...] = ("manual", "scanned", "brochure", "handbook")


@dataclass(frozen=True)
class JudgeSpec:
    evaluators: Tuple[str, ...] = ()
    rubric: Optional[str] = None


@dataclass(frozen=True)
class EvalCase:
    id: str
    family: str
    turns: Tuple[str, ...]
    run: RunConfig
    expect: Expect
    judge: JudgeSpec
    files: Tuple[str, ...]
    notes: str
    #: Generated reference documents (``LARGE_DOCUMENTS``), attached by reference
    #: and prepared by the deployed pipeline before the first turn.
    large_documents: Tuple[str, ...] = ()
    #: One per turn, parallel to ``turns``.
    turn_meta: Tuple[TurnMeta, ...] = ()

    def meta_for(self, index: int) -> TurnMeta:
        return self.turn_meta[index] if index < len(self.turn_meta) else TurnMeta()

    @property
    def all_files(self) -> Tuple[str, ...]:
        """Every fixture file the case attaches: its own, then each turn's, in order."""
        return self.files + tuple(name for meta in self.turn_meta for name in meta.files)

    @property
    def gating(self) -> bool:
        return self.family not in ADVISORY_FAMILIES


def _check_keys(raw: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    if not isinstance(raw, Mapping):
        raise CaseError(f"{where}: expected an object")
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise CaseError(f"{where}: unknown key(s) {unknown}")


def _str_tuple(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise CaseError(f"{where}: expected a list of non-empty strings")
    return tuple(value)


def _optional_str_tuple(value: Any, where: str) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    return _str_tuple(value, where)


def _compile(pattern: str, where: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise CaseError(f"{where}: invalid regex /{pattern}/: {exc}") from exc


def _parse_expect(raw: Mapping[str, Any], where: str) -> Expect:
    _check_keys(
        raw, ("contains_any", "contains_all", "forbidden", "forbidden_in_code", "tools", "refuses", "regex", "skill",
              "memory", "proposals"),
        where,
    )

    tools = None
    if raw.get("tools") is not None:
        t = raw["tools"]
        _check_keys(t, ("must_call", "must_not_call", "in_order", "max_calls"), f"{where}.tools")
        max_calls = t.get("max_calls")
        if max_calls is not None and (not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 0):
            raise CaseError(f"{where}.tools.max_calls: expected a non-negative integer")
        tools = ToolExpect(
            must_call=_str_tuple(t.get("must_call"), f"{where}.tools.must_call"),
            must_not_call=_str_tuple(t.get("must_not_call"), f"{where}.tools.must_not_call"),
            in_order=_optional_str_tuple(t.get("in_order"), f"{where}.tools.in_order"),
            max_calls=max_calls,
        )
        if max_calls is not None and max_calls < len(set(tools.must_call)):
            raise CaseError(f"{where}.tools.max_calls: lower than the number of tools it must call")
        clash = set(tools.must_call) & set(tools.must_not_call)
        if clash:
            raise CaseError(f"{where}.tools: {sorted(clash)} is both required and forbidden")

    regex = None
    if raw.get("regex") is not None:
        r = raw["regex"]
        _check_keys(r, ("fullmatch", "search", "absent"), f"{where}.regex")
        fullmatch = r.get("fullmatch")
        if fullmatch is not None and not isinstance(fullmatch, str):
            raise CaseError(f"{where}.regex.fullmatch: expected a string")
        regex = RegexExpect(
            fullmatch=fullmatch,
            search=_str_tuple(r.get("search"), f"{where}.regex.search"),
            absent=_str_tuple(r.get("absent"), f"{where}.regex.absent"),
        )
        for pattern in ([fullmatch] if fullmatch else []) + list(regex.search) + list(regex.absent):
            _compile(pattern, f"{where}.regex")

    skill = None
    if raw.get("skill") is not None:
        s = raw["skill"]
        _check_keys(s, ("loads", "not_loads"), f"{where}.skill")
        skill = SkillExpect(
            loads=_str_tuple(s.get("loads"), f"{where}.skill.loads"),
            not_loads=_str_tuple(s.get("not_loads"), f"{where}.skill.not_loads"),
        )

    refuses = raw.get("refuses")
    if refuses is not None and not isinstance(refuses, bool):
        raise CaseError(f"{where}.refuses: expected true, false or null")

    memory: List[MemoryExpect] = []
    memory_raw = raw.get("memory")
    if memory_raw is not None:
        if not isinstance(memory_raw, list) or not memory_raw:
            raise CaseError(f"{where}.memory: expected a list of checks")
        for i, m in enumerate(memory_raw):
            at = f"{where}.memory[{i}]"
            _check_keys(m, ("scope", "contains_any", "contains_all", "absent", "unchanged"), at)
            if m.get("scope") not in MEMORY_SCOPES:
                raise CaseError(f"{at}.scope: expected one of {', '.join(MEMORY_SCOPES)}")
            check = MemoryExpect(
                scope=m["scope"],
                contains_any=_str_tuple(m.get("contains_any"), f"{at}.contains_any"),
                contains_all=_str_tuple(m.get("contains_all"), f"{at}.contains_all"),
                absent=_str_tuple(m.get("absent"), f"{at}.absent"),
                unchanged=bool(m.get("unchanged", False)),
            )
            if not (check.contains_any or check.contains_all or check.absent or check.unchanged):
                raise CaseError(f"{at}: a memory check must check something")
            memory.append(check)
    proposals = raw.get("proposals")
    if proposals is not None and (not isinstance(proposals, int) or isinstance(proposals, bool) or proposals < 0):
        raise CaseError(f"{where}.proposals: expected a non-negative integer")

    return Expect(
        contains_any=_str_tuple(raw.get("contains_any"), f"{where}.contains_any"),
        contains_all=_str_tuple(raw.get("contains_all"), f"{where}.contains_all"),
        forbidden=_str_tuple(raw.get("forbidden"), f"{where}.forbidden"),
        forbidden_in_code=_str_tuple(raw.get("forbidden_in_code"), f"{where}.forbidden_in_code"),
        tools=tools,
        refuses=refuses,
        regex=regex,
        skill=skill,
        memory=tuple(memory),
        proposals=proposals,
    )


def parse_case(raw: Mapping[str, Any], *, family: str) -> EvalCase:
    """One case from its JSON object; raises :class:`CaseError` on anything doubtful."""
    case_id = raw.get("id") if isinstance(raw, Mapping) else None
    where = f"{family}:{case_id or '?'}"
    _check_keys(raw, ("id", "family", "turns", "run", "expect", "judge", "files", "large_documents", "notes"), where)

    if not isinstance(case_id, str) or not _ID_RE.match(case_id):
        raise CaseError(f"{where}: id must look like '<family>.<snake_case_name>'")
    if raw.get("family") != family or not case_id.startswith(family + "."):
        raise CaseError(f"{where}: family must be '{family}' and prefix the id")

    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise CaseError(f"{where}.turns: expected at least one turn")
    turns: List[str] = []
    turn_meta: List[TurnMeta] = []
    session_users: Dict[int, str] = {}
    for i, turn in enumerate(turns_raw):
        _check_keys(turn, ("prompt", "session", "as", "pause_seconds", "files"), f"{where}.turns[{i}]")
        prompt = turn.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CaseError(f"{where}.turns[{i}].prompt: expected text")
        session = turn.get("session", 1)
        if not isinstance(session, int) or isinstance(session, bool) or session < 1:
            raise CaseError(f"{where}.turns[{i}].session: expected a positive integer")
        as_user = turn.get("as", "eval")
        if as_user not in TURN_USERS:
            raise CaseError(f"{where}.turns[{i}].as: expected one of {', '.join(TURN_USERS)}")
        # A conversation belongs to one user; a second member opens their own.
        if session_users.setdefault(session, as_user) != as_user:
            raise CaseError(f"{where}.turns[{i}]: session {session} already belongs to {session_users[session]}")
        pause = turn.get("pause_seconds", 0)
        if not isinstance(pause, int) or isinstance(pause, bool) or not 0 <= pause <= MAX_PAUSE_SECONDS:
            raise CaseError(f"{where}.turns[{i}].pause_seconds: expected 0..{MAX_PAUSE_SECONDS}")
        if pause and family != DELIVERY_FAMILY:
            raise CaseError(f"{where}.turns[{i}].pause_seconds: only {DELIVERY_FAMILY} cases pause")
        turn_files = _str_tuple(turn.get("files"), f"{where}.turns[{i}].files")
        if turn_files and (family != DELIVERY_FAMILY or session != 1):
            raise CaseError(f"{where}.turns[{i}].files: only {DELIVERY_FAMILY} cases attach on a later turn, in session 1")
        turns.append(prompt)
        turn_meta.append(TurnMeta(session=session, as_user=as_user, pause_seconds=pause, files=turn_files))

    run_raw = raw.get("run") or {}
    _check_keys(
        run_raw,
        ("enabled_tools", "enabled_skills", "project", "agent", "project_memory", "member2_role", "client_surface",
         "client_tools", "arm", "ceiling"),
        f"{where}.run",
    )
    for key, allowed, reserved in (("arm", DELIVERY_ARMS, RESERVED_ARMS), ("ceiling", CEILINGS, RESERVED_CEILINGS)):
        value = run_raw.get(key)
        if value is None:
            continue
        if family != DELIVERY_FAMILY:
            raise CaseError(f"{where}.run.{key}: only {DELIVERY_FAMILY} cases name a {key}")
        if value in reserved:
            raise CaseError(f"{where}.run.{key}: '{value}' is reserved")
        if value not in allowed:
            raise CaseError(f"{where}.run.{key}: expected one of {', '.join(allowed + reserved)}")
    client_surface = run_raw.get("client_surface")
    if client_surface is not None and client_surface not in ("terminal", "web"):
        raise CaseError(f"{where}.run.client_surface: expected 'terminal' or 'web'")
    client_tools = run_raw.get("client_tools")
    if client_tools is not None:
        if not isinstance(client_tools, list) or not client_tools:
            raise CaseError(f"{where}.run.client_tools: expected a list of tools")
        for i, tool in enumerate(client_tools):
            at = f"{where}.run.client_tools[{i}]"
            _check_keys(tool, ("name", "description", "input_schema", "respond"), at)
            if not isinstance(tool.get("name"), str) or not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$", tool["name"]):
                raise CaseError(f"{at}.name: expected an identifier")
            if not isinstance(tool.get("description"), str) or not tool["description"].strip():
                raise CaseError(f"{at}.description: expected text")
            respond = tool.get("respond")
            if not isinstance(respond, Mapping) or not (respond.get("declined") is True or isinstance(respond.get("output"), str)):
                raise CaseError(f"{at}.respond: expected {{output, is_error}} or {{declined: true}}")
    for key in ("project", "agent"):
        value = run_raw.get(key)
        if key == "project" and value == EPHEMERAL_PROJECT:
            continue
        if value is not None and (not isinstance(value, str) or not _FIXTURE_RE.match(value)):
            raise CaseError(f"{where}.run.{key}: expected a fixture name like 'kb_small'")
    if run_raw.get("project") and run_raw.get("agent"):
        raise CaseError(f"{where}.run: name a project or an agent, not both")
    ephemeral = run_raw.get("project") == EPHEMERAL_PROJECT
    project_memory = run_raw.get("project_memory")
    if project_memory is not None:
        if not ephemeral:
            raise CaseError(f"{where}.run.project_memory: only a project of its own ('{EPHEMERAL_PROJECT}') can be seeded")
        _check_keys(project_memory, MEMORY_SCOPES, f"{where}.run.project_memory")
        for scope, entries in project_memory.items():
            if not isinstance(entries, Mapping) or not entries:
                raise CaseError(f"{where}.run.project_memory.{scope}: expected {{slug: {{body, description}}}}")
            for slug, entry in entries.items():
                _check_keys(entry, ("body", "description"), f"{where}.run.project_memory.{scope}.{slug}")
                if not isinstance(entry.get("body"), str) or not entry["body"].strip():
                    raise CaseError(f"{where}.run.project_memory.{scope}.{slug}.body: expected text")
    member2_role = run_raw.get("member2_role")
    if member2_role is not None and (not ephemeral or member2_role not in MEMBER_ROLES):
        raise CaseError(f"{where}.run.member2_role: one of {', '.join(MEMBER_ROLES)}, on a '{EPHEMERAL_PROJECT}' project")
    if any(m.as_user == "member2" for m in turn_meta) and member2_role is None:
        raise CaseError(f"{where}: a turn as member2 needs run.member2_role")
    run = RunConfig(
        enabled_tools=_optional_str_tuple(run_raw.get("enabled_tools"), f"{where}.run.enabled_tools"),
        enabled_skills=_optional_str_tuple(run_raw.get("enabled_skills"), f"{where}.run.enabled_skills"),
        project=run_raw.get("project"),
        agent=run_raw.get("agent"),
        project_memory=project_memory,
        member2_role=member2_role,
        client_surface=client_surface,
        client_tools=tuple(client_tools) if client_tools else None,
        arm=run_raw.get("arm") or "auto",
        ceiling=run_raw.get("ceiling") or "default",
    )

    expect = _parse_expect(raw.get("expect") or {}, f"{where}.expect")
    if (expect.memory or expect.proposals is not None) and not ephemeral:
        raise CaseError(f"{where}.expect: memory and proposals are read from a project of its own ('{EPHEMERAL_PROJECT}')")

    judge_raw = raw.get("judge") or {}
    _check_keys(judge_raw, ("evaluators", "rubric"), f"{where}.judge")
    judge = JudgeSpec(
        evaluators=_str_tuple(judge_raw.get("evaluators"), f"{where}.judge.evaluators"),
        rubric=judge_raw.get("rubric"),
    )
    unknown_evaluators = sorted(set(judge.evaluators) - JUDGE_EVALUATORS)
    if unknown_evaluators:
        raise CaseError(f"{where}.judge.evaluators: unknown {unknown_evaluators}")

    if "refusal" in judge.evaluators and expect.refuses is None:
        raise CaseError(f"{where}: the refusal judge needs expect.refuses to orient its score")

    if family in JUDGED_FAMILIES:
        if not judge.evaluators and not judge.rubric:
            raise CaseError(f"{where}: a judged family's case must name an evaluator or a rubric")
    elif expect.is_empty():
        raise CaseError(f"{where}: a gating family's case must declare at least one deterministic check")

    if run.enabled_tools is not None and expect.tools is not None:
        unreachable = sorted(
            name for name in expect.tools.must_call
            if grant_for_tool(name) is not None and grant_for_tool(name) not in run.enabled_tools
        )
        if unreachable:
            raise CaseError(f"{where}: must_call {unreachable} is not in run.enabled_tools")
    if run.enabled_skills is not None and expect.skill is not None:
        unreachable = sorted(set(expect.skill.loads) - set(run.enabled_skills))
        if unreachable:
            raise CaseError(f"{where}: skill.loads {unreachable} is not in run.enabled_skills")

    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        raise CaseError(f"{where}.notes: expected text")
    large_documents = _str_tuple(raw.get("large_documents"), f"{where}.large_documents")
    unknown_documents = sorted(set(large_documents) - set(LARGE_DOCUMENTS))
    if unknown_documents:
        raise CaseError(f"{where}.large_documents: unknown {unknown_documents} (known: {', '.join(LARGE_DOCUMENTS)})")

    return EvalCase(
        id=case_id,
        family=family,
        turns=tuple(turns),
        run=run,
        expect=expect,
        judge=judge,
        files=_str_tuple(raw.get("files"), f"{where}.files"),
        notes=notes,
        large_documents=large_documents,
        turn_meta=tuple(turn_meta),
    )


def load_family(path: Path) -> List[EvalCase]:
    family = path.stem
    if family not in FAMILIES:
        raise CaseError(f"{path.name}: unknown family '{family}' (known: {', '.join(FAMILIES)})")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise CaseError(f"{path.name}: expected a JSON array of cases")
    return [parse_case(item, family=family) for item in raw]


def load_cases(
    cases_dir: Path,
    *,
    families: Optional[Sequence[str]] = None,
    fixtures_dir: Optional[Path] = None,
    generated: Iterable[str] = (),
) -> List[EvalCase]:
    """Every case under ``cases_dir`` (optionally only ``families``), in family then file order.

    Ids must be unique across the whole set. With ``fixtures_dir``, every file a
    case names must exist there or be one of ``generated``, the fixtures the
    harness builds rather than reads (``scripts/eval_fixtures``).
    """
    generated = frozenset(generated)
    wanted = set(families) if families else None
    if wanted:
        unknown = sorted(wanted - set(FAMILIES))
        if unknown:
            raise CaseError(f"unknown famil{'y' if len(unknown) == 1 else 'ies'} {unknown}")

    cases: List[EvalCase] = []
    seen: Dict[str, str] = {}
    for path in sorted(cases_dir.glob("*.json"), key=lambda p: FAMILIES.index(p.stem) if p.stem in FAMILIES else 99):
        if wanted and path.stem not in wanted:
            continue
        for case in load_family(path):
            if case.id in seen:
                raise CaseError(f"duplicate case id {case.id}")
            seen[case.id] = path.name
            cases.append(case)

    if fixtures_dir is not None:
        for case in cases:
            for name in case.all_files:
                if name not in generated and not (fixtures_dir / name).is_file():
                    raise CaseError(f"{case.id}: fixture file '{name}' not found in {fixtures_dir}")
    return cases


def fixture_keys(cases: Iterable[EvalCase]) -> List[str]:
    """``"project:<name>"`` / ``"agent:<name>"`` for every seeded fixture these cases run against."""
    keys = set()
    for case in cases:
        if case.run.project and case.run.project != EPHEMERAL_PROJECT:
            keys.add(f"project:{case.run.project}")
        if case.run.agent:
            keys.add(f"agent:{case.run.agent}")
    return sorted(keys)


def fixture_key(case: EvalCase) -> Optional[str]:
    if case.run.project and case.run.project != EPHEMERAL_PROJECT:
        return f"project:{case.run.project}"
    if case.run.agent:
        return f"agent:{case.run.agent}"
    return None


def required_grants(cases: Iterable[EvalCase]) -> Dict[str, List[str]]:
    """The tools and skills the eval user's role must grant for these cases to be meaningful."""
    tools: set = set()
    skills: set = set()
    for case in cases:
        tools.update(case.run.enabled_tools or ())
        if case.expect.tools is not None:
            tools.update(g for g in map(grant_for_tool, case.expect.tools.must_call) if g)
        skills.update(case.run.enabled_skills or ())
        if case.expect.skill is not None:
            skills.update(case.expect.skill.loads)
    return {"tools": sorted(tools), "skills": sorted(skills)}
