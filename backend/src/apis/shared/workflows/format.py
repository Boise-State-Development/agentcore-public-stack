"""A saved workflow: its steps, what each reads, and how the result comes back.

``docs/specs/workflow-designer.md`` §3, §9.1 and §20.3 (PR-D.1). A workflow is
one markdown file in a skill's reference bundle, ``references/workflow.<name>.md``,
so it inherits the skill's ownership, visibility, role grants, versions and
bindings and needs no table of its own (D1). The server owns the format in both
directions: the designer edits the structured :class:`Workflow` and never parses
or writes the file.

THE GRAMMAR
-----------
::

    ---
    title: Proposal review          (required, one line)
    version: 3                      (a positive integer; the server moves it on a change)
    files: one                      (none | one | any; any when absent)
    result: card                    (card | merge; card when absent)
    ---

    Prose here is kept, and nothing reads it.

    | step       | kind      | worker                | reads            |
    |------------|-----------|-----------------------|------------------|
    | generalist | agent     | AST-123 @ 5           | request, files   |
    | fiscal     | agent     | AST-456               | request, files   |
    | summary    | prompt    | us.anthropic.claude-… | files, fiscal    |
    | audit      | checklist | us.anthropic.claude-… | files            |

    ## Request

    The request the sheet opens with. Any length, any number of lines.

    ## Step: fiscal

    This step's own request, when it is not the shared one.

    ## Step: audit

    Field sets: budget-caps, compliance

    Guidance appended to every one of the step's calls (optional).

    ## Result

    Reads: generalist, fiscal

    The merge instruction, when ``result: merge``.

- **The table** is the steps, in authored order. It is found by its header: a
  row naming the ``step``, ``kind`` and ``worker`` columns. A table of anything
  else before it is prose. Any other column is kept, cell by cell, and nothing
  reads it. ``kind`` is ``agent``, ``prompt`` or ``checklist``. An agent's
  ``worker`` is its id, and ``@ <n>`` records the published version it had when
  the step was added or last reviewed (§10, D6). A prompt step (§21) is one
  model call with no Agent: its ``worker`` is a model id, with no version, and
  its ``## Step:`` section is the instruction. A checklist step (§22) runs named
  field sets over the run's files, one model call per set: its ``worker`` is a
  model id too, and its ``## Step:`` section opens with a ``Field sets:`` line
  naming them (see below). The parser accepts both whatever the flags say; run
  create is what refuses one while ``PROMPT_STEPS_ENABLED`` (or
  ``CHECKLIST_STEPS_ENABLED``) is off. Coordinates are never stored: the canvas
  arranges itself.
- **``reads`` is the whole graph:** ``request``, ``files``, and the names of other
  steps, comma-separated. It is a set, so it is held and written in one order:
  ``request``, ``files``, then steps in table order.
- **Sections** start at a line ``## Request``, ``## Result`` or
  ``## Step: <name>`` and run to the next of those. Each appears at most once, and
  their bodies are verbatim, so an instruction may hold its own tables. Text after
  the table and before the first section is kept as prose.
- **``## Result``** opens with an optional ``Reads:`` line naming steps; the rest is
  the instruction. Without one, the result takes every step.
- **A checklist step's ``## Step:`` section** is required and opens with a
  ``Field sets:`` line: one or more field-set names, comma-separated, in the
  order the step runs them. The rest of the section is guidance, appended to
  every one of the step's calls. A checklist step reads ``files``, and may read
  ``request`` and earlier steps too.

THE ENVELOPE
------------
The parser refuses, with a message naming the problem: more than
:data:`MAX_STEPS` steps (``WORKFLOW_RUN_MAX_STEPS``), a step named twice, a read
of a step that does not exist, a step reading itself, and any cycle. A checklist
step must name between one and :data:`MAX_CHECKLIST_FIELD_SETS` field sets, none
twice, and read ``files``. A file that breaks one is refused on every skill-file
write, whatever the flags say (:func:`validate_workflow_upload`), so a broken
file cannot collect quietly.

What a checklist step's sets *are* is a fact about the skill, not the file, so
it is checked by :func:`check_field_set_refs` against the skill's own field sets,
which the write paths hand it: each set must be
``references/field-set.<name>.md`` in the same skill, and none may hold a
``list`` field (§22.3: ``record_checklist``'s ``value`` is a string or null).

PURITY
------
Standard library only, like ``extraction.field_sets``: unit-testable without a
fixture and importable from app-api, the Runtime and a Lambda alike. Nothing
here reads DynamoDB or S3, and nothing here is on a turn's path. Field sets are
read duck-typed (``.fields``, each with ``.name`` and ``.type``) rather than
imported, because importing ``apis.shared.extraction`` loads its Pydantic layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field as dataclass_field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The envelope's step limit. Equal to ``apis.shared.handoff.runs.WORKFLOW_RUN_MAX_STEPS``
#: (a test pins the two together); copied rather than imported so this module
#: stays standard-library only.
MAX_STEPS = 8

#: A request's limit, equal to ``apis.shared.handoff.models.HANDOFF_REQUEST_MAX_CHARS``
#: (pinned by a test, for the same reason).
MAX_REQUEST_CHARS = 20_000

#: ``references/workflow.<name>.md``, at the top of ``references/`` only.
WORKFLOW_PREFIX = "workflow."
WORKFLOW_SUFFIX = ".md"

KIND_AGENT = "agent"
KIND_PROMPT = "prompt"
KIND_CHECKLIST = "checklist"
#: The kinds a step can be today, in the order a message lists them.
STEP_KINDS = (KIND_AGENT, KIND_PROMPT, KIND_CHECKLIST)
#: Steps that are model calls with no Agent: their ``worker`` is a model id.
MODEL_STEP_KINDS = frozenset({KIND_PROMPT, KIND_CHECKLIST})
#: Kinds the format will carry once they ship. Named here so a file holding one
#: is refused with a sentence, not as an unknown word. None are pending today.
RESERVED_KINDS: frozenset = frozenset()

#: The most field sets one checklist step runs (§22.3). The compliance audit is
#: 15 sets and the envelope is 8 steps, so several sets share one step.
MAX_CHECKLIST_FIELD_SETS = 16
#: The field type a checklist step can't carry yet (§22.3): ``record_checklist``'s
#: ``value`` is a string or null. Equal to ``extraction.field_sets.TYPE_LIST``
#: (pinned by a test).
CHECKLIST_REFUSED_FIELD_TYPE = "list"

READ_REQUEST = "request"
READ_FILES = "files"
#: What a step can read besides other steps. Neither can be a step's name.
INPUT_READS = (READ_REQUEST, READ_FILES)

FILES_NONE = "none"
FILES_ONE = "one"
FILES_ANY = "any"
FILES_MODES = (FILES_NONE, FILES_ONE, FILES_ANY)

RESULT_CARD = "card"
RESULT_MERGE = "merge"
RESULT_MODES = (RESULT_CARD, RESULT_MERGE)

_WORKFLOW_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STEP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
#: A Bedrock model id or inference-profile id (``us.anthropic.claude-…-v1:0``), or
#: an ARN. Never whitespace, ``|`` (it ends a cell) or ``@`` (it starts a version).
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_WORKER_RE = re.compile(r"^(?P<id>[^@\s]+)\s*(?:@\s*(?P<version>\S+))?$")
#: A field set's name, as ``extraction.field_sets`` reads one from
#: ``field-set.<name>.md`` (pinned by a test).
_FIELD_SET_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FIELD_SETS_LINE_RE = re.compile(r"^field[ \t]+sets:(?P<rest>.*)$", re.IGNORECASE)
_SECTION_RE = re.compile(
    r"^##[ \t]+(?:(?P<request>request)|(?P<result>result)|step:[ \t]*(?P<step>\S*))[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_READS_LINE_RE = re.compile(r"^reads:(?P<rest>.*)$", re.IGNORECASE)

_MAX_TITLE_CHARS = 200


class WorkflowError(ValueError):
    """A workflow file or definition that cannot be read or written.

    The message names what is wrong and where; it is what the author reads, as
    a 400 from a route or a tool error from ``my_skill_write_file``.
    """


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowStep:
    """One step: an Agent (``kind="agent"``), one model call (``kind="prompt"``),
    or one model call per field set (``kind="checklist"``).

    A prompt or checklist step has no Agent, so its ``agent_id`` is ``""`` and
    its ``agent_version`` ``None``; its worker is ``model_id``. An Agent step's
    ``model_id`` is ``None``: its model is the Agent's own. A checklist step's
    ``request`` is its guidance, and ``field_sets`` the sets it runs.
    """

    #: ``snake_case``, unique in the workflow. What other steps read it by.
    name: str
    agent_id: str
    #: The Agent's ``listing.published_version`` when the step was added or last
    #: reviewed; ``None`` for an Agent with nothing published (§10, D6).
    agent_version: Optional[int] = None
    #: In canonical order: ``request``, ``files``, then steps in table order.
    reads: Tuple[str, ...] = INPUT_READS
    #: This step's own request; ``None`` means the shared one.
    request: Optional[str] = None
    kind: str = KIND_AGENT
    #: A prompt or checklist step's model id; ``None`` for an Agent step.
    model_id: Optional[str] = None
    #: A checklist step's field-set names, in the order it runs them; ``()`` otherwise.
    field_sets: Tuple[str, ...] = ()

    @property
    def is_prompt(self) -> bool:
        return self.kind == KIND_PROMPT

    @property
    def is_checklist(self) -> bool:
        return self.kind == KIND_CHECKLIST

    @property
    def is_model_step(self) -> bool:
        """A prompt or checklist step: model calls with no Agent, whose worker is a model id."""
        return self.kind in MODEL_STEP_KINDS

    @property
    def worker(self) -> str:
        """What the ``worker`` cell names: the Agent's id, or a model step's model id."""
        return (self.model_id or "") if self.is_model_step else self.agent_id


@dataclass(frozen=True)
class WorkflowResult:
    """What comes back to the conversation that started the run."""

    mode: str = RESULT_CARD
    #: Step names; ``None`` means every step.
    reads: Optional[Tuple[str, ...]] = None
    instruction: Optional[str] = None


@dataclass(frozen=True)
class Workflow:
    """A saved run shape."""

    name: str
    title: str
    version: int = 1
    files: str = FILES_ANY
    #: The request the sheet opens with; ``None`` for none.
    request: Optional[str] = None
    steps: Tuple[WorkflowStep, ...] = ()
    result: WorkflowResult = dataclass_field(default_factory=WorkflowResult)

    def step(self, name: str) -> Optional[WorkflowStep]:
        return next((s for s in self.steps if s.name == name), None)

    @property
    def result_steps(self) -> Tuple[str, ...]:
        """The steps the result takes, in table order."""
        if self.result.reads is None:
            return tuple(s.name for s in self.steps)
        return self.result.reads


@dataclass(frozen=True)
class WorkflowLayout:
    """What a workflow file carries besides the workflow itself.

    Kept apart from :class:`Workflow` so the definition's equality, and its
    fingerprint, are only what a run uses. A structured save re-applies the
    stored file's layout (:func:`carry_layout`), so a hand-written note survives
    the designer.
    """

    #: Prose between the frontmatter and the table.
    preamble: Optional[str] = None
    #: Prose after the table and before the first section.
    postscript: Optional[str] = None
    #: Frontmatter lines the workflow does not read, as written.
    frontmatter: Tuple[str, ...] = ()
    #: Headers of the table's unread columns, in authored order.
    unmanaged_columns: Tuple[str, ...] = ()
    #: One tuple per step, in step order, of that row's unread cells.
    unmanaged_cells: Tuple[Tuple[str, ...], ...] = ()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def workflow_filename(name: str) -> str:
    return f"{WORKFLOW_PREFIX}{name}{WORKFLOW_SUFFIX}"


def workflow_name_for(path_or_filename: str) -> Optional[str]:
    """The workflow name a bundle filename declares, or ``None``."""
    tail = (path_or_filename or "").strip().rsplit("/", 1)[-1]
    if not tail.startswith(WORKFLOW_PREFIX) or not tail.endswith(WORKFLOW_SUFFIX):
        return None
    name = tail[len(WORKFLOW_PREFIX) : -len(WORKFLOW_SUFFIX)]
    return name if _WORKFLOW_NAME_RE.match(name) else None


def workflow_name_of_ref(ref: object) -> Optional[str]:
    """The workflow name a manifest entry declares, or ``None``.

    A workflow is a ``reference`` at the top of ``references/``; the same name in
    a subfolder, or as another kind, is an ordinary file.
    """
    if (getattr(ref, "kind", "reference") or "reference") != "reference":
        return None
    filename = getattr(ref, "filename", "") or ""
    path = getattr(ref, "path", None) or filename
    if path != filename:
        return None
    return workflow_name_for(filename)


def require_workflow_name(name: str) -> str:
    """``name`` if it is a valid workflow name, else the naming rule."""
    if not _WORKFLOW_NAME_RE.match(name or "") or len(name) > 100:
        raise WorkflowError(
            f"workflow name {name!r} must be lowercase words joined by hyphens, "
            "e.g. 'workflow.proposal-review.md'"
        )
    return name


def slug_for_title(title: str, *, taken: Sequence[str] = ()) -> str:
    """A workflow name from a title, unique among ``taken``."""
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60].strip("-")
    base = base or "workflow"
    candidate, n = base, 2
    used = set(taken)
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def step_name_for(label: str, *, taken: Sequence[str] = ()) -> str:
    """A step name from a label (an Agent's name), unique among ``taken``."""
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:40].strip("_")
    if not base or not base[0].isalpha():
        base = f"step_{base}" if base else "step"
    if base in INPUT_READS:
        base = f"{base}_step"
    candidate, n = base, 2
    used = set(taken)
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


def _check_request(text: Optional[str], *, what: str, name: str) -> None:
    if text is not None and len(text) > MAX_REQUEST_CHARS:
        raise WorkflowError(
            f"workflow '{name}': {what} is {len(text):,} characters; the limit is "
            f"{MAX_REQUEST_CHARS:,}"
        )


def check_envelope(workflow: Workflow) -> None:
    """Refuse a definition the envelope does not allow, naming the problem.

    Run by the parser on every file and by the serializer on every definition,
    so a file on disk and a definition on the wire obey one set of rules.
    """
    name = workflow.name
    if not workflow.title.strip():
        raise WorkflowError(f"workflow '{name}' needs a title")
    if len(workflow.title) > _MAX_TITLE_CHARS:
        raise WorkflowError(
            f"workflow '{name}': the title is longer than {_MAX_TITLE_CHARS} characters"
        )
    if not isinstance(workflow.version, int) or isinstance(workflow.version, bool) or workflow.version < 1:
        raise WorkflowError(f"workflow '{name}': version must be a positive whole number")
    if workflow.files not in FILES_MODES:
        raise WorkflowError(
            f"workflow '{name}': files must be one of {', '.join(FILES_MODES)}, "
            f"not {workflow.files!r}"
        )
    if workflow.result.mode not in RESULT_MODES:
        raise WorkflowError(
            f"workflow '{name}': result must be one of {', '.join(RESULT_MODES)}, "
            f"not {workflow.result.mode!r}"
        )
    _check_request(workflow.request, what="the request", name=name)
    _check_request(workflow.result.instruction, what="the result's instruction", name=name)

    if len(workflow.steps) > MAX_STEPS:
        raise WorkflowError(
            f"workflow '{name}' has {len(workflow.steps)} steps; a workflow holds at most "
            f"{MAX_STEPS}. Remove {len(workflow.steps) - MAX_STEPS} to save it"
        )

    names: Dict[str, int] = {}
    for index, step in enumerate(workflow.steps):
        _check_kind(step.kind, step=step.name, name=name)
        if not _STEP_NAME_RE.match(step.name or ""):
            raise WorkflowError(
                f"workflow '{name}': step name {step.name!r} must be snake_case, starting "
                "with a letter, at most 64 characters"
            )
        if step.name in INPUT_READS:
            raise WorkflowError(
                f"workflow '{name}': a step cannot be named {step.name!r}, which is what "
                "reading the run's own input is called"
            )
        if step.name in names:
            raise WorkflowError(
                f"workflow '{name}': two steps are named '{step.name}'; each step needs "
                "its own name, because that is what other steps read it by"
            )
        names[step.name] = index
        if step.is_model_step:
            _check_model_worker(step, name=name)
        elif step.model_id is not None:
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': an Agent step runs on its Agent's "
                "own model, so it names no model"
            )
        elif not _AGENT_ID_RE.match(step.agent_id or ""):
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': {step.agent_id!r} is not an Agent id"
            )
        if step.agent_version is not None and (
            not isinstance(step.agent_version, int)
            or isinstance(step.agent_version, bool)
            or step.agent_version < 1
        ):
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': the Agent's version must be a "
                "positive whole number"
            )
        _check_request(step.request, what=f"step '{step.name}''s request", name=name)
        _check_field_set_names(step, name=name)

    for step in workflow.steps:
        if len(set(step.reads)) != len(step.reads):
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}' reads the same thing twice"
            )
        for read in step.reads:
            if read in INPUT_READS:
                continue
            if read == step.name:
                raise WorkflowError(
                    f"workflow '{name}', step '{step.name}' reads its own result"
                )
            if read not in names:
                raise WorkflowError(
                    f"workflow '{name}', step '{step.name}' reads '{read}', which is not "
                    "a step in this workflow"
                )
        if step.is_checklist and READ_FILES not in step.reads:
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': a checklist step runs its field "
                "sets over the run's files, so it must read 'files'"
            )

    _check_acyclic(workflow)

    reads = workflow.result.reads
    if reads is not None:
        if not reads:
            raise WorkflowError(
                f"workflow '{name}': the result reads no step. Name at least one, or "
                "leave out the Reads line to take every step"
            )
        if len(set(reads)) != len(reads):
            raise WorkflowError(f"workflow '{name}': the result reads one step twice")
        for read in reads:
            if read not in names:
                raise WorkflowError(
                    f"workflow '{name}': the result reads '{read}', which is not a step "
                    "in this workflow"
                )


def _check_kind(kind: str, *, step: str, name: str) -> None:
    if kind in RESERVED_KINDS:
        raise WorkflowError(
            f"workflow '{name}', step '{step}': '{kind}' steps are not available "
            f"yet. Only {_kinds_listed()} steps can be saved today"
        )
    if kind not in STEP_KINDS:
        raise WorkflowError(
            f"workflow '{name}', step '{step}': unknown kind {kind!r}. "
            f"Expected {_kinds_listed('or')}"
        )


def _kinds_listed(conjunction: str = "and") -> str:
    quoted = [f"'{kind}'" for kind in STEP_KINDS]
    return ", ".join(quoted[:-1]) + f" {conjunction} {quoted[-1]}" if len(quoted) > 1 else quoted[0]


def _check_model_worker(step: WorkflowStep, *, name: str) -> None:
    """A prompt or checklist step names a model and nothing of an Agent's."""
    if step.agent_id or step.agent_version is not None:
        raise WorkflowError(
            f"workflow '{name}', step '{step.name}': a {step.kind} step has no Agent, so "
            "it names no Agent or Agent version"
        )
    if not _MODEL_ID_RE.match(step.model_id or ""):
        raise WorkflowError(
            f"workflow '{name}', step '{step.name}': {step.model_id!r} is not a model id"
        )


def _check_field_set_names(step: WorkflowStep, *, name: str) -> None:
    """A checklist step names 1–16 field sets, each once; no other kind names any."""
    if not step.is_checklist:
        if step.field_sets:
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': only a checklist step runs field sets"
            )
        return
    sets = step.field_sets
    if not sets:
        raise WorkflowError(
            f"workflow '{name}', step '{step.name}': a checklist step names at least one "
            "field set, on a 'Field sets:' line opening its '## Step:' section"
        )
    if len(sets) > MAX_CHECKLIST_FIELD_SETS:
        raise WorkflowError(
            f"workflow '{name}', step '{step.name}' names {len(sets)} field sets; a checklist "
            f"step runs at most {MAX_CHECKLIST_FIELD_SETS}. Split them across two steps"
        )
    seen = set()
    for set_name in sets:
        if not _FIELD_SET_NAME_RE.match(set_name or ""):
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}': {set_name!r} is not a field-set name. "
                "A field set is named by its file, 'field-set.<name>.md': lowercase words "
                "joined by hyphens"
            )
        if set_name in seen:
            raise WorkflowError(
                f"workflow '{name}', step '{step.name}' names the field set '{set_name}' twice"
            )
        seen.add(set_name)


def checklist_field_set_names(workflow: Workflow) -> Tuple[str, ...]:
    """Every field set the workflow's checklist steps run, once each, in first-named order."""
    names: List[str] = []
    for step in workflow.steps:
        for set_name in step.field_sets if step.is_checklist else ():
            if set_name not in names:
                names.append(set_name)
    return tuple(names)


def field_set_ref_problem(set_name: str, field_set: Any) -> Optional[str]:
    """Why a checklist step can't run ``field_set``, or ``None`` when it can.

    ``field_set`` is the skill's parsed set of that name (anything with ``.fields``,
    each with ``.name`` and ``.type``), or ``None`` when the skill has none. The
    sentence names the set and, for a ``list`` field, the field.
    """
    if field_set is None:
        return (
            f"the field set '{set_name}' is not in this skill. Add "
            f"'references/field-set.{set_name}.md' to the skill, or name another set"
        )
    for field in getattr(field_set, "fields", ()) or ():
        if getattr(field, "type", None) == CHECKLIST_REFUSED_FIELD_TYPE:
            return (
                f"the field set '{set_name}' has a list field, '{getattr(field, 'name', '')}', "
                "which a checklist step can't fill yet. Change its type, or run the set "
                "another way"
            )
    return None


def check_field_set_refs(workflow: Workflow, field_sets: Mapping[str, Any]) -> None:
    """Refuse a checklist step whose field set the skill doesn't have, or can't run (§22.3).

    ``field_sets`` maps a set name to the skill's parsed set (anything with
    ``.fields``, each with ``.name`` and ``.type``); a name it lacks, or maps to
    ``None``, is a set the skill doesn't have. Every skill-file write path calls
    this on a workflow with a checklist step, whatever the flags say; run create
    re-checks at the sheet, because a set can change after the workflow is saved.

    Raises:
        WorkflowError: naming the step and the set.
    """
    for step in workflow.steps:
        if not step.is_checklist:
            continue
        for set_name in step.field_sets:
            problem = field_set_ref_problem(set_name, field_sets.get(set_name))
            if problem is not None:
                raise WorkflowError(f"workflow '{workflow.name}', step '{step.name}': {problem}")


def canonical_reads(reads: Sequence[str], steps: Sequence[WorkflowStep]) -> Tuple[str, ...]:
    """``reads`` in the one order the format holds: inputs, then steps in table order.

    A name that is neither stays at the end in its given order, so the envelope
    check can name it rather than lose it.
    """
    order = {read: index for index, read in enumerate(INPUT_READS)}
    for index, step in enumerate(steps):
        order.setdefault(step.name, len(INPUT_READS) + index)
    known = sorted((r for r in reads if r in order), key=order.__getitem__)
    return tuple(known) + tuple(r for r in reads if r not in order)


def _check_acyclic(workflow: Workflow) -> None:
    """Refuse a cycle, naming the steps on it."""
    edges = {
        step.name: [r for r in step.reads if r not in INPUT_READS] for step in workflow.steps
    }
    state: Dict[str, int] = {}
    path: List[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt) :] + [nxt]
                raise WorkflowError(
                    f"workflow '{workflow.name}': steps read each other in a circle "
                    f"({' → '.join(cycle)}), so none of them could start"
                )
            if state.get(nxt) is None:
                visit(nxt)
        path.pop()
        state[node] = 2

    for step in workflow.steps:
        if state.get(step.name) is None:
            visit(step.name)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_WORKFLOW_KEYS = frozenset({"title", "version", "files", "result"})

#: Column headers, mapped to what they set. Matched case-insensitively.
_COLUMN_ALIASES: Dict[str, str] = {
    "step": "step",
    "kind": "kind",
    "worker": "worker",
    "reads": "reads",
}
_REQUIRED_COLUMNS = ("step", "kind", "worker")


def _split_frontmatter(text: str, *, name: str) -> Tuple[Dict[str, str], Tuple[str, ...], str]:
    """``(frontmatter, other_lines, body)``. ``key: value`` lines, not YAML."""
    lines = text.splitlines()
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first >= len(lines) or lines[first].strip() != "---":
        raise WorkflowError(
            f"workflow '{name}' has no frontmatter. It starts with a '---' block "
            "holding at least its title"
        )
    meta: Dict[str, str] = {}
    other: List[str] = []
    for index in range(first + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            return meta, tuple(other), "\n".join(lines[index + 1 :])
        if not stripped:
            continue
        key, colon, value = stripped.partition(":")
        lowered = key.strip().lower()
        if lowered == "request" and colon:
            raise WorkflowError(
                f"workflow '{name}': the request is a '## Request' section, not a "
                "frontmatter line, because a request can span lines"
            )
        if stripped.startswith("#") or not colon or lowered not in _WORKFLOW_KEYS:
            other.append(stripped)
            continue
        if lowered in meta:
            raise WorkflowError(f"workflow '{name}': '{lowered}' is set twice in the frontmatter")
        meta[lowered] = value.strip()
    raise WorkflowError(f"workflow '{name}': frontmatter opened with '---' but was never closed")


def _prose(lines: Sequence[str]) -> Optional[str]:
    text = "\n".join(lines).strip()
    return text or None


def _row_cells(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_delimiter_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= set("-: ") and "-" in cell for cell in cells)


def _split_sections(body: str, *, name: str) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """``(before_sections, {"request"|"result": body}, {step_name: body})``.

    Split before the table is read, so a section holding a table of its own is
    never mistaken for the step table.
    """
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return body, {}, {}
    head = body[: matches[0].start()]
    named: Dict[str, str] = {}
    steps: Dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[match.end() : end].strip()
        if match.group("request") is not None or match.group("result") is not None:
            key = "request" if match.group("request") is not None else "result"
            if key in named:
                raise WorkflowError(f"workflow '{name}' has two '## {key.title()}' sections")
            named[key] = text
            continue
        step = (match.group("step") or "").strip()
        if not step:
            raise WorkflowError(f"workflow '{name}': a '## Step:' section names no step")
        if step in steps:
            raise WorkflowError(f"workflow '{name}' has two '## Step: {step}' sections")
        steps[step] = text
    return head, named, steps


def _parse_version(raw: Optional[str], *, what: str, name: str) -> Optional[int]:
    if raw is None or raw == "":
        return None
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        raise WorkflowError(f"workflow '{name}': {what} must be a positive whole number, got {raw!r}")
    return int(raw)


def _parse_reads(raw: str) -> Tuple[str, ...]:
    return tuple(part.strip().strip("`") for part in raw.split(",") if part.strip())


def _split_field_sets(
    section: Optional[str], *, step: str, name: str
) -> Tuple[Tuple[str, ...], Optional[str]]:
    """``(field_sets, guidance)`` from a checklist step's ``## Step:`` section.

    The section's first non-blank line is ``Field sets: a, b``; the rest is guidance.
    """
    lines = (section or "").splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    match = _FIELD_SETS_LINE_RE.match(lines[first].strip()) if first is not None else None
    if match is None:
        raise WorkflowError(
            f"workflow '{name}', step '{step}': a checklist step's '## Step: {step}' section "
            "opens with a 'Field sets:' line naming the field sets it runs"
        )
    assert first is not None
    return _parse_reads(match.group("rest")), _prose(lines[first + 1 :])


def parse_workflow(text: str, *, name: str) -> Workflow:
    """Parse one workflow file. ``name`` comes from its filename."""
    return read_workflow_file(text, name=name)[0]


def read_workflow_file(text: str, *, name: str) -> Tuple[Workflow, WorkflowLayout]:
    """:func:`parse_workflow`, plus everything in the file the workflow ignores.

    Raises:
        WorkflowError: with a message naming the problem.
    """
    require_workflow_name(name)
    if not (text or "").strip():
        raise WorkflowError(f"workflow '{name}' is empty")

    meta, frontmatter_other, body = _split_frontmatter(text, name=name)
    body, sections, step_sections = _split_sections(body, name=name)

    header: Optional[List[str]] = None
    unmanaged_at: List[int] = []
    unmanaged_headers: Tuple[str, ...] = ()
    unmanaged_rows: List[Tuple[str, ...]] = []
    preamble_lines: List[str] = []
    postscript_lines: List[str] = []
    rows: List[Tuple[int, Dict[str, str]]] = []

    for line_number, line in enumerate(body.splitlines(), start=1):
        if "|" not in line:
            (preamble_lines if header is None else postscript_lines).append(line)
            continue
        cells = _row_cells(line)
        if header is None:
            mapped = [_COLUMN_ALIASES.get(cell.strip().lower()) for cell in cells]
            if not all(column in mapped for column in _REQUIRED_COLUMNS):
                preamble_lines.append(line)
                continue
            for column in set(c for c in mapped if c):
                if mapped.count(column) > 1:
                    raise WorkflowError(f"workflow '{name}': the step table has two '{column}' columns")
            header = [column or "" for column in mapped]
            unmanaged_at = [index for index, column in enumerate(header) if not column]
            unmanaged_headers = tuple(cells[index] for index in unmanaged_at)
            continue
        if _is_delimiter_row(cells):
            continue
        if not any(cells):
            continue
        row = {column: cell for column, cell in zip(header, cells) if column}
        rows.append((line_number, row))
        unmanaged_rows.append(
            tuple(cells[index] if index < len(cells) else "" for index in unmanaged_at)
        )

    if header is None:
        raise WorkflowError(
            f"workflow '{name}': no step table found. Expected a markdown table with "
            "'step', 'kind', 'worker' and 'reads' columns"
        )

    raw_steps: List[WorkflowStep] = []
    for line_number, row in rows:
        step_name = row.get("step", "").strip().strip("`")
        if not step_name:
            raise WorkflowError(f"workflow '{name}', table row {line_number}: no step name")
        kind = (row.get("kind") or KIND_AGENT).strip().lower() or KIND_AGENT
        _check_kind(kind, step=step_name, name=name)
        worker = (row.get("worker") or "").strip().strip("`")
        reads = _parse_reads(row.get("reads", ""))
        request = step_sections.get(step_name) or None
        if kind in MODEL_STEP_KINDS:
            if not worker or not _MODEL_ID_RE.match(worker):
                raise WorkflowError(
                    f"workflow '{name}', step '{step_name}': a {kind} step's worker must be "
                    "a model id, with no '@ <version>'"
                )
            field_sets: Tuple[str, ...] = ()
            if kind == KIND_CHECKLIST:
                field_sets, request = _split_field_sets(request, step=step_name, name=name)
            raw_steps.append(
                WorkflowStep(
                    name=step_name,
                    agent_id="",
                    reads=reads,
                    request=request,
                    kind=kind,
                    model_id=worker,
                    field_sets=field_sets,
                )
            )
            continue
        match = _WORKER_RE.match(worker)
        if not worker or match is None:
            raise WorkflowError(
                f"workflow '{name}', step '{step_name}': the worker must be an Agent id, "
                "optionally followed by '@ <version>'"
            )
        raw_steps.append(
            WorkflowStep(
                name=step_name,
                agent_id=match.group("id"),
                agent_version=_parse_version(
                    match.group("version"), what=f"step '{step_name}''s Agent version", name=name
                ),
                reads=reads,
                request=request,
                kind=kind,
            )
        )

    for section_step in step_sections:
        if not any(s.name == section_step for s in raw_steps):
            raise WorkflowError(
                f"workflow '{name}': '## Step: {section_step}' names no step in the table"
            )

    steps = tuple(
        WorkflowStep(
            name=s.name,
            agent_id=s.agent_id,
            agent_version=s.agent_version,
            reads=canonical_reads(s.reads, raw_steps),
            request=s.request,
            kind=s.kind,
            model_id=s.model_id,
            field_sets=s.field_sets,
        )
        for s in raw_steps
    )

    result_reads: Optional[Tuple[str, ...]] = None
    instruction: Optional[str] = None
    if "result" in sections:
        lines = sections["result"].splitlines()
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        if first is not None:
            reads_match = _READS_LINE_RE.match(lines[first].strip())
            if reads_match is not None:
                listed = _parse_reads(reads_match.group("rest"))
                order = {s.name: i for i, s in enumerate(steps)}
                result_reads = tuple(sorted(listed, key=lambda r: order.get(r, len(order))))
                lines = lines[first + 1 :]
        instruction = _prose(lines)

    title = meta.get("title", "").strip()
    if not title:
        raise WorkflowError(f"workflow '{name}' needs a 'title:' line in its frontmatter")
    workflow = Workflow(
        name=name,
        title=title,
        version=_parse_version(meta.get("version"), what="version", name=name) or 1,
        files=(meta.get("files") or FILES_ANY).strip().lower(),
        request=sections.get("request") or None,
        steps=steps,
        result=WorkflowResult(
            mode=(meta.get("result") or RESULT_CARD).strip().lower(),
            reads=result_reads,
            instruction=instruction,
        ),
    )
    check_envelope(workflow)
    layout = WorkflowLayout(
        preamble=_prose(preamble_lines),
        postscript=_prose(postscript_lines),
        frontmatter=frontmatter_other,
        unmanaged_columns=unmanaged_headers,
        unmanaged_cells=tuple(unmanaged_rows) if unmanaged_headers else (),
    )
    return workflow, layout


# ---------------------------------------------------------------------------
# Serializing
# ---------------------------------------------------------------------------

_SERIALIZED_COLUMNS = ("step", "kind", "worker", "reads")


def _single_line(value: str, *, what: str, name: str) -> str:
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise WorkflowError(f"workflow '{name}': {what} cannot contain a line break")
    return text


def _cell(value: str, *, what: str, name: str) -> str:
    text = _single_line(value, what=what, name=name)
    if "|" in text:
        raise WorkflowError(f"workflow '{name}': {what} cannot contain '|', which ends a table cell")
    return text


def _row(cells: Sequence[str]) -> str:
    return "|" + "|".join(f" {cell} " if cell else " " for cell in cells) + "|"


def _section_body(text: str, *, what: str, name: str) -> str:
    body = text.strip()
    if _SECTION_RE.search(body):
        raise WorkflowError(
            f"workflow '{name}': {what} cannot contain a line that starts a section "
            "('## Request', '## Result' or '## Step: …')"
        )
    return body


def serialize_workflow(workflow: Workflow, *, layout: Optional[WorkflowLayout] = None) -> str:
    """The file for ``workflow``, the inverse of :func:`read_workflow_file`.

    One fixed shape, so two saves of one definition are byte-identical: the
    frontmatter (``title``, ``version``, then ``files`` and ``result`` when they
    are not the default, then the layout's other lines), the preamble, the table
    with canonical columns then any unread ones, the postscript, then
    ``## Request``, each ``## Step:`` in table order, and ``## Result``.

    The output is read back and compared before it is returned, so a value the
    format cannot carry is refused here, by name, rather than saved as another
    workflow.
    """
    layout = layout or WorkflowLayout()
    name = workflow.name
    require_workflow_name(name)
    check_envelope(workflow)
    workflow = normalized(workflow)

    title = _single_line(workflow.title, what="the title", name=name)
    extra_columns = [_cell(h, what=f"column {h!r}", name=name) for h in layout.unmanaged_columns]
    for header in extra_columns:
        if header.lower() in _COLUMN_ALIASES:
            raise WorkflowError(f"workflow '{name}': an extra column cannot be headed {header!r}")
    if extra_columns and len(layout.unmanaged_cells) != len(workflow.steps):
        raise WorkflowError(
            f"workflow '{name}': the extra columns have {len(layout.unmanaged_cells)} rows "
            f"for {len(workflow.steps)} steps"
        )
    frontmatter = tuple(
        _single_line(line, what="a frontmatter line", name=name)
        for line in layout.frontmatter
        if line.strip()
    )
    preamble = _prose([layout.preamble or ""])
    postscript = _prose([layout.postscript or ""])
    if postscript and "|" in postscript:
        raise WorkflowError(
            f"workflow '{name}': the text after the table cannot contain '|', because a "
            "line with one reads as a row of the table"
        )
    for what, prose in (("the text before the table", preamble), ("the text after the table", postscript)):
        if prose and _SECTION_RE.search(prose):
            raise WorkflowError(f"workflow '{name}': {what} cannot contain a section heading")

    lines = ["---", f"title: {title}", f"version: {workflow.version}"]
    if workflow.files != FILES_ANY:
        lines.append(f"files: {workflow.files}")
    if workflow.result.mode != RESULT_CARD:
        lines.append(f"result: {workflow.result.mode}")
    lines.extend([*frontmatter, "---", ""])
    if preamble:
        lines.extend([preamble, ""])

    lines.append(_row([*_SERIALIZED_COLUMNS, *extra_columns]))
    lines.append("|" + "---|" * (len(_SERIALIZED_COLUMNS) + len(extra_columns)))
    extra_rows: List[Tuple[str, ...]] = []
    for index, step in enumerate(workflow.steps):
        worker = step.worker if step.agent_version is None else f"{step.agent_id} @ {step.agent_version}"
        extras: Tuple[str, ...] = ()
        if extra_columns:
            row_cells = layout.unmanaged_cells[index]
            if len(row_cells) != len(extra_columns):
                raise WorkflowError(
                    f"workflow '{name}', step '{step.name}': {len(row_cells)} extra cells for "
                    f"{len(extra_columns)} extra columns"
                )
            extras = tuple(
                _cell(cell, what=f"step '{step.name}': the {h!r} cell", name=name)
                for h, cell in zip(extra_columns, row_cells)
            )
            extra_rows.append(extras)
        lines.append(_row([step.name, step.kind, worker, ", ".join(step.reads), *extras]))

    if postscript:
        lines.extend(["", postscript])
    if workflow.request and workflow.request.strip():
        lines.extend(["", "## Request", "", _section_body(workflow.request, what="the request", name=name)])
    for step in workflow.steps:
        if step.is_checklist:
            # Always written: the section is where a checklist step's sets are named.
            lines.extend(["", f"## Step: {step.name}", "", f"Field sets: {', '.join(step.field_sets)}"])
            if step.request and step.request.strip():
                lines.extend(["", _section_body(step.request, what=f"step '{step.name}''s guidance", name=name)])
            continue
        if step.request and step.request.strip():
            body = _section_body(step.request, what=f"step '{step.name}''s request", name=name)
            lines.extend(["", f"## Step: {step.name}", "", body])
    result = workflow.result
    instruction = (result.instruction or "").strip()
    if result.reads is not None or instruction:
        lines.extend(["", "## Result"])
        if result.reads is not None:
            lines.extend(["", f"Reads: {', '.join(result.reads)}"])
        if instruction:
            body = _section_body(instruction, what="the result's instruction", name=name)
            if _READS_LINE_RE.match(body.splitlines()[0].strip()):
                raise WorkflowError(
                    f"workflow '{name}': the result's instruction cannot start with 'Reads:'"
                )
            lines.extend(["", body])

    text = "\n".join(lines) + "\n"

    expected_layout = WorkflowLayout(
        preamble=preamble,
        postscript=postscript,
        frontmatter=frontmatter,
        unmanaged_columns=tuple(extra_columns),
        unmanaged_cells=tuple(extra_rows),
    )
    reparsed, relaid = read_workflow_file(text, name=name)
    if reparsed != workflow:
        raise WorkflowError(
            f"workflow '{name}' cannot be written as a file that reads back the same"
        )
    if relaid != expected_layout:
        raise WorkflowError(
            f"workflow '{name}' cannot be written as a file that reads back the same: "
            "its notes or extra columns would change"
        )
    return text


def normalized(workflow: Workflow) -> Workflow:
    """``workflow`` as the file would hold it.

    Text trimmed and empty text as ``None``; every ``reads`` in canonical order
    (inputs, then steps in table order), since a read set has no order of its own.
    """

    def text(value: Optional[str]) -> Optional[str]:
        trimmed = (value or "").strip()
        return trimmed or None

    return Workflow(
        name=workflow.name,
        title=workflow.title.strip(),
        version=workflow.version,
        files=workflow.files,
        request=text(workflow.request),
        steps=tuple(
            WorkflowStep(
                name=s.name,
                agent_id=s.agent_id,
                agent_version=s.agent_version,
                reads=canonical_reads(s.reads, workflow.steps),
                request=text(s.request),
                kind=s.kind,
                model_id=s.model_id,
                field_sets=tuple(s.field_sets),
            )
            for s in workflow.steps
        ),
        result=WorkflowResult(
            mode=workflow.result.mode,
            reads=(
                None
                if workflow.result.reads is None
                else canonical_reads(workflow.result.reads, workflow.steps)
            ),
            instruction=text(workflow.result.instruction),
        ),
    )


def carry_layout(
    layout: WorkflowLayout, before: Workflow, after: Workflow
) -> WorkflowLayout:
    """``layout`` re-fitted to ``after``'s steps: a row's unread cells follow its step's name.

    A new step gets empty cells; a removed or renamed step's cells go with it.
    """
    if not layout.unmanaged_columns:
        return layout
    width = len(layout.unmanaged_columns)
    by_name = {
        step.name: cells for step, cells in zip(before.steps, layout.unmanaged_cells)
    }
    return WorkflowLayout(
        preamble=layout.preamble,
        postscript=layout.postscript,
        frontmatter=layout.frontmatter,
        unmanaged_columns=layout.unmanaged_columns,
        unmanaged_cells=tuple(by_name.get(step.name, ("",) * width) for step in after.steps),
    )


def same_definition(a: Workflow, b: Workflow) -> bool:
    """Whether two definitions run the same, whatever their ``version``."""
    return replace(normalized(a), version=1) == replace(normalized(b), version=1)


def workflow_fingerprint(workflow: Workflow) -> str:
    """A digest of what a run uses, ``version`` excluded. For the ``WFRUN#`` row (D.2)."""
    w = normalized(workflow)
    payload = {
        "files": w.files,
        "request": w.request,
        "steps": [
            # The worker cell's value, so an Agent-only file's digest is what it always was, and
            # the field sets only where a step runs some, so no other step's digest moves.
            [s.name, s.kind, s.worker, s.agent_version, list(s.reads), s.request]
            + ([list(s.field_sets)] if s.field_sets else [])
            for s in w.steps
        ],
        "result": [w.result.mode, list(w.result.reads) if w.result.reads is not None else None, w.result.instruction],
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def validate_workflow_upload(
    filename: str, content: bytes, *, field_sets: Optional[Mapping[str, Any]] = None
) -> Optional[Workflow]:
    """Parse a bundle file that claims to be a workflow, or return ``None``.

    The check every skill-file write path runs, whatever the flags say (§9.1):
    a file stored invalid would be refused later, in front of someone starting
    it. Only the filename decides whether a file is a workflow. A
    ``workflow.<x>.md`` whose ``<x>`` is not a valid name is refused rather than
    stored, since it would otherwise never list.

    ``field_sets`` is the skill's own field sets by name, as the write would
    leave them (:func:`check_field_set_refs`). The write paths always pass it, so
    a checklist step's sets are checked on every write; without it only the file
    itself is checked.

    Raises:
        WorkflowError: not UTF-8, a bad name, contents that do not parse, or (with
            ``field_sets``) a checklist step whose set the skill can't run.
    """
    tail = (filename or "").strip().rsplit("/", 1)[-1]
    if not tail.startswith(WORKFLOW_PREFIX) or not tail.endswith(WORKFLOW_SUFFIX):
        return None
    raw = tail[len(WORKFLOW_PREFIX) : -len(WORKFLOW_SUFFIX)]
    name = require_workflow_name(raw)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkflowError(f"workflow '{name}' is not UTF-8 text") from None
    workflow = parse_workflow(text, name=name)
    if field_sets is not None:
        check_field_set_refs(workflow, field_sets)
    return workflow


__all__ = [
    "FILES_ANY",
    "FILES_MODES",
    "FILES_NONE",
    "FILES_ONE",
    "INPUT_READS",
    "KIND_AGENT",
    "KIND_CHECKLIST",
    "KIND_PROMPT",
    "CHECKLIST_REFUSED_FIELD_TYPE",
    "MAX_CHECKLIST_FIELD_SETS",
    "MAX_REQUEST_CHARS",
    "MAX_STEPS",
    "MODEL_STEP_KINDS",
    "READ_FILES",
    "READ_REQUEST",
    "RESERVED_KINDS",
    "RESULT_CARD",
    "RESULT_MERGE",
    "RESULT_MODES",
    "STEP_KINDS",
    "WORKFLOW_PREFIX",
    "WORKFLOW_SUFFIX",
    "Workflow",
    "WorkflowError",
    "WorkflowLayout",
    "WorkflowResult",
    "WorkflowStep",
    "canonical_reads",
    "carry_layout",
    "check_envelope",
    "check_field_set_refs",
    "checklist_field_set_names",
    "field_set_ref_problem",
    "normalized",
    "parse_workflow",
    "read_workflow_file",
    "require_workflow_name",
    "same_definition",
    "serialize_workflow",
    "slug_for_title",
    "step_name_for",
    "validate_workflow_upload",
    "workflow_filename",
    "workflow_fingerprint",
    "workflow_name_for",
    "workflow_name_of_ref",
]
