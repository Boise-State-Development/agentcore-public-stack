"""The field sets a workflow's checklist steps name, read from the skill that holds them.

``docs/specs/workflow-designer.md`` §22.3. A checklist step names field sets by name, and each
must be ``references/field-set.<name>.md`` in the **same** skill as the workflow, with no
``list`` field. The format can't check that on its own (it is handed one file), so this module
reads the skill's sets and hands them to :func:`~apis.shared.workflows.format.check_field_set_refs`:

- **On every skill-file write**, whatever the flags say: a single upload checks against the
  skill's stored sets (:func:`read_skill_field_sets`), and a bundle import against the sets in the
  import itself (:func:`field_sets_in_files`), since an import replaces the skill's files.
- **At the sheet** (``sheet.field_set_views``): a set can change or go after the workflow is saved,
  so run create re-checks and shows a blocker rather than run a step that can't.

Only the sets a checklist step names are read, one S3 GET each, issued together. A workflow
with no checklist step reads nothing. Nothing here is on a turn's path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from apis.shared.blocking_io import run_blocking
from apis.shared.extraction.field_sets import (
    FieldSet,
    FieldSetError,
    field_set_name_for,
    field_set_name_of_ref,
    parse_field_set,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldSetLookup:
    """One named set as the skill holds it: parsed, absent, or there but unreadable."""

    name: str
    field_set: Optional[FieldSet] = None
    #: ``True`` when the skill has the file but it couldn't be read or parsed now.
    unreadable: bool = False


def skill_field_set_refs(resources: Iterable[Any]) -> Dict[str, Any]:
    """``{set_name: manifest_ref}`` for the skill's top-level field sets. No I/O."""
    found: Dict[str, Any] = {}
    for ref in resources or ():
        name = field_set_name_of_ref(ref)
        if name is not None and name not in found:
            found[name] = ref
    return found


async def read_skill_field_sets(
    resources: Iterable[Any],
    names: Sequence[str],
    *,
    read: Optional[Callable[[str], bytes]] = None,
) -> Dict[str, FieldSetLookup]:
    """Each of ``names`` as the skill holds it, read from its bundle (one GET each, together).

    ``read`` maps a manifest ref's ``s3_key`` to its bytes; the skill resource store's ``get`` by
    default. A set the manifest doesn't list is absent; one whose bytes can't be read or parsed
    is ``unreadable``. Never raises.
    """
    refs = skill_field_set_refs(resources)
    if read is None:
        from apis.shared.skills.resource_store import get_skill_resource_store

        read = get_skill_resource_store().get

    async def one(name: str) -> FieldSetLookup:
        ref = refs.get(name)
        if ref is None:
            return FieldSetLookup(name=name)
        try:
            raw = await run_blocking(lambda: read(ref.s3_key))
            return FieldSetLookup(name=name, field_set=parse_field_set(raw.decode("utf-8"), name=name))
        except (FieldSetError, UnicodeDecodeError):
            logger.warning("Workflow: a field set a checklist step names no longer parses")
            return FieldSetLookup(name=name, unreadable=True)
        except Exception:  # noqa: BLE001 - an unreadable set is reported, never a 500
            logger.warning("Workflow: a field set a checklist step names couldn't be read", exc_info=True)
            return FieldSetLookup(name=name, unreadable=True)

    unique = list(dict.fromkeys(names))
    found = await asyncio.gather(*(one(name) for name in unique))
    return dict(zip(unique, found))


def parsed_field_sets(lookups: Mapping[str, FieldSetLookup]) -> Dict[str, Optional[FieldSet]]:
    """The lookups as :func:`~apis.shared.workflows.format.check_field_set_refs` reads them."""
    return {name: lookup.field_set for name, lookup in lookups.items()}


def unreadable_sets(lookups: Mapping[str, FieldSetLookup]) -> Sequence[str]:
    """The names the skill has but that couldn't be read, in the order asked."""
    return [name for name, lookup in lookups.items() if lookup.unreadable]


def field_sets_in_files(files: Iterable[Any]) -> Dict[str, FieldSet]:
    """The field sets among a bundle import's files, parsed from the bytes in hand. No I/O.

    ``files`` are ``bundle_import.BundleFile``-shaped (``filename``, ``kind``, ``content`` and
    ``resource_path``). Only a top-level reference is a field set, as for the manifest. A file that
    doesn't parse is left out: its own check refuses it.
    """
    found: Dict[str, FieldSet] = {}
    for f in files:
        if getattr(f, "kind", "reference") != "reference":
            continue
        path = getattr(f, "resource_path", "") or getattr(f, "filename", "")
        if "/" in path:
            continue
        name = field_set_name_for(getattr(f, "filename", ""))
        if name is None or name in found:
            continue
        try:
            found[name] = parse_field_set(f.content.decode("utf-8"), name=name)
        except (FieldSetError, UnicodeDecodeError):
            continue
    return found


__all__ = [
    "FieldSetLookup",
    "field_sets_in_files",
    "parsed_field_sets",
    "read_skill_field_sets",
    "skill_field_set_refs",
    "unreadable_sets",
]
