"""Nested paths in a skill's bundle (agentskills-conformance F10).

What these pin, in the order a mistake would hurt:

1. A row written before nested paths reads back unchanged: ``path == filename``,
   the same S3 key, the same version content hash (a pin must not read as
   "Update available" after deploy) and the same "Available reference files"
   bytes (the prompt-cache prefix).
2. A refused path shape is refused on every write path: the REST upload, the
   bundle import and the authoring tools (which take flat names only).
3. Two same-named files in different folders are both kept, both readable by
   path, ambiguous by bare name, exported at their paths, and round-trip.
4. Field sets and script resolution keep their top-level, strict rules.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.main_agent.skills import strands_mapping as sm
from agents.main_agent.skills.skill_scripts import SkillScriptError, resolve_skill_script
from apis.app_api.skills import bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.app_api.skills.field_sets import list_field_sets
from apis.shared.auth import get_current_user_from_session
from apis.shared.extraction import resolver
from apis.shared.extraction.field_sets import field_set_name_of_ref
from apis.shared.skills.bundle_export import build_skill_zip
from apis.shared.skills.bundle_import import load_bundle
from apis.shared.skills.models import SkillDefinition, SkillResourceRef
from apis.shared.skills.resource_paths import MAX_PATH_SEGMENTS, path_problem
from apis.shared.skills.resource_store import resource_key
from apis.shared.skills.versions import SkillVersion, content_hash

SKILL_MD = """---
name: contract-redline
description: Redline a contract against a playbook.
---

Read `references/legal/nda.md` for vendors and `references/hr/nda.md` for staff.
"""

NESTED = {
    "contract-redline/SKILL.md": SKILL_MD,
    "contract-redline/references/legal/nda.md": "Legal NDA rules",
    "contract-redline/references/hr/nda.md": "HR NDA rules",
    "contract-redline/references/overview.md": "Overview",
}

FIELD_SET = """---
title: NSF Solicitation
version: 2
---

| name | find |
|---|---|
| deadline | the deadline |
"""

# A path for each refused shape, and the word its reason must carry.
REFUSED_PATHS = {
    "../escape.md": "'..'",
    "legal/../../escape.md": "'..'",
    "/etc/passwd.md": "leading '/'",
    "legal\\nda.md": "backslash",
    "legal//nda.md": "empty folder",
    "legal/": "empty folder",
    "nda\x00.md": "NUL",
    "a/b/c/d/e/f.md": f"at most {MAX_PATH_SEGMENTS}",
    "legal/bad name!/nda.md": "not allowed",
    "legal/-rf.md": "not allowed",
    ".hidden/nda.md": "not allowed",
    "": "empty",
}


def make_zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _old_item(filename="forms.md", kind="reference", skill_id="pdf_workflows"):
    """A manifest entry exactly as a row written before nested paths stores it."""
    subdir = {"reference": "references", "script": "scripts", "asset": "assets"}[kind]
    return {
        "filename": filename,
        "contentHash": hashlib.sha256(filename.encode()).hexdigest(),
        "size": 10,
        "contentType": "text/markdown",
        "s3Key": f"skills/{skill_id}/{subdir}/{filename}",
        "kind": kind,
    }


def _old_row(resources):
    return {
        "PK": "SKILL#pdf_workflows",
        "SK": "METADATA",
        "skillId": "pdf_workflows",
        "displayName": "PDF Workflows",
        "description": "Work with PDF files.",
        "instructions": "# PDF",
        "resources": resources,
        "status": "active",
    }


def _pre_nesting_hash(skill: SkillDefinition) -> str:
    """``versions.content_hash`` exactly as it was written before nested paths."""
    payload = {
        "display_name": skill.display_name,
        "description": skill.description,
        "instructions": skill.instructions,
        "compose": list(skill.compose),
        "allowed_tools": list(skill.allowed_tools),
        "skill_metadata": skill.skill_metadata,
        "resources": [[r.filename, r.kind, r.content_hash] for r in skill.resources],
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# =============================================================================
# 1. Rows written before nested paths
# =============================================================================


class TestOldRowsAreUnchanged:
    def test_an_old_entry_reads_back_with_path_equal_to_filename(self):
        skill = SkillDefinition.from_dynamo_item(
            _old_row([_old_item("forms.md"), _old_item("engine.py", kind="script")])
        )
        assert [(r.path, r.filename) for r in skill.resources] == [
            ("forms.md", "forms.md"),
            ("engine.py", "engine.py"),
        ]
        assert [r.s3_key for r in skill.resources] == [
            resource_key("pdf_workflows", "reference", "forms.md"),
            resource_key("pdf_workflows", "script", "engine.py"),
        ]

    def test_an_old_entry_is_written_back_byte_identical(self):
        items = [_old_item("forms.md"), _old_item("engine.py", kind="script")]
        skill = SkillDefinition.from_dynamo_item(_old_row(items))
        assert skill.to_dynamo_item()["resources"] == items

    def test_the_version_content_hash_is_unchanged(self):
        skill = SkillDefinition.from_dynamo_item(
            _old_row([_old_item("forms.md"), _old_item("engine.py", kind="script")])
        )
        assert content_hash(skill) == _pre_nesting_hash(skill)

    def test_an_old_version_row_reads_back_and_hashes_the_same(self):
        skill = SkillDefinition.from_dynamo_item(_old_row([_old_item("forms.md")]))
        version = SkillVersion.from_dynamo_item(
            {"skillId": "pdf_workflows", "version": 1, "contentHash": "h", "displayName": "PDF Workflows",
             "resources": [_old_item("forms.md")]}
        )
        assert version.resources[0].path == "forms.md"
        assert "path" not in version.model_copy(update={"version": 1}).to_dynamo_item()["resources"][0]
        assert content_hash(skill) == _pre_nesting_hash(skill)

    def test_the_bundled_files_listing_is_exactly_the_flat_listing(self):
        """A skill with only top-level files lists exactly as the grouped "Bundled
        files" section writes it for a flat manifest (the wording is that
        section's; this pins that nested paths add nothing to it)."""
        skill = SkillDefinition.from_dynamo_item(
            _old_row([_old_item("forms.md"), _old_item("engine.py", kind="script")])
        )
        assert sm._reference_files_section(skill) == (
            "\n\n## Bundled files\n"
            "Load any of these with the `read_skill_file` tool "
            "(skill_name `pdf-workflows`, path = one of the entries below):\n\n"
            "### References\n"
            "- references/forms.md\n\n"
            "### Scripts\n"
            "- scripts/engine.py"
        )

    def test_a_nested_file_is_listed_at_its_manifest_path_in_its_group(self):
        refs = [
            _old_item("forms.md"),
            {**_old_item("nda.md"), "path": "legal/nda.md",
             "s3Key": "skills/pdf_workflows/references/legal/nda.md"},
            {**_old_item("check.py", kind="script"), "path": "tools/check.py",
             "s3Key": "skills/pdf_workflows/scripts/tools/check.py"},
        ]
        skill = SkillDefinition.from_dynamo_item(_old_row(refs))
        listing = sm._reference_files_section(skill)
        assert listing.endswith(
            "### References\n"
            "- references/forms.md\n"
            "- references/legal/nda.md\n\n"
            "### Scripts\n"
            "- scripts/tools/check.py"
        )
        assert "- references/nda.md" not in listing

    def test_a_nested_entry_stores_and_hashes_its_path(self):
        flat = SkillResourceRef(filename="nda.md", content_hash="h", size=1, content_type="text/markdown", s3_key="k")
        nested = flat.model_copy(update={"path": "legal/nda.md"})
        assert "path" not in flat.to_item()
        assert nested.to_item()["path"] == "legal/nda.md"
        assert SkillResourceRef.from_item(nested.to_item()).path == "legal/nda.md"
        base = dict(skill_id="nested_skill", display_name="S", description="d", instructions="i")
        assert content_hash(SkillDefinition(**base, resources=[flat])) != content_hash(
            SkillDefinition(**base, resources=[nested])
        )

    def test_a_path_alone_gives_the_filename(self):
        ref = SkillResourceRef(path="legal/nda.md", content_hash="h", size=1, content_type="t", s3_key="k", filename="")
        assert ref.filename == "nda.md"


# =============================================================================
# 2. Refused path shapes, on every write path
# =============================================================================


@pytest.mark.parametrize("bad,why", list(REFUSED_PATHS.items()), ids=list(map(repr, REFUSED_PATHS)))
def test_the_path_rule_refuses_each_shape(bad, why):
    problem = path_problem(bad)
    assert problem is not None and why.lower() in problem.lower()


@pytest.mark.parametrize(
    "good",
    ["nda.md", "legal/nda.md", "a/b/c/d.md", "a/b/c/d/e.md", "v1.2/notes_x-y.md", "__init__.py", "office/helpers/__init__.py"],
)
def test_the_path_rule_accepts_up_to_five_segments(good):
    assert path_problem(good) is None


def _skill_app(user, service, monkeypatch):
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true")
    monkeypatch.setattr(bundle_import_routes, "get_user_skill_service", lambda: service)
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: service)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    with _skill_app(author_user, user_skill_service, monkeypatch) as c:
        yield c


@pytest.fixture()
def skill_id(client):
    created = client.post(
        "/skills/mine",
        json={"displayName": "Contract Redline", "description": "Redline contracts.", "instructions": "# Redline"},
    )
    assert created.status_code == 200, created.text
    return created.json()["skillId"]


def _upload(client, skill_id, name, body=b"x", **data):
    return client.post(
        f"/skills/mine/{skill_id}/resources",
        files={"file": (name, body, "text/markdown")},
        data=data,
    )


@pytest.mark.parametrize("bad", list(REFUSED_PATHS), ids=list(map(repr, REFUSED_PATHS)))
def test_the_rest_upload_refuses_each_shape(client, skill_id, bad):
    if bad == "":
        # An empty form field is "no path": the upload falls back to the filename.
        return
    resp = _upload(client, skill_id, "nda.md", path=bad)
    assert resp.status_code == 400, resp.text
    assert client.get(f"/skills/mine/{skill_id}/resources").json()["resources"] == []


@pytest.mark.parametrize(
    "entry,reason",
    [
        ("references/a/b/c/d/e/f.md", f"more than {MAX_PATH_SEGMENTS} folders deep"),
        ("references/legal//nda.md", "name is not allowed"),
        ("references/legal/ü/nda.md", "name is not allowed"),
    ],
)
def test_the_bundle_import_skips_a_refused_shape_with_its_reason(entry, reason):
    bundle = load_bundle(make_zip({"SKILL.md": SKILL_MD, entry: "x", "references/ok.md": "ok"})).bundle
    assert [f.resource_path for f in bundle.files] == ["ok.md"]
    assert {s.path: s.reason for s in bundle.skipped}[entry].startswith(reason)


@pytest.mark.parametrize("bad", ["../escape.md", "/abs/nda.md", "a\\b.md"])
def test_the_bundle_import_refuses_a_traversing_archive(bad):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        info = zipfile.ZipInfo("x")
        info.filename = f"references/{bad}" if not bad.startswith("/") else bad
        zf.writestr(info, "x")
    from apis.shared.skills.bundle_import import BundleImportError

    with pytest.raises(BundleImportError):
        load_bundle(buf.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["legal/nda.md", "../nda.md", "/nda.md", "a\\b.md", "nda\x00.md"])
async def test_the_authoring_tools_write_flat_names_only(user_skill_service, author_user, bad):
    """``my_skill_write_file`` goes through ``write_text_file``, which takes a filename, never a path."""
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Flat Only", description="d", instructions="i"
    )
    with pytest.raises(ValueError):
        await user_skill_service.write_text_file(skill.skill_id, "reference", bad, "x", author_user)
    assert (await user_skill_service.list_resources(skill.skill_id, author_user)) == []


# =============================================================================
# 3. Two nda.md in two folders
# =============================================================================


class TestTwoSameNamedFiles:
    def _import(self, client, files):
        resp = client.post(
            "/skills/mine/import",
            files={"file": ("bundle.zip", make_zip(files), "application/zip")},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["skill"]["skillId"]

    def test_both_are_stored_at_their_paths(self, client, user_skill_service, author_user):
        sid = self._import(client, NESTED)
        resources = client.get(f"/skills/mine/{sid}/resources").json()["resources"]
        assert sorted((r["path"], r["filename"], r["s3Key"]) for r in resources) == [
            ("hr/nda.md", "nda.md", f"skills/{sid}/references/hr/nda.md"),
            ("legal/nda.md", "nda.md", f"skills/{sid}/references/legal/nda.md"),
            ("overview.md", "overview.md", f"skills/{sid}/references/overview.md"),
        ]

    def test_read_skill_file_reads_each_by_path_and_refuses_the_bare_name(
        self, client, user_skill_service, author_user, monkeypatch
    ):
        sid = self._import(client, NESTED)
        skill = _sync(user_skill_service.get_my_skill(sid, author_user))
        monkeypatch.setattr(sm, "get_skill_resource_store", lambda: user_skill_service.bundle_files.resource_store)
        read = sm.make_read_skill_file_tool([skill])
        name = sm.slugify_skill_name(sid)
        assert read(skill_name=name, path="references/legal/nda.md") == "Legal NDA rules"
        assert read(skill_name=name, path="legal/nda.md") == "Legal NDA rules"
        assert read(skill_name=name, path="references/hr/nda.md") == "HR NDA rules"
        assert read(skill_name=name, path="hr/nda.md") == "HR NDA rules"
        assert read(skill_name=name, path="overview.md") == "Overview"

        ambiguous = read(skill_name=name, path="nda.md")
        assert "More than one file" in ambiguous
        assert "references/hr/nda.md" in ambiguous and "references/legal/nda.md" in ambiguous

        listing = sm._reference_files_section(skill)
        assert "- references/hr/nda.md\n- references/legal/nda.md\n- references/overview.md" in listing

    def test_rest_download_and_delete_address_a_file_by_path(self, client):
        sid = self._import(client, NESTED)
        base = f"/skills/mine/{sid}/resources"
        assert client.get(f"{base}/legal/nda.md").content == b"Legal NDA rules"
        assert client.get(f"{base}/legal%2Fnda.md").content == b"Legal NDA rules"
        assert client.get(f"{base}/overview.md").content == b"Overview"
        bare = client.get(f"{base}/nda.md")
        assert bare.status_code == 400
        assert "hr/nda.md" in bare.json()["detail"] and "legal/nda.md" in bare.json()["detail"]

        after = client.delete(f"{base}/hr/nda.md")
        assert after.status_code == 200
        assert sorted(r["path"] for r in after.json()["resources"]) == ["legal/nda.md", "overview.md"]
        # With one left, the bare name names it again.
        assert client.get(f"{base}/nda.md").content == b"Legal NDA rules"

    def test_the_export_holds_both_at_their_paths_and_round_trips(self, client, user_skill_service, author_user):
        sid = self._import(client, NESTED)
        exported = client.get(f"/skills/mine/{sid}/export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as zf:
            names = sorted(zf.namelist())
            assert zf.read(next(n for n in names if n.endswith("references/legal/nda.md"))) == b"Legal NDA rules"
        slug = names[0].split("/", 1)[0]
        assert names == [
            f"{slug}/SKILL.md",
            f"{slug}/references/hr/nda.md",
            f"{slug}/references/legal/nda.md",
            f"{slug}/references/overview.md",
        ]

        again = client.post(
            "/skills/mine/import/preview",
            files={"file": ("bundle.zip", exported.content, "application/zip")},
            data={"skillId": sid},
        )
        assert again.status_code == 200, again.text
        assert {r["path"]: r["status"] for r in again.json()["files"]} == {
            "references/hr/nda.md": "unchanged",
            "references/legal/nda.md": "unchanged",
            "references/overview.md": "unchanged",
        }

    def test_a_rest_upload_with_a_path_lands_beside_a_same_named_file(self, client, skill_id):
        assert _upload(client, skill_id, "nda.md", b"top").status_code == 200
        resp = _upload(client, skill_id, "ignored.md", b"nested", path="legal/nda.md")
        assert resp.status_code == 200, resp.text
        assert sorted((r["path"], r["filename"]) for r in resp.json()["resources"]) == [
            ("legal/nda.md", "nda.md"),
            ("nda.md", "nda.md"),
        ]
        # The top-level file is named exactly by its filename, as before.
        assert client.get(f"/skills/mine/{skill_id}/resources/nda.md").content == b"top"


def _sync(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =============================================================================
# Same path under two kind directories
# =============================================================================


def test_the_same_path_under_two_kind_directories_keeps_the_first_in_archive_order():
    bundle = load_bundle(
        make_zip(
            {
                "SKILL.md": SKILL_MD,
                "scripts/tools/x.py": "script",
                "references/tools/x.py": "reference",
            }
        )
    ).bundle
    assert [(f.path, f.kind, f.resource_path) for f in bundle.files] == [
        ("references/tools/x.py", "reference", "tools/x.py")
    ]
    reason = {s.path: s.reason for s in bundle.skipped}["scripts/tools/x.py"]
    assert "references/tools/x.py" in reason and "scripts/tools/x.py" in reason


# =============================================================================
# 4. Field sets and scripts keep their rules
# =============================================================================


def _ref(path, kind="reference"):
    subdir = {"reference": "references", "script": "scripts", "asset": "assets"}[kind]
    return SimpleNamespace(
        filename=path.rsplit("/", 1)[-1],
        path=path,
        kind=kind,
        content_type="text/plain",
        size=10,
        s3_key=f"skills/s/{subdir}/{path}",
        content_hash="h",
    )


def _record(resources):
    return SimpleNamespace(skill_id="contract_redline", resources=resources)


class TestFieldSets:
    def test_a_nested_file_named_like_a_field_set_is_not_one(self):
        nested = _ref("legal/field-set.nsf.md")
        assert field_set_name_of_ref(nested) is None
        rec = _record([nested])
        assert resolver.declared_field_sets([rec]) == {}
        assert resolver.turn_has_field_set([rec]) is False
        assert list_field_sets([SkillResourceRef(path="legal/field-set.nsf.md", filename="field-set.nsf.md",
                                                 content_hash="h", size=1, content_type="t", s3_key="k")]) == []

    def test_a_top_level_field_set_still_resolves_for_document_extract(self, monkeypatch):
        top = _ref("field-set.nsf.md")
        rec = _record([_ref("legal/field-set.nsf.md"), top])

        class Store:
            def get(self, key):
                assert key == top.s3_key
                return FIELD_SET.encode()

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())
        assert resolver.field_set_names([rec]) == ["nsf"]
        assert resolver.resolve_field_set([rec], "nsf").name == "nsf"

    @pytest.mark.asyncio
    async def test_a_nested_field_set_named_file_is_not_validated_as_one(self, user_skill_service, author_user):
        skill = await user_skill_service.create_my_skill(author_user, display_name="FS", description="d", instructions="i")
        refs = await user_skill_service.add_resource(
            skill.skill_id, "x.md", b"not a field set", "text/markdown", author_user,
            path="legal/field-set.nsf.md",
        )
        assert [r.path for r in refs] == ["legal/field-set.nsf.md"]
        with pytest.raises(ValueError):
            await user_skill_service.add_resource(
                skill.skill_id, "field-set.nsf.md", b"not a field set", "text/markdown", author_user
            )


class TestScripts:
    def test_a_word_script_resolves_by_its_manifest_path_and_bare_name(self):
        rec = _record([_ref("x.py", kind="script")])
        assert resolve_skill_script([rec], "contract-redline", "scripts/x.py").path == "x.py"
        assert resolve_skill_script([rec], "contract-redline", "x.py").path == "x.py"

    def test_a_nested_script_resolves_only_by_its_full_manifest_path(self):
        rec = _record([_ref("tools/y.py", kind="script")])
        assert resolve_skill_script([rec], "contract-redline", "scripts/tools/y.py").path == "tools/y.py"
        for guess in ("y.py", "tools/y.py", "scripts/y.py"):
            with pytest.raises(SkillScriptError):
                resolve_skill_script([rec], "contract-redline", guess)


# =============================================================================
# Audit records identify files by path
# =============================================================================


class TestAuditRecords:
    def _skill(self, refs):
        return SkillDefinition.from_dynamo_item(_old_row(refs))

    def test_two_nested_files_with_one_filename_are_both_recorded(self):
        from apis.shared.skills.audit import skill_state

        legal = {**_old_item("nda.md"), "path": "legal/nda.md", "contentHash": "a" * 64}
        hr = {**_old_item("nda.md"), "path": "hr/nda.md", "contentHash": "b" * 64}
        assert skill_state(self._skill([legal, hr]))["files"] == {
            "legal/nda.md": "a" * 64,
            "hr/nda.md": "b" * 64,
        }

    def test_a_top_level_file_is_recorded_under_its_filename_as_before(self):
        from apis.shared.skills.audit import skill_state

        item = _old_item("forms.md")
        assert skill_state(self._skill([item]))["files"] == {"forms.md": item["contentHash"]}

    def test_moving_a_file_to_another_folder_is_not_a_no_op(self):
        from apis.shared.skills.audit import changed_keys, is_no_op, skill_state

        top = _old_item("nda.md")
        moved = {**top, "path": "legal/nda.md", "s3Key": "skills/pdf_workflows/references/legal/nda.md"}
        before, after = self._skill([top]), self._skill([moved])
        assert not is_no_op(before, after)
        assert "files" in changed_keys(skill_state(before), skill_state(after))
        assert is_no_op(before, self._skill([dict(top)]))
