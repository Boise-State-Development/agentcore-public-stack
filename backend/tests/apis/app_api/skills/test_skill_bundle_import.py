"""Reading a skill bundle out of a zip, and the GitHub source (skill-bundle-import.md).

What these pin, in the order a mistake would hurt: a security bound refuses the
whole archive (traversal, absolute paths, encryption, entry and size caps); a
content rule skips one file and says why (symlinks, disallowed types, oversize,
invalid field sets); the GitHub fetch only ever goes to codeload, with redirects
off; and the mapping onto our manifest is deterministic.
"""

from __future__ import annotations

import io
import stat
import zipfile
from typing import Dict, Optional

import httpx
import pytest

from apis.app_api.skills import github_source
from apis.app_api.skills.github_source import (
    GitHubLocation,
    GitHubSourceError,
    fetch_archive,
    parse_github_url,
)
from apis.shared.skills import bundle_import
from apis.shared.skills.bundle_files import MAX_RESOURCE_BYTES, MAX_RESOURCES_PER_SKILL
from apis.shared.skills.bundle_import import (
    BundleImportError,
    diff_files,
    load_bundle,
    normalize_bundle_path,
    parse_skill_md,
)
from apis.shared.skills.models import SkillDefinition, SkillResourceRef

SKILL_MD = """---
name: contract-redline
description: Redline a contract against a playbook.
allowed-tools: Read Write
license: Apache-2.0
version: 1.5
updated: 2026-11-01
---

# Contract redline

Read `references/nda.md` first.
"""


def make_zip(files: Dict[str, bytes | str], *, comment: bytes = b"", symlinks=(), encrypted=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in symlinks:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, body.encode() if isinstance(body, str) else body)
        zf.comment = comment
    data = bytearray(buf.getvalue())
    # zipfile resets flag bits on write, so set "encrypted" in the central
    # directory, which is what the reader's ZipInfo carries.
    at = data.find(b"PK\x01\x02")
    while at != -1:
        name_len = int.from_bytes(data[at + 28 : at + 30], "little")
        if data[at + 46 : at + 46 + name_len].decode() in encrypted:
            data[at + 8] |= 0x1
        at = data.find(b"PK\x01\x02", at + 4)
    return bytes(data)


def bundle_of(files, path: Optional[str] = None, **kw):
    load = load_bundle(make_zip(files, **kw), path=path)
    assert load.bundle is not None, load.candidates
    return load.bundle


def by_path(bundle):
    return {f.path: f for f in bundle.files}


def skipped(bundle):
    return {s.path: s.reason for s in bundle.skipped}


# =============================================================================
# Mapping
# =============================================================================


class TestMapping:
    def test_reads_frontmatter_and_body(self):
        b = bundle_of({"SKILL.md": SKILL_MD})
        assert b.name == "contract-redline"
        assert b.display_name == "Contract redline"
        assert b.description == "Redline a contract against a playbook."
        assert b.instructions.startswith("# Contract redline")
        assert b.allowed_tools == ["Read", "Write"]
        # Floats and dates are strings, which a DynamoDB put accepts.
        assert b.skill_metadata == {"license": "Apache-2.0", "version": "1.5", "updated": "2026-11-01"}

    def test_kinds_follow_the_first_directory(self):
        b = bundle_of(
            {
                "SKILL.md": SKILL_MD,
                "references/nda.md": "nda",
                "reference/eval.md": "eval",
                "scripts/engine.py": "print(1)",
                "assets/logo.png": b"\x89PNG....",
                "FORMS.md": "forms",
                "examples/sample.md": "sample",
                "examples/chart.png": b"\x89PNG..",
            }
        )
        kinds = {p: (f.kind, f.filename) for p, f in by_path(b).items()}
        assert kinds == {
            "references/nda.md": ("reference", "nda.md"),
            "reference/eval.md": ("reference", "eval.md"),
            "scripts/engine.py": ("script", "engine.py"),
            "assets/logo.png": ("asset", "logo.png"),
            "FORMS.md": ("reference", "FORMS.md"),
            "examples/sample.md": ("reference", "sample.md"),
            "examples/chart.png": ("asset", "chart.png"),
        }

    def test_strips_one_wrapping_folder(self):
        b = bundle_of({"skills-main/SKILL.md": SKILL_MD, "skills-main/references/a.md": "a"})
        assert b.root == ""
        assert [f.path for f in b.files] == ["references/a.md"]

    def test_nested_files_keep_their_path_below_the_kind_directory(self):
        # Before nested paths this flattened, renaming the clash to
        # "office-__init__.py". Now each file keeps its folder.
        b = bundle_of(
            {
                "SKILL.md": SKILL_MD,
                "scripts/__init__.py": "x",
                "scripts/office/__init__.py": "y",
                "scripts/office/pack.py": "z",
            }
        )
        names = {f.path: (f.resource_path, f.filename) for f in b.files}
        assert names == {
            # A package marker keeps its name, or the package stops importing
            # (anthropics/skills' office helpers came in as "init__.py").
            "scripts/__init__.py": ("__init__.py", "__init__.py"),
            "scripts/office/__init__.py": ("office/__init__.py", "__init__.py"),
            "scripts/office/pack.py": ("office/pack.py", "pack.py"),
        }
        assert b.skipped == []

    def test_a_name_starting_with_a_dash_is_cleaned_not_kept(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/-rf.md": "x"})
        assert [f.filename for f in b.files] == ["rf.md"]

    def test_hidden_files_and_macos_metadata_are_ignored(self):
        b = bundle_of(
            {
                "SKILL.md": SKILL_MD,
                ".gitignore": "x",
                "references/.DS_Store": "x",
                "__MACOSX/references/._a.md": "x",
                ".github/workflows/ci.yml": "x",
                "references/a.md": "a",
            }
        )
        assert [f.path for f in b.files] == ["references/a.md"]
        assert b.skipped == []

    def test_no_frontmatter_needs_a_description(self):
        with pytest.raises(BundleImportError, match="no description"):
            bundle_of({"SKILL.md": "Just a body."})

    def test_a_description_over_the_limit_is_refused(self):
        with pytest.raises(BundleImportError, match="description is 1025 characters"):
            bundle_of({"SKILL.md": f"---\nname: x\ndescription: {'d' * 1025}\n---\nbody"})

    def test_frontmatter_that_is_not_a_mapping_is_refused(self):
        with pytest.raises(BundleImportError, match="key: value"):
            parse_skill_md("---\n- a\n- b\n---\nbody")

    def test_commit_sha_comes_from_the_archive_comment(self):
        sha = b"e4bb5d960d87126ff3b3a8af1a104b7db67560ba"
        assert bundle_of({"SKILL.md": SKILL_MD}, comment=sha).commit_sha == sha.decode()
        assert bundle_of({"SKILL.md": SKILL_MD}, comment=b"made by zip").commit_sha is None

    def test_bundle_hash_tracks_what_would_be_written(self):
        one = bundle_of({"SKILL.md": SKILL_MD, "references/a.md": "a"})
        same = bundle_of({"x/SKILL.md": SKILL_MD, "x/references/a.md": "a"})
        edited = bundle_of({"SKILL.md": SKILL_MD, "references/a.md": "b"})
        assert one.bundle_hash == same.bundle_hash
        assert one.bundle_hash != edited.bundle_hash


# =============================================================================
# Choosing a skill in a multi-skill archive
# =============================================================================


class TestRoot:
    FILES = {
        "repo-HEAD/README.md": "repo",
        "repo-HEAD/skills/pdf/SKILL.md": SKILL_MD,
        "repo-HEAD/skills/pdf/REFERENCE.md": "pdf ref",
        "repo-HEAD/skills/docx/SKILL.md": SKILL_MD,
        "repo-HEAD/skills/docx/ooxml.md": "docx ref",
    }

    def test_several_skills_and_no_path_returns_candidates(self):
        load = load_bundle(make_zip(self.FILES))
        assert load.bundle is None
        assert load.candidates == ["skills/docx", "skills/pdf"]

    def test_a_path_picks_one_and_imports_only_its_files(self):
        b = bundle_of(self.FILES, path="skills/pdf/")
        assert b.root == "skills/pdf"
        assert [f.path for f in b.files] == ["REFERENCE.md"]

    def test_a_path_to_skill_md_itself_works(self):
        assert bundle_of(self.FILES, path="/skills/docx/SKILL.md").root == "skills/docx"

    def test_a_path_with_no_skill_md_names_what_exists(self):
        with pytest.raises(BundleImportError, match="skills/docx, skills/pdf"):
            load_bundle(make_zip(self.FILES), path="skills/xlsx")

    def test_no_skill_md_at_all(self):
        with pytest.raises(BundleImportError, match="No SKILL.md found"):
            load_bundle(make_zip({"README.md": "x"}))

    def test_a_nested_skill_md_is_skipped_not_merged(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "skills/other/SKILL.md": SKILL_MD}, path="")
        assert skipped(b) == {"skills/other/SKILL.md": "another skill's SKILL.md"}

    @pytest.mark.parametrize("bad", ["../etc", "a/../../b", "a\\b"])
    def test_a_traversing_path_argument_is_refused(self, bad):
        with pytest.raises(BundleImportError):
            normalize_bundle_path(bad)


# =============================================================================
# Security bounds: the whole archive is refused
# =============================================================================


class TestRefusals:
    @pytest.mark.parametrize(
        "name",
        ["../evil.md", "skill/../../evil.md", "/etc/passwd", "C:/evil.md", "refs\\evil.md"],
    )
    def test_unsafe_entry_names_refuse_the_archive(self, name):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", SKILL_MD)
            info = zipfile.ZipInfo("placeholder")
            info.filename = name  # past ZipInfo's own normalization
            zf.writestr(info, "x")
        with pytest.raises(BundleImportError, match="unsafe path"):
            load_bundle(buf.getvalue())

    def test_encrypted_entries_refuse_the_archive(self):
        with pytest.raises(BundleImportError, match="encrypted"):
            load_bundle(make_zip({"SKILL.md": SKILL_MD, "references/a.md": "a"}, encrypted={"references/a.md"}))

    def test_not_a_zip(self):
        with pytest.raises(BundleImportError, match="not a valid .zip"):
            load_bundle(b"PK\x03\x04 not really")

    def test_too_many_entries(self, monkeypatch):
        monkeypatch.setattr(bundle_import, "MAX_ARCHIVE_ENTRIES", 3)
        with pytest.raises(BundleImportError, match="entries"):
            load_bundle(make_zip({"SKILL.md": SKILL_MD, "a.md": "a", "b.md": "b", "c.md": "c"}))

    def test_too_many_files_for_one_skill(self):
        files = {"SKILL.md": SKILL_MD} | {f"references/r{i}.md": "x" for i in range(MAX_RESOURCES_PER_SKILL + 1)}
        with pytest.raises(BundleImportError, match=f"{MAX_RESOURCES_PER_SKILL + 1} files"):
            load_bundle(make_zip(files))

    def test_too_many_files_names_the_folders_that_hold_them(self):
        """The cap message says where the files are, so the author knows what to leave out."""
        files = {"SKILL.md": SKILL_MD}
        files |= {f"csharp/c{i}.md": "x" for i in range(30)}
        files |= {f"go/g{i}.md": "x" for i in range(30)}
        files |= {f"references/r{i}.md": "x" for i in range(5)}
        assert len(files) - 1 > MAX_RESOURCES_PER_SKILL
        with pytest.raises(BundleImportError) as e:
            load_bundle(make_zip(files))
        message = str(e.value)
        assert "Most are in csharp/ (30), go/ (30), references/ (5)." in message
        assert "Upload a .zip" in message

    def test_a_missing_skill_folder_says_how_many_more_were_found(self):
        files = {f"s{i:02d}/SKILL.md": SKILL_MD for i in range(13)}
        with pytest.raises(BundleImportError) as e:
            load_bundle(make_zip(files), path="nope")
        assert "s09 and 3 more." in str(e.value)

    def test_too_many_bytes_in_total(self, monkeypatch):
        monkeypatch.setattr(bundle_import, "MAX_IMPORT_TOTAL_BYTES", 10)
        with pytest.raises(BundleImportError, match="add up to"):
            load_bundle(make_zip({"SKILL.md": SKILL_MD, "a.md": "a" * 11}))

    def test_an_oversize_skill_md(self):
        big = SKILL_MD + "x" * bundle_import.MAX_SKILL_MD_BYTES
        with pytest.raises(BundleImportError, match="SKILL.md is larger"):
            load_bundle(make_zip({"SKILL.md": big}))


# =============================================================================
# Content rules: one file is skipped, with its reason
# =============================================================================


class TestSkips:
    def test_symlinks_are_skipped(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/link.md": "/etc/passwd"}, symlinks={"references/link.md"})
        assert skipped(b) == {"references/link.md": "symbolic link"}
        assert b.files == []

    @pytest.mark.parametrize("name", ["assets/icon.svg", "assets/page.html", "schemas/a.xml", "LICENSE"])
    def test_types_off_the_allowlist_are_skipped(self, name):
        b = bundle_of({"SKILL.md": SKILL_MD, name: "<x/>"})
        assert skipped(b)[name].startswith("file type not allowed")

    def test_a_file_over_one_mib_is_skipped_and_never_read_whole(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/big.md": "x" * (MAX_RESOURCE_BYTES + 1)})
        assert skipped(b) == {"references/big.md": "larger than 1 MiB"}

    def test_an_empty_file_is_skipped(self):
        assert skipped(bundle_of({"SKILL.md": SKILL_MD, "references/e.md": ""})) == {"references/e.md": "empty file"}

    def test_an_invalid_field_set_is_skipped_with_the_parser_message(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/field-set.budget.md": "| name |\n|---|\n| x |\n"})
        assert skipped(b)["references/field-set.budget.md"].startswith("invalid field set:")

    def test_an_office_skill_imports_whole_with_its_schemas(self):
        """anthropics/skills' xlsx/docx/pptx: 39 OOXML schemas, four of them five
        folders below scripts/, plus package markers. Every file comes across."""
        files = {"SKILL.md": SKILL_MD, "LICENSE.txt": "license"}
        files |= {f"scripts/office/helpers/{n}.py": "x" for n in ("__init__", "pptx_chart", "pptx_slide")}
        files |= {f"scripts/office/validators/{n}.py": "x" for n in ("__init__", "base", "docx", "pptx")}
        files |= {"scripts/office/validate.py": "x", "scripts/office/soffice.py": "x", "scripts/recalc.py": "x"}
        files |= {f"scripts/office/schemas/ISO-IEC29500-4_2016/s{i}.xsd": "<xs:schema/>" for i in range(31)}
        files |= {f"scripts/office/schemas/microsoft/m{i}.xsd": "<xs:schema/>" for i in range(4)}
        files |= {
            f"scripts/office/schemas/ecma/fouth-edition/opc-{n}.xsd": "<xs:schema/>"
            for n in ("contentTypes", "coreProperties", "digSig", "relationships")
        }
        b = bundle_of(files)
        assert b.skipped == []
        assert len(b.files) == len(files) - 1 <= MAX_RESOURCES_PER_SKILL
        by_path = {f.path: f for f in b.files}
        rels = by_path["scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd"]
        assert (rels.kind, rels.resource_path, rels.content_type) == (
            "script",
            "office/schemas/ecma/fouth-edition/opc-relationships.xsd",
            "text/plain",
        )

    def test_a_name_the_manifest_cannot_hold_is_cleaned(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/My Notes (v2).md": "x"})
        assert [f.filename for f in b.files] == ["My_Notes_v2_.md"]


# =============================================================================
# Diff
# =============================================================================


def _ref(filename, body, kind="reference"):
    from apis.shared.skills.resource_store import compute_content_hash

    return SkillResourceRef(
        filename=filename,
        content_hash=compute_content_hash(body.encode()),
        size=len(body),
        content_type="text/markdown",
        s3_key=f"skills/s/{kind}s/{filename}",
        kind=kind,
    )


def test_diff_reports_added_changed_unchanged_removed_and_skipped():
    target = SkillDefinition(
        skill_id="contract_redline",
        display_name="Contract",
        description="d",
        instructions="i",
        resources=[_ref("same.md", "same"), _ref("edit.md", "old"), _ref("gone.md", "gone"), _ref("moved.md", "m")],
    )
    b = bundle_of(
        {
            "SKILL.md": SKILL_MD,
            "references/same.md": "same",
            "references/edit.md": "new",
            "references/new.md": "new",
            "scripts/moved.md": "m",
            "assets/x.svg": "<svg/>",
        }
    )
    rows = {(r.filename or r.path): r.status for r in diff_files(b, target)}
    assert rows == {
        "same.md": "unchanged",
        "edit.md": "changed",
        "new.md": "added",
        "moved.md": "changed",  # same bytes, new kind
        "gone.md": "removed",
        "assets/x.svg": "skipped",
    }


# =============================================================================
# GitHub URLs: parsed, never fetched as given
# =============================================================================


class TestGitHubUrls:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/anthropics/skills", ("anthropics", "skills", None, "")),
            ("https://github.com/anthropics/skills.git", ("anthropics", "skills", None, "")),
            ("https://github.com/anthropics/skills/tree/main/skills/pdf", ("anthropics", "skills", "main", "skills/pdf")),
            ("https://www.github.com/a/b/blob/v1.2/x/SKILL.md", ("a", "b", "v1.2", "x")),
            ("https://raw.githubusercontent.com/a/b/e4bb5d9/x/y/SKILL.md", ("a", "b", "e4bb5d9", "x/y")),
            ("https://github.com/a/b/tree/main/My%20Skill", ("a", "b", "main", "My Skill")),
        ],
    )
    def test_accepted_shapes(self, url, expected):
        loc = parse_github_url(url)
        assert (loc.owner, loc.repo, loc.ref, loc.path) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "http://github.com/a/b",
            "https://github.com.evil.example/a/b",
            "https://evil.example/github.com/a/b",
            "https://codeload.github.com/a/b/zip/main",
            "https://api.github.com/repos/a/b",
            "https://user:pw@github.com/a/b",
            "https://github.com:8443/a/b",
            "https://github.com/a",
            "https://github.com/-a/b",
            "https://github.com/a/b/issues/1",
            "https://github.com/a/b/tree/feat%2F..%2Fx/p",
            "https://github.com/a/b/tree/main/../../etc",
            "file:///etc/passwd",
            "",
        ],
    )
    def test_rejected(self, url):
        with pytest.raises(GitHubSourceError):
            parse_github_url(url)

    def test_the_fetch_url_is_built_on_codeload(self):
        assert GitHubLocation("a", "b", None, "").archive_url == "https://codeload.github.com/a/b/zip/HEAD"
        assert GitHubLocation("a", "b", "v1.0", "x").archive_url == "https://codeload.github.com/a/b/zip/v1.0"


class TestFetch:
    LOC = GitHubLocation("anthropics", "skills", "main", "skills/pdf")

    async def _fetch(self, handler, **kw):
        seen = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        data = await fetch_archive(self.LOC, transport=httpx.MockTransport(record), **kw)
        return data, seen

    @pytest.mark.asyncio
    async def test_downloads_from_codeload_without_credentials(self):
        data, seen = await self._fetch(lambda r: httpx.Response(200, content=b"zipbytes"))
        assert data == b"zipbytes"
        assert [str(r.url) for r in seen] == ["https://codeload.github.com/anthropics/skills/zip/main"]
        assert "authorization" not in seen[0].headers
        assert "cookie" not in seen[0].headers

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_followed(self):
        with pytest.raises(GitHubSourceError) as e:
            await self._fetch(lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
        assert e.value.status == 502

    @pytest.mark.asyncio
    async def test_not_found_reads_as_not_public(self):
        with pytest.raises(GitHubSourceError, match="not public") as e:
            await self._fetch(lambda r: httpx.Response(404))
        assert e.value.status == 404

    @pytest.mark.asyncio
    async def test_not_found_with_a_path_explains_a_slash_in_the_branch(self):
        """``…/tree/feature/x/skills`` reads as branch ``feature``; the 404 says why."""
        with pytest.raises(GitHubSourceError) as e:
            await self._fetch(lambda r: httpx.Response(404))
        assert "has a '/' in it, it was read as 'main'" in str(e.value)

    @pytest.mark.asyncio
    async def test_not_found_without_a_path_has_no_branch_hint(self):
        loc = GitHubLocation("anthropics", "nope", None, "")
        with pytest.raises(GitHubSourceError) as e:
            await fetch_archive(loc, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        assert "'/'" not in str(e.value)

    @pytest.mark.asyncio
    async def test_a_declared_oversize_archive_is_refused(self):
        with pytest.raises(GitHubSourceError, match="larger than"):
            await self._fetch(
                lambda r: httpx.Response(200, headers={"content-length": "999999999"}, content=b"x"), max_bytes=10
            )

    @pytest.mark.asyncio
    async def test_a_streamed_oversize_archive_is_cut_off(self):
        with pytest.raises(GitHubSourceError, match="larger than"):
            await self._fetch(lambda r: httpx.Response(200, content=b"x" * 11), max_bytes=10)

    def test_production_uses_the_public_only_transport(self):
        from apis.app_api.web_sources.public_transport import PublicOnlyTransport

        assert github_source.PublicOnlyTransport is PublicOnlyTransport


class TestWorkflowSkips:
    """A saved workflow is checked on import beside a field set (workflow-designer §9.1)."""

    def test_an_invalid_workflow_is_skipped_with_the_parser_message(self):
        broken = "---\ntitle: T\n---\n\n| step | kind | worker | reads |\n|---|---|---|---|\n| a | agent | AST-1 | a |\n"
        b = bundle_of({"SKILL.md": SKILL_MD, "references/workflow.review.md": broken})
        assert skipped(b)["references/workflow.review.md"].startswith("invalid workflow:")

    def test_a_valid_workflow_is_imported(self):
        ok = "---\ntitle: T\n---\n\n| step | kind | worker | reads |\n|---|---|---|---|\n| a | agent | AST-1 | request |\n"
        b = bundle_of({"SKILL.md": SKILL_MD, "references/workflow.review.md": ok})
        assert "references/workflow.review.md" not in skipped(b)
        assert [f.filename for f in b.files] == ["workflow.review.md"]

    _CHECKLIST = (
        "---\ntitle: T\n---\n\n| step | kind | worker | reads |\n|---|---|---|---|\n"
        "| a | checklist | us.anthropic.claude-haiku-5-5-v1:0 | files |\n\n## Step: a\n\nField sets: budget\n"
    )
    _BUDGET = (
        "---\ntitle: Budget\nversion: 1\n---\n\n| name | title | type | find | enum | optional |\n"
        "|---|---|---|---|---|---|\n| cap | Cap | currency | the cap | | |\n"
    )

    def test_a_checklist_steps_set_may_come_later_in_the_archive(self):
        """workflow-designer §22.3: the sets are the import's own, checked once every file is read."""
        b = bundle_of(
            {
                "SKILL.md": SKILL_MD,
                "references/workflow.review.md": self._CHECKLIST,
                "references/field-set.budget.md": self._BUDGET,
            }
        )
        assert "references/workflow.review.md" not in skipped(b)
        assert sorted(f.filename for f in b.files) == ["field-set.budget.md", "workflow.review.md"]

    def test_a_checklist_step_whose_set_is_missing_is_skipped(self):
        b = bundle_of({"SKILL.md": SKILL_MD, "references/workflow.review.md": self._CHECKLIST})
        reason = skipped(b)["references/workflow.review.md"]
        assert reason.startswith("invalid workflow:")
        assert "the field set 'budget' is not in this skill" in reason

    def test_a_checklist_steps_set_with_a_list_field_is_imported(self):
        listed = self._BUDGET.replace("| cap | Cap | currency |", "| cap | Cap | list |")
        assert listed != self._BUDGET
        b = bundle_of(
            {"SKILL.md": SKILL_MD, "references/workflow.review.md": self._CHECKLIST, "references/field-set.budget.md": listed}
        )
        assert "references/workflow.review.md" not in skipped(b)
