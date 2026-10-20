"""One module decides whether a cited value is supported.

``docs/specs/source-provenance-verification.md`` §4.1. The feature rests on two
questions never being collapsed into one, and the ported design's own history
is the argument: its badge keyed off "did we find this quote in the document?"
alone, which certified the weaker proposition — a fabricated award amount
carrying a real sentence from the budget section earned the same "traced to
p. 12" chip as a correct one.

The state is therefore the API. ``source_provenance.py`` derives it, there is no
public function that takes the two underlying booleans, and the result object
does not carry them — so there is nothing for a second consumer to re-derive a
different answer from. This test keeps it that way:

* **Nothing reconstructs a state.** No module outside the pure module may
  compare against the state constants' *values* or build a ``SourceVerification``
  itself; the only way to get one is ``verify_claim`` / ``verify_claims``.
* **Nothing reintroduces the boolean pair.** A ``verified`` or
  ``value_supported`` attribute on a provenance payload is the ported design's
  misuse vector arriving by the back door.
* **The module stays pure.** It imports stdlib only, which is what makes it safe
  to import from app-api, the AgentCore Runtime and a Lambda alike.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_BACKEND = Path(__file__).resolve().parent.parent.parent
_ROOTS = (_BACKEND / "src", _BACKEND / "scripts")

_PURE_MODULE = "src/apis/shared/files/source_provenance.py"

#: The state values, as strings. A consumer that writes one of these literally
#: is deciding a state for itself rather than reading the one it was given.
_STATE_VALUES = {"supported", "quote_unsupported", "unassessed", "unverified"}

#: Names the ported design exposed as a pair. Reintroducing either on a
#: provenance payload re-opens the conflation this feature exists to close.
_BANNED_FIELDS = {"value_supported", "value_support_method", "support_state"}

#: Modules allowed to name a state value literally, and why.
_LITERAL_ALLOWED = {
    _PURE_MODULE: "defines the states",
}


def _python_files() -> Iterator[Tuple[str, Path]]:
    for root in _ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            yield str(path.relative_to(_BACKEND)), path


def test_no_module_outside_the_pure_one_writes_a_state_value_literally() -> None:
    """A literal state is a state decided locally.

    Consumers read ``result.state`` and pass it on; they compare against the
    exported constants when they must compare at all.
    """
    offenders: List[str] = []
    for rel, path in _python_files():
        if rel in _LITERAL_ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not our file to fix
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _STATE_VALUES
                # "supported" and "unassessed" are ordinary English; only flag a
                # file that also deals in provenance, so this stays a boundary
                # test and not a spell-checker.
                and "provenance" in rel
            ):
                offenders.append(f"{rel}:{node.lineno} -> {node.value!r}")
    assert not offenders, (
        "a source-provenance state must come from verify_claim, not from a "
        "literal decided here:\n  " + "\n  ".join(offenders)
    )


def test_nothing_reintroduces_the_boolean_pair() -> None:
    """``verified`` plus ``value_supported`` is the ported design's shape.

    Keeping only ``state`` and ``method`` is what makes the weaker claim
    impossible to re-certify: there is no pair to mis-combine.
    """
    offenders: List[str] = []
    for rel, path in _python_files():
        if rel == _PURE_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        if "provenance" not in rel and "verify_sources" not in rel:
            continue
        for banned in _BANNED_FIELDS:
            if banned in text:
                offenders.append(f"{rel} -> {banned}")
    assert not offenders, (
        "the two underlying booleans must stay inside source_provenance.py:\n  "
        + "\n  ".join(offenders)
    )


def test_the_result_object_exposes_no_booleans_to_branch_on() -> None:
    from apis.shared.files.source_provenance import SourceVerification

    assert set(SourceVerification.__dataclass_fields__) == {
        "claim_id",
        "state",
        "method",
        "locator",
    }


def test_there_is_no_public_function_taking_the_pair() -> None:
    """The ported ``support_state(verified, value_supported)`` is itself the
    misuse vector: a caller can pass a pair it invented, or pass the weaker
    answer twice and earn the stronger badge."""
    from apis.shared.files import source_provenance as sp

    for name in ("support_state", "support_state_of"):
        assert not hasattr(sp, name), f"{name} re-opens the conflation"
    # The only producers.
    assert {"verify_claim", "verify_claims"} <= set(sp.__all__)


def test_the_pure_module_imports_stdlib_only() -> None:
    tree = ast.parse((_BACKEND / _PURE_MODULE).read_text(encoding="utf-8"))
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = [
        name
        for name in imported
        if name.split(".")[0] in {"apis", "agents", "boto3", "botocore", "pydantic", "strands"}
    ]
    assert not forbidden, (
        "source_provenance.py must stay importable from app-api, the Runtime and "
        f"a Lambda alike; found {forbidden}"
    )
