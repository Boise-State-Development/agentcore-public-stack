"""What changed between two Agent snapshots (version-snapshots §6.1).

The reviewer's actual question is *"what changed since I approved this?"*, and before this
they could not see it — the queue showed a submission with no reference to what it replaces.
A resubmission that fixes a typo should be approvable in seconds; one that rewrites the
instructions should be impossible to miss.

Pure functions over two ``AgentVersion`` objects. No I/O, no HTTP shapes: the service layer
loads the pair and the route projects the result, so the comparison itself stays trivially
testable against hand-built versions.

**Two decisions worth stating.**

*The instructions diff is computed server-side.* A client-side diff library would render
more prettily, but it would mean a new SPA dependency (the repo pins exactly and does not
add packages casually) and a second implementation of "did this change" that can disagree
with the field-level answer. ``difflib`` is in the standard library and the two answers come
from one place.

*A field is "changed" by value equality, not by identity of formatting.* Two versions whose
``bindings`` differ only in list order genuinely differ — order is meaningful to the
resolver — so no normalization is applied. The one exception is whitespace-only instruction
edits, which are reported as changed but produce an empty line-diff; the reviewer sees the
field flagged and an empty diff, which is the honest rendering of "something moved but no
line did".
"""

import difflib
from typing import Any, List, Optional, Tuple

from .models import AgentVersion
from .versions import DIFF_FIELD_ORDER

# How many lines of unchanged context to keep around each change in the instructions diff.
# Three is the ``diff -u`` default and reads right for prose: enough to locate an edit in a
# long prompt, not so much that a one-line fix arrives as a wall of text.
_DIFF_CONTEXT_LINES = 3


def _comparable(value: Any) -> Any:
    """Normalize a snapshot value for equality, without changing what it means.

    Pydantic models compare fine, but a version read from DynamoDB and one built in memory
    can hold equal-but-not-identical nested models, so both sides are dumped. ``None`` and
    an empty list stay distinct: absent ``bindings`` means "synthesize the legacy KB
    binding" and ``[]`` means "binds nothing", and collapsing them here would hide a real
    behavior change from the person reviewing it.
    """
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    return dump(by_alias=True) if dump else value


def field_changed(before: Optional[AgentVersion], after: AgentVersion, field: str) -> bool:
    """Whether ``field`` differs between the two versions.

    ``before`` is ``None`` on a first submission, where every populated field counts as new
    — there is no prior approved state for it to match.
    """
    if before is None:
        return getattr(after, field, None) is not None
    return _comparable(getattr(before, field, None)) != _comparable(getattr(after, field, None))


def changed_fields(
    before: Optional[AgentVersion], after: AgentVersion
) -> List[Tuple[str, Any, Any]]:
    """Every differing field as ``(field, before_value, after_value)``, in reviewer order.

    Iterates ``DIFF_FIELD_ORDER`` rather than the model's fields, so record metadata
    (``version``, ``createdAt``, ``createdBy``) never shows up as a change — it differs on
    every single version and would bury the fields that matter.
    """
    changes: List[Tuple[str, Any, Any]] = []
    for field in DIFF_FIELD_ORDER:
        if field_changed(before, after, field):
            changes.append(
                (
                    field,
                    getattr(before, field, None) if before else None,
                    getattr(after, field, None),
                )
            )
    return changes


def instructions_diff(before: Optional[AgentVersion], after: AgentVersion) -> List[str]:
    """A unified line diff of the instructions, or ``[]`` when they are unchanged.

    Empty on a first submission too: there is nothing to compare against, and rendering the
    entire prompt as "added" would be noise where the reviewer is going to read the whole
    thing anyway.
    """
    if before is None:
        return []

    old = before.instructions or ""
    new = after.instructions or ""
    if old == new:
        return []

    return list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="approved",
            tofile="submitted",
            lineterm="",
            n=_DIFF_CONTEXT_LINES,
        )
    )


def behavior_changed(before: Optional[AgentVersion], after: AgentVersion) -> bool:
    """Whether anything that changes what the Agent *does* differs.

    The single most useful line in the review UI, and deliberately narrower than "did
    anything change": instructions, bindings and model. A reviewer glancing at a queue needs
    to know whether this is a presentation fix or a behavior change, and that distinction is
    the same one ``BEHAVIOR_FIELDS`` draws for the D13 admin-edit guard.
    """
    return any(
        field_changed(before, after, field)
        for field in ("instructions", "bindings", "model_settings")
    )
