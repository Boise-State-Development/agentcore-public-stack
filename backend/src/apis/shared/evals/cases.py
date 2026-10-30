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

from apis.shared.evals.judges import EVALUATOR_CLASSES

#: Families in report order. ``general_chat`` is judged, so it never gates
#: (spec D5); every other family is scored by a pure function and does.
FAMILIES: Tuple[str, ...] = (
    "tool_selection",
    "document_qa",
    "retention",
    "instruction_following",
    "refusal",
    "skill_selection",
    "general_chat",
)
JUDGED_FAMILIES = frozenset({"general_chat"})

#: The strands-evals evaluators a case may name: exactly the ones the judge adapter maps.
JUDGE_EVALUATORS = frozenset(EVALUATOR_CLASSES)

_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class CaseError(ValueError):
    """A case file that cannot be trusted to score anything."""


@dataclass(frozen=True)
class ToolExpect:
    must_call: Tuple[str, ...] = ()
    must_not_call: Tuple[str, ...] = ()
    in_order: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class RegexExpect:
    fullmatch: Optional[str] = None
    search: Tuple[str, ...] = ()
    absent: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillExpect:
    loads: Tuple[str, ...] = ()
    not_loads: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Expect:
    contains_any: Tuple[str, ...] = ()
    contains_all: Tuple[str, ...] = ()
    forbidden: Tuple[str, ...] = ()
    tools: Optional[ToolExpect] = None
    refuses: Optional[bool] = None
    regex: Optional[RegexExpect] = None
    skill: Optional[SkillExpect] = None

    def is_empty(self) -> bool:
        return not (
            self.contains_any
            or self.contains_all
            or self.forbidden
            or self.tools
            or self.refuses is not None
            or self.regex
            or self.skill
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

    @property
    def gating(self) -> bool:
        return self.family not in JUDGED_FAMILIES


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
    _check_keys(raw, ("contains_any", "contains_all", "forbidden", "tools", "refuses", "regex", "skill"), where)

    tools = None
    if raw.get("tools") is not None:
        t = raw["tools"]
        _check_keys(t, ("must_call", "must_not_call", "in_order"), f"{where}.tools")
        tools = ToolExpect(
            must_call=_str_tuple(t.get("must_call"), f"{where}.tools.must_call"),
            must_not_call=_str_tuple(t.get("must_not_call"), f"{where}.tools.must_not_call"),
            in_order=_optional_str_tuple(t.get("in_order"), f"{where}.tools.in_order"),
        )
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

    return Expect(
        contains_any=_str_tuple(raw.get("contains_any"), f"{where}.contains_any"),
        contains_all=_str_tuple(raw.get("contains_all"), f"{where}.contains_all"),
        forbidden=_str_tuple(raw.get("forbidden"), f"{where}.forbidden"),
        tools=tools,
        refuses=refuses,
        regex=regex,
        skill=skill,
    )


def parse_case(raw: Mapping[str, Any], *, family: str) -> EvalCase:
    """One case from its JSON object; raises :class:`CaseError` on anything doubtful."""
    case_id = raw.get("id") if isinstance(raw, Mapping) else None
    where = f"{family}:{case_id or '?'}"
    _check_keys(raw, ("id", "family", "turns", "run", "expect", "judge", "files", "notes"), where)

    if not isinstance(case_id, str) or not _ID_RE.match(case_id):
        raise CaseError(f"{where}: id must look like '<family>.<snake_case_name>'")
    if raw.get("family") != family or not case_id.startswith(family + "."):
        raise CaseError(f"{where}: family must be '{family}' and prefix the id")

    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise CaseError(f"{where}.turns: expected at least one turn")
    turns: List[str] = []
    for i, turn in enumerate(turns_raw):
        _check_keys(turn, ("prompt",), f"{where}.turns[{i}]")
        prompt = turn.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CaseError(f"{where}.turns[{i}].prompt: expected text")
        turns.append(prompt)

    run_raw = raw.get("run") or {}
    _check_keys(run_raw, ("enabled_tools", "enabled_skills"), f"{where}.run")
    run = RunConfig(
        enabled_tools=_optional_str_tuple(run_raw.get("enabled_tools"), f"{where}.run.enabled_tools"),
        enabled_skills=_optional_str_tuple(run_raw.get("enabled_skills"), f"{where}.run.enabled_skills"),
    )

    expect = _parse_expect(raw.get("expect") or {}, f"{where}.expect")

    judge_raw = raw.get("judge") or {}
    _check_keys(judge_raw, ("evaluators", "rubric"), f"{where}.judge")
    judge = JudgeSpec(
        evaluators=_str_tuple(judge_raw.get("evaluators"), f"{where}.judge.evaluators"),
        rubric=judge_raw.get("rubric"),
    )
    unknown_evaluators = sorted(set(judge.evaluators) - JUDGE_EVALUATORS)
    if unknown_evaluators:
        raise CaseError(f"{where}.judge.evaluators: unknown {unknown_evaluators}")

    if family in JUDGED_FAMILIES:
        if not judge.evaluators and not judge.rubric:
            raise CaseError(f"{where}: a judged family's case must name an evaluator or a rubric")
    elif expect.is_empty():
        raise CaseError(f"{where}: a gating family's case must declare at least one deterministic check")

    if run.enabled_tools is not None and expect.tools is not None:
        unreachable = sorted(set(expect.tools.must_call) - set(run.enabled_tools))
        if unreachable:
            raise CaseError(f"{where}: must_call {unreachable} is not in run.enabled_tools")
    if run.enabled_skills is not None and expect.skill is not None:
        unreachable = sorted(set(expect.skill.loads) - set(run.enabled_skills))
        if unreachable:
            raise CaseError(f"{where}: skill.loads {unreachable} is not in run.enabled_skills")

    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        raise CaseError(f"{where}.notes: expected text")

    return EvalCase(
        id=case_id,
        family=family,
        turns=tuple(turns),
        run=run,
        expect=expect,
        judge=judge,
        files=_str_tuple(raw.get("files"), f"{where}.files"),
        notes=notes,
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
) -> List[EvalCase]:
    """Every case under ``cases_dir`` (optionally only ``families``), in family then file order.

    Ids must be unique across the whole set. With ``fixtures_dir``, every file a
    case names must exist there.
    """
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
            for name in case.files:
                if not (fixtures_dir / name).is_file():
                    raise CaseError(f"{case.id}: fixture file '{name}' not found in {fixtures_dir}")
    return cases


def required_grants(cases: Iterable[EvalCase]) -> Dict[str, List[str]]:
    """The tools and skills the eval user's role must grant for these cases to be meaningful."""
    tools: set = set()
    skills: set = set()
    for case in cases:
        tools.update(case.run.enabled_tools or ())
        if case.expect.tools is not None:
            tools.update(case.expect.tools.must_call)
        skills.update(case.run.enabled_skills or ())
        if case.expect.skill is not None:
            skills.update(case.expect.skill.loads)
    return {"tools": sorted(tools), "skills": sorted(skills)}
