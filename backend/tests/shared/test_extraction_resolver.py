"""Finding a field set among the turn's skills.

``docs/specs/structured-extraction.md`` §4, §5.2, §10, D2, D3. The properties
that matter are an access property and a cost property, and they pull in the
same direction: resolution goes through exactly the turn's effective skill
records, so a set the invoker cannot reach is unreachable by construction, and
the *mount* decision reads only the manifest those records already carry, so it
does no I/O at all.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from apis.shared.extraction.field_sets import FieldSetError
from apis.shared.extraction.resolver import (
    FieldSetNotFound,
    declared_field_sets,
    field_set_names,
    resolve_field_set,
    turn_has_field_set,
)


class FakeRef:
    def __init__(self, filename: str, *, kind: str = "reference", s3_key: str = "") -> None:
        self.filename = filename
        self.kind = kind
        self.s3_key = s3_key or f"skills/x/references/{filename}"


class FakeRecord:
    def __init__(self, skill_id: str, filenames: List[Any]) -> None:
        self.skill_id = skill_id
        self.resources = [
            f if isinstance(f, FakeRef) else FakeRef(f) for f in filenames
        ]


GOOD = """---
title: NSF Solicitation
version: 2
---

| name | find |
|---|---|
| deadline | the deadline |
"""


class TestTheMountGate:
    """§5.2, §10 — mounted on a field set, not on a document (D9)."""

    def test_a_turn_with_a_field_set_mounts(self) -> None:
        records = [FakeRecord("grants", ["field-set.nsf-solicitation.md", "forms.md"])]
        assert turn_has_field_set(records) is True

    def test_a_turn_with_skills_but_no_field_set_does_not(self) -> None:
        assert turn_has_field_set([FakeRecord("grants", ["forms.md"])]) is False

    def test_a_turn_with_no_skills_does_not(self) -> None:
        assert turn_has_field_set([]) is False
        assert turn_has_field_set(None) is False

    def test_a_record_with_no_resources_does_not_raise(self) -> None:
        empty = FakeRecord("grants", [])
        assert turn_has_field_set([empty]) is False

    def test_the_gate_reads_only_the_manifest(self, monkeypatch) -> None:
        """The cost property this whole design rests on (§10).

        The gate runs in ``ChatAgent._create_agent``, inside the TTFT budget.
        If it ever reached S3 the preamble would grow by a network round trip
        per turn — so the store is replaced with something that explodes.
        """
        import apis.shared.extraction.resolver as resolver

        def explode() -> Any:
            raise AssertionError("the mount gate must not touch the resource store")

        monkeypatch.setattr(resolver, "get_skill_resource_store", explode)
        records = [FakeRecord("grants", ["field-set.nsf-solicitation.md"])]
        assert turn_has_field_set(records) is True
        assert field_set_names(records) == ["nsf-solicitation"]
        assert list(declared_field_sets(records)) == ["nsf-solicitation"]


class TestWhichSetsAreVisible:
    def test_only_reference_files_count(self) -> None:
        """A script is accept-and-inert and an asset is bytes. Neither is
        somewhere a schema should be able to hide."""
        records = [
            FakeRecord(
                "grants",
                [
                    FakeRef("field-set.real.md", kind="reference"),
                    FakeRef("field-set.script.md", kind="script"),
                    FakeRef("field-set.asset.md", kind="asset"),
                ],
            )
        ]
        assert field_set_names(records) == ["real"]

    def test_sets_from_several_skills_are_all_visible(self) -> None:
        records = [
            FakeRecord("grants", ["field-set.awards.md"]),
            FakeRecord("compliance", ["field-set.subawards.md"]),
        ]
        assert field_set_names(records) == ["awards", "subawards"]

    def test_names_come_back_sorted(self) -> None:
        """The listing reaches the model in an error message, and an order that
        varies between turns of one session is a cached-prefix rewrite for
        nothing."""
        records = [
            FakeRecord("z", ["field-set.zebra.md"]),
            FakeRecord("a", ["field-set.alpha.md"]),
            FakeRecord("m", ["field-set.middle.md"]),
        ]
        assert field_set_names(records) == ["alpha", "middle", "zebra"]

    def test_a_name_collision_resolves_deterministically(self) -> None:
        """Two active skills declaring one name must not make the turn's
        available schemas depend on fetch order — a field set that changes
        shape between turns is a grammar-compilation problem as well as a
        correctness one (§6.4)."""
        a = FakeRecord("aardvark", [FakeRef("field-set.shared.md", s3_key="key-a")])
        z = FakeRecord("zebra", [FakeRef("field-set.shared.md", s3_key="key-z")])
        first = declared_field_sets([a, z])["shared"][1].s3_key
        second = declared_field_sets([z, a])["shared"][1].s3_key
        assert first == second == "key-a"

    def test_non_field_set_files_are_ignored_rather_than_rejected(self) -> None:
        records = [
            FakeRecord(
                "grants",
                ["forms.md", "field-set.ok.md", "notes.txt", "field-set.BAD.md"],
            )
        ]
        assert field_set_names(records) == ["ok"]


class TestResolution:
    def test_a_declared_set_is_read_and_parsed(self, monkeypatch) -> None:
        import apis.shared.extraction.resolver as resolver

        class Store:
            def get(self, key: str) -> bytes:
                assert key == "skills/grants/references/field-set.nsf-solicitation.md"
                return GOOD.encode("utf-8")

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())
        records = [
            FakeRecord(
                "grants",
                [
                    FakeRef(
                        "field-set.nsf-solicitation.md",
                        s3_key="skills/grants/references/field-set.nsf-solicitation.md",
                    )
                ],
            )
        ]
        field_set = resolve_field_set(records, "nsf-solicitation")
        assert field_set.name == "nsf-solicitation"
        assert field_set.version == "2"
        assert [f.name for f in field_set.fields] == ["deadline"]

    def test_an_undeclared_name_names_what_is_available(self) -> None:
        """A model that mistypes a set name should be corrected, not left to
        guess again."""
        records = [FakeRecord("grants", ["field-set.awards.md"])]
        with pytest.raises(FieldSetNotFound) as exc:
            resolve_field_set(records, "award")
        assert "awards" in str(exc.value)
        assert exc.value.available == ["awards"]

    def test_a_set_from_a_skill_this_turn_lacks_is_simply_absent(self) -> None:
        """Access is structural, not re-checked (D2).

        There is no id the model can name to reach a skill the invoker cannot
        use, because such a skill is not in ``records`` at all. Strictly
        stronger than re-running a predicate on a caller-supplied id.
        """
        this_turn = [FakeRecord("grants", ["field-set.awards.md"])]
        with pytest.raises(FieldSetNotFound):
            resolve_field_set(this_turn, "confidential-review")

    def test_a_read_failure_is_a_field_set_error(self, monkeypatch) -> None:
        import apis.shared.extraction.resolver as resolver
        from apis.shared.skills.resource_store import SkillResourceStoreError

        class Store:
            def get(self, key: str) -> bytes:
                raise SkillResourceStoreError("nope")

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())
        records = [FakeRecord("grants", ["field-set.awards.md"])]
        with pytest.raises(FieldSetError, match="Could not read field set"):
            resolve_field_set(records, "awards")

    def test_a_malformed_set_raises_the_authors_error(self, monkeypatch) -> None:
        """Distinguished from a missing one on purpose: a parse failure is the
        author's mistake and a missing name is the model's, and each needs a
        different person to read the message."""
        import apis.shared.extraction.resolver as resolver

        class Store:
            def get(self, key: str) -> bytes:
                return b"---\ntitle: Broken\n---\n\nNo table here.\n"

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())
        records = [FakeRecord("grants", ["field-set.awards.md"])]
        with pytest.raises(FieldSetError, match="no field table"):
            resolve_field_set(records, "awards")

    def test_a_whitespace_padded_name_still_resolves(self, monkeypatch) -> None:
        import apis.shared.extraction.resolver as resolver

        class Store:
            def get(self, key: str) -> bytes:
                return GOOD.encode("utf-8")

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())
        records = [FakeRecord("grants", ["field-set.nsf-solicitation.md"])]
        assert resolve_field_set(records, "  nsf-solicitation  ").name == (
            "nsf-solicitation"
        )
