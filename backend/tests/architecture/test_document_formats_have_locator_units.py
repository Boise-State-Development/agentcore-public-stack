"""Every readable document format has a provenance unit, or a turn dies.

An invariant spanning two modules that do not import each other's constants:

* ``document_read.DOCUMENT_CLASS_FORMATS`` — the formats
  ``document_provenance_units`` will hand back as ``fmt``.
* ``source_provenance.UNIT_KINDS`` — the formats ``document_units`` knows a
  locator unit for. It raises ``ValueError`` for anything else.

Both ``verify_sources`` and ``document_extract`` call
``document_units(fmt, texts)`` on the result of
``document_provenance_units(...)``, outside any try block, following the house
pattern. So a format added to the first set without the second turns a working
attachment into a raised exception through the agent's event loop — the user's
turn fails, and the only clue is a ``ValueError`` naming a format string.

They match today. This test is why they will still match after the next format
is added: the failure it prevents is cheap to cause (one line in an allowlist)
and expensive to diagnose (a crash two features away from the change).
"""

from __future__ import annotations

import pytest

from apis.shared.files.document_read import DOCUMENT_CLASS_FORMATS
from apis.shared.files.source_provenance import UNIT_KINDS, document_units


def test_every_readable_format_has_a_locator_unit() -> None:
    missing = sorted(set(DOCUMENT_CLASS_FORMATS) - set(UNIT_KINDS))
    assert not missing, (
        "these formats are readable but have no locator unit, so "
        "document_units() raises for them and any tool that verifies a quote "
        "against them fails the turn: " + ", ".join(missing) + ". Add each to "
        "UNIT_KINDS with the unit the format actually has — a .docx has "
        "paragraphs and no pages, and inventing a page there is the defect "
        "source provenance exists to prevent."
    )


def test_no_locator_unit_is_defined_for_a_format_nothing_can_read() -> None:
    """The other direction, which is harmless but worth knowing about.

    A unit kind for a format ``document_read`` cannot produce is dead
    configuration — not a bug, but it reads as support that does not exist.
    """
    orphaned = sorted(set(UNIT_KINDS) - set(DOCUMENT_CLASS_FORMATS))
    assert not orphaned, (
        "these formats have a locator unit but are not document-class, so "
        "nothing can ever reach them: " + ", ".join(orphaned)
    )


@pytest.mark.parametrize("fmt", sorted(DOCUMENT_CLASS_FORMATS))
def test_document_units_accepts_each_readable_format(fmt: str) -> None:
    """Asserted by calling it, not by comparing sets.

    The sets agreeing is the mechanism; ``document_units`` not raising is the
    property, and only one of those is what the tools depend on.
    """
    units = document_units(fmt, ["some text"])
    assert units.unit_count == 1
    assert units.kind == UNIT_KINDS[fmt]
