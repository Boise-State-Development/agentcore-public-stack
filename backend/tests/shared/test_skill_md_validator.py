"""agentskills.io frontmatter validator and the conformant SKILL.md projection."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from apis.shared.skills.bundle import generate_skill_md
from apis.shared.skills.skill_md import (
    Diagnostic,
    STANDARD_KEYS,
    parse_skill_md,
    skill_metadata_problem,
    validate_frontmatter,
    validate_skill_metadata,
)


def _valid(**overrides: Any) -> Dict[str, Any]:
    frontmatter: Dict[str, Any] = {"name": "pdf-processing", "description": "Work with PDFs."}
    frontmatter.update(overrides)
    return frontmatter


def _errors(frontmatter: Dict[str, Any], *, strict: bool = True) -> List[Diagnostic]:
    return [d for d in validate_frontmatter(frontmatter, strict=strict) if d.level == "error"]


# =============================================================================
# The validator, table-driven from the specification's own examples
# =============================================================================


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param(_valid(), id="pdf-processing"),
        pytest.param(_valid(name="data-analysis"), id="data-analysis"),
        pytest.param(_valid(name="code-review"), id="code-review"),
        pytest.param(_valid(name="a"), id="one-character"),
        pytest.param(_valid(name="a" * 64), id="64-characters"),
        pytest.param(_valid(description="d" * 1024), id="1024-character-description"),
        pytest.param(_valid(description="  padded  "), id="description-trimmed"),
        pytest.param(_valid(license="Apache-2.0"), id="license"),
        pytest.param(_valid(license="Proprietary. LICENSE.txt has complete terms"), id="license-text"),
        pytest.param(_valid(license=""), id="empty-license"),
        pytest.param(_valid(license="l" * 500), id="500-character-license"),
        pytest.param(
            _valid(compatibility="Requires git, docker, jq, and access to the internet"),
            id="compatibility",
        ),
        pytest.param(_valid(compatibility="c" * 500), id="500-character-compatibility"),
        pytest.param(
            _valid(metadata={"author": "example-org", "version": "1.0"}), id="metadata"
        ),
        pytest.param(_valid(metadata={}), id="empty-metadata"),
        pytest.param(_valid(**{"allowed-tools": "Bash(git:*) Bash(jq:*) Read"}), id="allowed-tools-string"),
        pytest.param(_valid(**{"allowed-tools": ["Read", "Bash(git:*)"]}), id="allowed-tools-list"),
        pytest.param(_valid(**{"allowed-tools": " ".join(["t"] * 64)}), id="64-tools"),
    ],
)
def test_valid_frontmatter_has_no_diagnostics(frontmatter: Dict[str, Any]) -> None:
    assert validate_frontmatter(frontmatter, strict=True) == []


@pytest.mark.parametrize(
    "frontmatter,field,code",
    [
        pytest.param(_valid(name="PDF-Processing"), "name", "name_invalid", id="uppercase"),
        pytest.param(_valid(name="-pdf"), "name", "name_invalid", id="leading-hyphen"),
        pytest.param(_valid(name="pdf-"), "name", "name_invalid", id="trailing-hyphen"),
        pytest.param(_valid(name="pdf--processing"), "name", "name_invalid", id="double-hyphen"),
        pytest.param(_valid(name="pdf_processing"), "name", "name_invalid", id="underscore"),
        pytest.param(_valid(name="a" * 65), "name", "name_too_long", id="65-characters"),
        pytest.param(_valid(name=""), "name", "name_missing", id="empty-name"),
        pytest.param({"description": "d"}, "name", "name_missing", id="no-name"),
        pytest.param(_valid(name=7), "name", "name_missing", id="non-string-name"),
        pytest.param(
            _valid(description="d" * 1025), "description", "description_too_long", id="1025-description"
        ),
        pytest.param(_valid(description=""), "description", "description_missing", id="empty-description"),
        pytest.param(_valid(description="   "), "description", "description_missing", id="blank-description"),
        pytest.param({"name": "x"}, "description", "description_missing", id="no-description"),
        pytest.param(
            _valid(compatibility="c" * 501), "compatibility", "compatibility_too_long", id="501-compatibility"
        ),
        pytest.param(_valid(compatibility=""), "compatibility", "compatibility_invalid", id="empty-compatibility"),
        pytest.param(_valid(compatibility=3), "compatibility", "compatibility_invalid", id="non-string-compatibility"),
        pytest.param(_valid(license="l" * 501), "license", "license_too_long", id="501-license"),
        pytest.param(_valid(license=["MIT"]), "license", "license_invalid", id="non-string-license"),
        pytest.param(
            _valid(metadata={"version": 1.0}), "metadata", "metadata_value_not_string", id="non-string-metadata-value"
        ),
        pytest.param(
            _valid(metadata={"nested": {"a": "b"}}), "metadata", "metadata_value_not_string", id="nested-metadata"
        ),
        pytest.param(_valid(metadata="author=me"), "metadata", "metadata_invalid", id="metadata-not-a-map"),
        pytest.param(
            _valid(metadata={f"k{i}": "v" for i in range(51)}), "metadata", "metadata_too_many_keys", id="51-keys"
        ),
        pytest.param(
            _valid(metadata={"big": "x" * (16 * 1024)}), "metadata", "metadata_too_large", id="over-16-KiB"
        ),
        pytest.param(
            _valid(**{"allowed-tools": " ".join(["t"] * 65)}), "allowed-tools", "allowed_tools_too_many", id="65-tools"
        ),
        pytest.param(
            _valid(**{"allowed-tools": {"Read": True}}), "allowed-tools", "allowed_tools_invalid", id="tools-map"
        ),
        pytest.param(
            _valid(**{"allowed-tools": ["Read", 3]}), "allowed-tools", "allowed_tools_invalid", id="tools-non-string"
        ),
    ],
)
def test_invalid_frontmatter_is_an_error_naming_its_field(
    frontmatter: Dict[str, Any], field: str, code: str
) -> None:
    for strict in (True, False):
        errors = _errors(frontmatter, strict=strict)
        assert [(d.field, d.code) for d in errors] == [(field, code)]
        assert errors[0].message


def test_an_unknown_key_is_a_warning_when_lenient_and_an_error_when_strict() -> None:
    frontmatter = _valid(version="1.0", tags=["a"])
    lenient = validate_frontmatter(frontmatter, strict=False)
    strict = validate_frontmatter(frontmatter, strict=True)
    assert [(d.level, d.field) for d in lenient] == [("warning", "tags"), ("warning", "version")]
    assert [(d.level, d.field) for d in strict] == [("error", "tags"), ("error", "version")]


def test_the_parser_notes_keep_working_as_diagnostics() -> None:
    """PR 1's callers build Diagnostic(code, message); level and field default."""
    note = Diagnostic("byte_order_mark", "removed")
    assert (note.level, note.field, note.is_error) == ("warning", "", False)
    parsed = parse_skill_md("﻿---\nname: x\ndescription: y\n---\nbody")
    assert [d.code for d in parsed.diagnostics] == ["byte_order_mark"]


# =============================================================================
# skillMetadata, the lenient check every write path uses
# =============================================================================


@pytest.mark.parametrize(
    "skill_metadata",
    [
        None,
        {},
        {"license": "MIT", "compatibility": "Needs network access"},
        {"metadata": {"author": "example-org"}},
        # Non-standard keys are warnings: they are kept under metadata.
        {"version": 2, "tags": ["a", "b"], "x-team": {"owner": "web"}},
        # Repaired losslessly by the projection, so input only warns.
        {"metadata": {"version": 2, "beta": True}, "license": "MIT"},
        {"metadata": {"version": Decimal("2")}},
        {"license": 3},
    ],
)
def test_skill_metadata_that_passes(skill_metadata: Any) -> None:
    assert skill_metadata_problem(skill_metadata) is None
    assert all(d.level == "warning" for d in validate_skill_metadata(skill_metadata))


@pytest.mark.parametrize(
    "skill_metadata,needle",
    [
        ({"compatibility": "c" * 501}, "compatibility"),
        ({"compatibility": 3}, "compatibility"),
        ({"metadata": "author"}, "metadata"),
        ({"metadata": {f"k{i}": "v" for i in range(51)}}, "metadata"),
        ({"metadata": {"big": "x" * (16 * 1024)}}, "metadata"),
        ({"allowed-tools": " ".join(["t"] * 65)}, "allowed-tools"),
        ("not a map", "skillMetadata"),
    ],
)
def test_skill_metadata_that_is_refused(skill_metadata: Any, needle: str) -> None:
    problem = skill_metadata_problem(skill_metadata)
    assert problem is not None and needle in problem


def test_non_string_metadata_values_and_license_warn_on_input_but_fail_output() -> None:
    stored = {"metadata": {"version": 2}, "license": ["MIT"]}
    warnings = validate_skill_metadata(stored)
    assert {(d.level, d.code) for d in warnings} == {
        ("warning", "metadata_value_not_string"),
        ("warning", "license_invalid"),
    }
    # Output stays strict: the same values written as frontmatter are errors.
    assert {d.code for d in _errors(_valid(**stored))} == {
        "metadata_value_not_string",
        "license_invalid",
    }


@pytest.mark.parametrize("version", [2, Decimal("2")])
def test_a_stored_non_string_metadata_value_projects_strictly_valid(version: Any) -> None:
    md = generate_skill_md(
        skill_id="stored_skill",
        description="d",
        instructions="i",
        skill_metadata={"metadata": {"version": version}, "license": "MIT"},
    )
    frontmatter = parse_skill_md(md).frontmatter
    assert validate_frontmatter(frontmatter, strict=True) == []
    assert frontmatter["license"] == "MIT"
    assert frontmatter["metadata"] == {"version": "2"}


def test_unknown_skill_metadata_keys_are_warnings() -> None:
    diagnostics = validate_skill_metadata({"version": "1", "name": "other"})
    assert {(d.level, d.field) for d in diagnostics} == {("warning", "version"), ("warning", "name")}


# =============================================================================
# The projection is conformant and loses nothing
# =============================================================================

FIXTURE_ROWS: List[Dict[str, Any]] = [
    {"skill_id": "plain_skill", "description": "Plain.", "instructions": "# Plain"},
    {
        "skill_id": "tooled_skill",
        "description": "Uses tools.",
        "instructions": "body",
        "allowed_tools": ["Read", "Bash(git:*)", "web_search"],
    },
    {
        "skill_id": "licensed",
        "description": "Has a license and a compatibility.",
        "instructions": "body",
        "skill_metadata": {"license": "Apache-2.0", "compatibility": "Requires uv and network"},
    },
    {
        "skill_id": "nested_metadata",
        "description": "Nested metadata values.",
        "instructions": "body",
        "skill_metadata": {
            "metadata": {"author": "example-org", "version": 2, "deps": {"b": [1, 2], "a": None}},
        },
    },
    {
        "skill_id": "with_compose",
        "description": "A composite skill.",
        "instructions": "body",
        "skill_metadata": {"compose": ["plain_skill", "tooled_skill"], "tags": ["x", "y"]},
    },
    {
        "skill_id": "non_standard",
        "description": "Non-standard top-level keys, with a clash.",
        "instructions": "body",
        "skill_metadata": {
            "version": "1.0",
            "x-flag": True,
            "count": Decimal("3"),
            "ratio": Decimal("0.5"),
            "author": "loses-the-clash",
            "metadata": {"author": "wins-the-clash"},
        },
    },
    {
        "skill_id": "bad_license",
        "description": "A license that is not a string is kept under metadata.",
        "instructions": "body",
        "skill_metadata": {"license": ["MIT", "Apache-2.0"], "compatibility": 7, "metadata": "flat"},
    },
    {
        "skill_id": "row_owned",
        "description": "The row's own description.",
        "instructions": "body",
        "skill_metadata": {"name": "other-name", "description": "other", "instructions": "x"},
    },
]


def _generate(row: Dict[str, Any]) -> str:
    return generate_skill_md(
        skill_id=row["skill_id"],
        description=row["description"],
        instructions=row["instructions"],
        allowed_tools=row.get("allowed_tools"),
        skill_metadata=row.get("skill_metadata"),
    )


@pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["skill_id"] for r in FIXTURE_ROWS])
def test_every_projection_passes_strict_validation(row: Dict[str, Any]) -> None:
    frontmatter = parse_skill_md(_generate(row)).frontmatter
    assert validate_frontmatter(frontmatter, strict=True) == []
    assert list(frontmatter) == [k for k in STANDARD_KEYS if k in frontmatter]


@pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["skill_id"] for r in FIXTURE_ROWS])
def test_nothing_from_skill_metadata_is_lost(row: Dict[str, Any]) -> None:
    frontmatter = parse_skill_md(_generate(row)).frontmatter
    metadata = frontmatter.get("metadata", {})
    stored = row.get("skill_metadata") or {}
    explicit = stored.get("metadata") if isinstance(stored.get("metadata"), dict) else {}
    row_owned = {"name", "description", "instructions", "allowed-tools", "compose"}
    for key, value in stored.items():
        if key in row_owned or (key == "metadata" and isinstance(value, dict)):
            continue
        if key in ("license", "compatibility") and frontmatter.get(key) == value:
            continue
        if key in explicit:
            continue  # the explicit metadata entry wins the clash
        assert key in metadata, key
        if isinstance(value, str):
            assert metadata[key] == value
        elif not isinstance(value, Decimal):
            assert json.loads(metadata[key]) == value
    for key, value in explicit.items():
        assert key in metadata
        assert metadata[key] == (value if isinstance(value, str) else json.dumps(value, sort_keys=True))


def test_specific_folding() -> None:
    by_id = {r["skill_id"]: r for r in FIXTURE_ROWS}
    fm = parse_skill_md(_generate(by_id["non_standard"])).frontmatter
    assert fm["metadata"] == {
        "author": "wins-the-clash",
        "count": "3",
        "ratio": "0.5",
        "version": "1.0",
        "x-flag": "true",
    }
    fm = parse_skill_md(_generate(by_id["nested_metadata"])).frontmatter
    assert fm["metadata"]["deps"] == '{"a": null, "b": [1, 2]}'
    fm = parse_skill_md(_generate(by_id["with_compose"])).frontmatter
    assert "compose" not in fm and "compose" not in fm["metadata"]
    fm = parse_skill_md(_generate(by_id["bad_license"])).frontmatter
    assert "license" not in fm and "compatibility" not in fm
    assert fm["metadata"] == {"compatibility": "7", "license": '["MIT", "Apache-2.0"]', "metadata": "flat"}
    fm = parse_skill_md(_generate(by_id["row_owned"])).frontmatter
    assert fm == {"name": "row-owned", "description": "The row's own description."}


def test_allowed_tools_is_one_space_separated_string() -> None:
    by_id = {r["skill_id"]: r for r in FIXTURE_ROWS}
    md = _generate(by_id["tooled_skill"])
    assert "allowed-tools: Read Bash(git:*) web_search\n" in md
    assert parse_skill_md(md).frontmatter["allowed-tools"] == "Read Bash(git:*) web_search"


def _row_from_parsed(skill_id: str, md: str) -> Dict[str, Any]:
    """What an import of the projection would store (the inverse of the projection)."""
    parsed = parse_skill_md(md)
    fm = dict(parsed.frontmatter)
    tools = fm.pop("allowed-tools", "")
    fm.pop("name")
    return {
        "skill_id": skill_id,
        "description": fm.pop("description"),
        "instructions": parsed.body,
        "allowed_tools": tools.split(),
        "skill_metadata": fm,
    }


@pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["skill_id"] for r in FIXTURE_ROWS])
def test_parse_generate_parse_is_stable(row: Dict[str, Any]) -> None:
    first = _generate(row)
    second = _generate(_row_from_parsed(row["skill_id"], first))
    assert second == first
    assert parse_skill_md(second).frontmatter == parse_skill_md(first).frontmatter


@pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["skill_id"] for r in FIXTURE_ROWS])
def test_projection_is_deterministic_whatever_order_the_map_arrives_in(row: Dict[str, Any]) -> None:
    reordered = dict(row)
    stored = row.get("skill_metadata") or {}
    reordered["skill_metadata"] = {
        k: (dict(reversed(list(v.items()))) if isinstance(v, dict) else v)
        for k, v in reversed(list(stored.items()))
    }
    assert _generate(reordered) == _generate(row)


def test_a_legacy_empty_description_still_projects_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="apis.shared.skills.bundle"):
        md = generate_skill_md(skill_id="legacy_skill", description="", instructions="# Legacy")
    assert md == "---\nname: legacy-skill\ndescription: ''\n---\n\n# Legacy\n"
    assert any("legacy_skill" in r.getMessage() for r in caplog.records)


def test_it_never_raises_on_metadata_it_cannot_read(caplog: pytest.LogCaptureFixture) -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no")

    with caplog.at_level(logging.WARNING, logger="apis.shared.skills.bundle"):
        md = generate_skill_md(
            skill_id="odd_skill",
            description="d",
            instructions="i",
            skill_metadata={"weird": Unprintable()},
        )
    assert parse_skill_md(md).frontmatter == {"name": "odd-skill", "description": "d"}
    assert any("odd_skill" in r.getMessage() for r in caplog.records)


def test_oversized_metadata_still_projects(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="apis.shared.skills.bundle"):
        md = generate_skill_md(
            skill_id="big_meta",
            description="d",
            instructions="i",
            skill_metadata={f"k{i}": "v" for i in range(60)},
        )
    assert len(parse_skill_md(md).frontmatter["metadata"]) == 60
    assert any("big_meta" in r.getMessage() for r in caplog.records)
