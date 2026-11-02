"""Finding a field set among the turn's skills, by name.

``docs/specs/structured-extraction.md`` §4, D2, D3. Two operations, and the
difference between them is a cost decision:

* :func:`declared_field_sets` answers "does this turn have any field set at
  all" from the resource **manifest** already on the skill records the turn
  loaded. No S3 GET, no query, no network — which is what lets the mount check
  sit in ``ChatAgent._create_agent`` without spending any of the TTFT budget
  (§10).
* :func:`resolve_field_set` reads one set's bytes and parses it. That happens
  only inside a ``document_extract`` call, long after the first token.

ACCESS IS STRUCTURAL, NOT RE-CHECKED
------------------------------------
``records`` is exactly the turn's effective skill set — already resolved
against the invoker and narrowed by the opt-in selection — so a field set the
invoker cannot reach is simply absent from the index, and there is no name the
model can say to get at one. That is ``read_skill_file``'s argument, and it
holds here for the same reason: it keeps one resolution point, and anything
that widens access has to widen the effective skill set, where the tiering
rules actually live.

The model never reads the field file itself (D3). It names a set; the backend
resolves it. A model that read the table through ``read_skill_file`` and
re-typed the field names into a tool call would pay for the table in the
content blocks and could mangle a name, which silently breaks comparability
with last month's rows — the one thing a field set exists to provide.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.skills.resource_store import (
    SkillResourceStoreError,
    get_skill_resource_store,
)

from .field_sets import FieldSet, FieldSetError, field_set_name_for, parse_field_set

logger = logging.getLogger(__name__)

#: Bundle directory a field set is read from. Only ``reference`` files are
#: considered: a ``script`` is accept-and-inert and an ``asset`` is bytes, and
#: neither is somewhere a schema should be able to hide.
_REFERENCE_KIND = "reference"


def _resource_refs(record: Any) -> Iterable[Any]:
    return getattr(record, "resources", None) or []


def declared_field_sets(records: Iterable[Any]) -> Dict[str, Tuple[Any, Any]]:
    """``{set_name: (record, resource_ref)}`` for every field set in *records*.

    A comprehension over data already in memory: ``fetch_active_skill_records``
    loaded the manifest with the catalog row, so this costs no I/O. That is the
    whole reason the mount gate can be "does any active skill declare a field
    set" rather than "does this turn have a document" (D9).

    Deterministic on collision: when two active skills declare the same set
    name, the one whose ``skill_id`` sorts first wins, and the loser is logged.
    Picking arbitrarily would make a turn's available schemas depend on fetch
    order, and a field set that changes shape between turns of one session is
    both a correctness and a grammar-compilation problem (§6.4).
    """
    found: Dict[str, Tuple[Any, Any]] = {}
    owners: Dict[str, str] = {}
    for record in sorted(
        records, key=lambda r: getattr(r, "skill_id", "") or ""
    ):
        skill_id = getattr(record, "skill_id", "") or ""
        for ref in _resource_refs(record):
            if getattr(ref, "kind", _REFERENCE_KIND) != _REFERENCE_KIND:
                continue
            name = field_set_name_for(getattr(ref, "filename", ""))
            if name is None:
                continue
            if name in found:
                logger.info(
                    "field set %r declared by both %r and %r; using %r",
                    name,
                    owners[name],
                    skill_id,
                    owners[name],
                )
                continue
            found[name] = (record, ref)
            owners[name] = skill_id
    return found


def turn_has_field_set(records: Optional[Iterable[Any]]) -> bool:
    """Whether the turn's skills declare any field set at all.

    The mount condition (§5.2). Short-circuits on the first hit, so a turn with
    many skills does not walk every manifest.
    """
    if not records:
        return False
    for record in records:
        for ref in _resource_refs(record):
            if getattr(ref, "kind", _REFERENCE_KIND) != _REFERENCE_KIND:
                continue
            if field_set_name_for(getattr(ref, "filename", "")) is not None:
                return True
    return False


def field_set_names(records: Optional[Iterable[Any]]) -> List[str]:
    """Every field-set name this turn can run, sorted.

    Sorted because it reaches the model in the tool's own error message when a
    name does not resolve, and an order that varies between turns of one
    session is the kind of thing that re-writes a cached prefix for nothing.
    """
    if not records:
        return []
    return sorted(declared_field_sets(records))


class FieldSetNotFound(LookupError):
    """The named set is not among the turn's skills.

    Carries the available names so the tool can say what *is* there — a model
    that mistypes a set name should be corrected, not left to guess again.
    """

    def __init__(self, name: str, available: List[str]) -> None:
        self.name = name
        self.available = available
        listing = ", ".join(available) or "(none)"
        super().__init__(
            f"No field set '{name}' is available in this conversation. "
            f"Available field sets: {listing}"
        )


def resolve_field_set(records: Iterable[Any], name: str) -> FieldSet:
    """Read and parse one named field set from the turn's skills.

    Raises:
        FieldSetNotFound: the name is not declared by any active skill.
        FieldSetError: the file is there but cannot be parsed. Both are
            conversational errors from the tool, never a 500 — the first is the
            model's mistake and the second is the author's, and each needs a
            different person to read it.
    """
    # Normalized once, and the normalized form is what reaches the parser —
    # which validates the name itself, so passing the caller's padding through
    # would turn a tolerated input into a parse failure.
    wanted = (name or "").strip()
    index = declared_field_sets(records)
    entry = index.get(wanted)
    if entry is None:
        raise FieldSetNotFound(wanted, sorted(index))

    _, ref = entry
    try:
        raw = get_skill_resource_store().get(ref.s3_key)
    except SkillResourceStoreError as exc:
        raise FieldSetError(
            f"Could not read field set '{wanted}': {exc}"
        ) from exc

    return parse_field_set(raw.decode("utf-8", errors="replace"), name=wanted)


__all__ = [
    "FieldSetNotFound",
    "declared_field_sets",
    "field_set_names",
    "resolve_field_set",
    "turn_has_field_set",
]
