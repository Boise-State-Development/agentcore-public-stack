"""Read one uploaded ``SKILL.md`` into form fields and diagnostics, writing nothing.

The server half of the admin and My Skills "Import SKILL.md" controls: the SPA
posts the file and patches its form from what comes back, so the format is
parsed in one place (``skill_md.parse_skill_md``) and mapped in one place
(``bundle_import.skill_fields_from_frontmatter``), the same as bundle import.

Only a file that cannot be read refuses (:class:`SkillMdUploadError`): over the
size cap, not UTF-8, or a frontmatter the bounded parser refuses. Every other
problem is a diagnostic. The save that follows applies its own checks.

Also here: the unsafe-character report shared with the bundle preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from pydantic import BaseModel, Field

from apis.shared.skills.bundle_import import (
    BundleImportError,
    MAX_SKILL_MD_BYTES,
    skill_fields_from_frontmatter,
)
from apis.shared.skills.skill_md import (
    LEVEL_ERROR,
    LEVEL_WARNING,
    Diagnostic,
    SkillMdError,
    find_unsafe_characters,
    parse_skill_md,
    validate_frontmatter,
)

# The same cap bundle import holds a SKILL.md to: it becomes ``instructions``.
MAX_SKILL_MD_UPLOAD_BYTES = MAX_SKILL_MD_BYTES

# At most this many unsafe characters are listed; the rest are counted.
MAX_UNSAFE_CHARACTER_DIAGNOSTICS = 20


class SkillMdUploadError(ValueError):
    """The uploaded ``SKILL.md`` cannot be read (400). The message is shown as is."""


class DiagnosticModel(BaseModel):
    """One diagnostic as the API returns it."""

    level: str
    field: str
    code: str
    message: str

    @classmethod
    def from_diagnostic(cls, diagnostic: Diagnostic) -> "DiagnosticModel":
        return cls(
            level=diagnostic.level,
            field=diagnostic.field,
            code=diagnostic.code,
            message=diagnostic.message,
        )


@dataclass
class SkillMdImport:
    """What one ``SKILL.md`` fills in, and what is worth telling its author."""

    name: str
    display_name: str
    description: str
    instructions: str
    allowed_tools: List[str]
    skill_metadata: Dict[str, Any]
    diagnostics: List[Diagnostic] = field(default_factory=list)


def decode_skill_md_upload(raw: bytes) -> str:
    """``raw`` as text, or :class:`SkillMdUploadError` if it is too large or not UTF-8."""
    if len(raw) > MAX_SKILL_MD_UPLOAD_BYTES:
        raise SkillMdUploadError(
            f"SKILL.md is larger than {MAX_SKILL_MD_UPLOAD_BYTES // 1024} KiB. "
            "Move long sections into reference files."
        )
    try:
        # The parser drops a byte-order mark itself, and says so.
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillMdUploadError(
            "SKILL.md is not UTF-8 text. Save it as UTF-8 and try again."
        ) from None


def read_skill_md_upload(raw: bytes) -> SkillMdImport:
    """Parse an uploaded ``SKILL.md`` into form fields plus diagnostics.

    Pure and CPU-bound: app-api runs it in a worker thread.

    Raises:
        SkillMdUploadError: Too large, not UTF-8, or a frontmatter the bounded
            parser refuses (alias, anchor, depth, node count, size, not a mapping,
            not valid YAML even after the colon repair).
    """
    text = decode_skill_md_upload(raw)
    try:
        parsed = parse_skill_md(text)
        fields = skill_fields_from_frontmatter(parsed.frontmatter, parsed.body)
    except (SkillMdError, BundleImportError) as exc:
        raise SkillMdUploadError(str(exc)) from None

    diagnostics = list(parsed.diagnostics)
    diagnostics += validate_frontmatter(parsed.frontmatter, strict=False)
    diagnostics += field_unsafe_character_diagnostics(
        (("name", fields.name), ("description", fields.description))
    )
    diagnostics += unsafe_character_diagnostics((("SKILL.md", fields.instructions),))
    return SkillMdImport(
        name=fields.name,
        display_name=fields.display_name if fields.name else "",
        description=fields.description,
        instructions=fields.instructions,
        allowed_tools=fields.allowed_tools,
        skill_metadata=fields.skill_metadata,
        diagnostics=diagnostics,
    )


def field_unsafe_character_diagnostics(
    fields: Iterable[Tuple[str, str]],
) -> List[Diagnostic]:
    """One error per frontmatter field holding an unsafe character.

    These are errors because a save refuses them: the name and description are
    shown to people and to the model.
    """
    diagnostics: List[Diagnostic] = []
    for name, value in fields:
        found = find_unsafe_characters(value or "")
        if not found:
            continue
        first = found[0]
        more = f" and {len(found) - 1} more" if len(found) > 1 else ""
        diagnostics.append(
            Diagnostic(
                "unsafe_character",
                f"{name} contains an invisible or control character ({first.label} at "
                f"position {first.index}{more}). Remove it before saving.",
                level=LEVEL_ERROR,
                field=name,
            )
        )
    return diagnostics


def _line_and_column(text: str, index: int) -> Tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return line, column


def unsafe_character_diagnostics(
    sources: Sequence[Tuple[str, str]],
    *,
    limit: int = MAX_UNSAFE_CHARACTER_DIAGNOSTICS,
) -> List[Diagnostic]:
    """Warnings for unsafe characters in long text (a body, a text file).

    ``sources`` is ``(file, text)`` pairs. Each of the first ``limit`` characters
    found, across all sources, is one warning naming its file, line, column and
    code point; one more warning counts the rest. Carriage returns are allowed,
    since a file may use CRLF line ends.
    """
    diagnostics: List[Diagnostic] = []
    remaining = 0
    for filename, text in sources:
        for char in find_unsafe_characters(text or "", allow_carriage_return=True):
            if len(diagnostics) >= limit:
                remaining += 1
                continue
            line, column = _line_and_column(text, char.index)
            diagnostics.append(
                Diagnostic(
                    "unsafe_character",
                    f"{filename} has an invisible or control character ({char.label}) "
                    f"at line {line}, column {column}. It can hide or reorder text "
                    "the agent reads; remove it unless it is intended.",
                    level=LEVEL_WARNING,
                    field=filename,
                )
            )
    if remaining:
        diagnostics.append(
            Diagnostic(
                "unsafe_character_more",
                f"{remaining} more invisible or control "
                f"character{'s' if remaining != 1 else ''} not listed.",
                level=LEVEL_WARNING,
            )
        )
    return diagnostics


class SkillMdParseResponse(BaseModel):
    """``POST /skills/mine/import/parse`` and ``POST /admin/skills/import/parse``."""

    name: str
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions: str
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict[str, Any] = Field(default_factory=dict, alias="skillMetadata")
    diagnostics: List[DiagnosticModel] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @classmethod
    def from_import(cls, parsed: SkillMdImport) -> "SkillMdParseResponse":
        return cls(
            name=parsed.name,
            display_name=parsed.display_name,
            description=parsed.description,
            instructions=parsed.instructions,
            allowed_tools=parsed.allowed_tools,
            skill_metadata=parsed.skill_metadata,
            diagnostics=[DiagnosticModel.from_diagnostic(d) for d in parsed.diagnostics],
        )
