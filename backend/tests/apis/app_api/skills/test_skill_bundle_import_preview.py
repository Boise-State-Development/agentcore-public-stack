"""Bundle import: the id from the frontmatter name, and what the preview reports.

What these pin: an imported ``mcp-builder`` gets the id ``mcp_builder`` and exports
back as ``name: mcp-builder``; on a collision the id is suffixed and the preview
says the stored name differs; a name that cannot be an id falls back to the
display name; ``importSource.originalName`` keeps the frontmatter's name; a
refresh returns unified diffs of description and instructions (empty when
nothing changed, cut at 400 lines with a flag) and the previous commit; and the
preview lists frontmatter problems and unsafe characters (first 20, counted
after that).
"""

from __future__ import annotations

import io
import zipfile

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import bundle_import_core, bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills.bundle_import import MAX_DIFF_LINES, text_diff
from apis.shared.skills.user_service import skill_id_from_frontmatter_name


def _skill_md(name: str, description: str = "Build MCP servers.", body: str = "Step one.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def make_zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", "true")
    monkeypatch.setattr(bundle_import_routes, "get_user_skill_service", lambda: user_skill_service)
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        yield c


def _form(files, **data):
    return {"files": {"file": ("bundle.zip", make_zip(files), "application/zip")}, "data": data}


def _preview(client, files, **data):
    resp = client.post("/skills/mine/import/preview", **_form(files, **data))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create(client, files, **data):
    resp = client.post("/skills/mine/import", **_form(files, **data))
    assert resp.status_code == 200, resp.text
    return resp.json()["skill"]


def _export_frontmatter(client, skill_id) -> dict:
    resp = client.get(f"/skills/mine/{skill_id}/export")
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        (skill_md,) = [n for n in archive.namelist() if n.endswith("/SKILL.md")]
        text = archive.read(skill_md).decode("utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def _codes(preview, level=None):
    return [d["code"] for d in preview["diagnostics"] if level is None or d["level"] == level]


MCP = {"mcp-builder/SKILL.md": _skill_md("mcp-builder")}


class TestIdFromName:
    def test_the_id_comes_from_the_name_and_exports_back(self, client):
        preview = _preview(client, MCP)
        assert preview["skill"]["storedName"] == "mcp-builder"
        assert "stored_name_differs" not in _codes(preview)

        skill = _create(client, MCP, displayName="Something else entirely")
        assert skill["skillId"] == "mcp_builder"
        assert skill["displayName"] == "Something else entirely"
        assert skill["importSource"]["originalName"] == "mcp-builder"
        assert _export_frontmatter(client, "mcp_builder")["name"] == "mcp-builder"

    def test_a_taken_id_is_suffixed_and_the_preview_says_so(self, client):
        _create(client, MCP)
        preview = _preview(client, MCP)
        assert preview["skill"]["name"] == "mcp-builder"
        assert preview["skill"]["storedName"] == "mcp-builder-2"
        (warning,) = [d for d in preview["diagnostics"] if d["code"] == "stored_name_differs"]
        assert warning["level"] == "warning"
        assert "mcp-builder-2" in warning["message"] and "already exists" in warning["message"]

        second = _create(client, MCP)
        assert second["skillId"] == "mcp_builder_2"
        assert second["importSource"]["originalName"] == "mcp-builder"

    @pytest.mark.parametrize(
        "name",
        [
            "ab",  # converts to an id under three characters
            "a" * 51,  # over 50 once converted
            "9-lives",  # an id must start with a letter
            "Bad_Name",  # fails the agentskills.io name rule
        ],
    )
    def test_a_name_that_cannot_be_an_id_falls_back_to_the_display_name(self, name):
        assert skill_id_from_frontmatter_name(name) is None

    def test_the_fallback_allocates_from_the_display_name(self, client):
        files = {"x/SKILL.md": _skill_md("ab")}
        preview = _preview(client, files)
        skill = _create(client, files)
        # humanize("ab") = "Ab" → slugify pads a short stem
        assert skill["skillId"] == "ab_x"
        assert preview["skill"]["storedName"] == "ab-x"
        (warning,) = [d for d in preview["diagnostics"] if d["code"] == "stored_name_differs"]
        assert "cannot be used as a skill id" in warning["message"]

    def test_a_50_character_name_keeps_its_id_and_a_suffix_still_fits(self, user_skill_service, client):
        name = "a" * 50
        assert skill_id_from_frontmatter_name(name) == name
        files = {"x/SKILL.md": _skill_md(name)}
        assert _create(client, files)["skillId"] == name
        second = _create(client, files)["skillId"]
        assert second == "a" * 48 + "_2"


class TestRefreshDiff:
    V1 = {"s/SKILL.md": _skill_md("diff-skill", body="Line one.\nLine two.\nLine three.")}
    V2 = {"s/SKILL.md": _skill_md("diff-skill", body="Line one.\nLine 2, edited.\nLine three.")}

    def test_a_create_preview_has_no_diff(self, client):
        preview = _preview(client, self.V1)
        assert preview["instructionsDiff"] is None
        assert preview["descriptionDiff"] is None
        assert preview["previousCommitSha"] is None

    def test_changed_instructions_come_back_as_a_unified_diff(self, client):
        sid = _create(client, self.V1)["skillId"]
        preview = _preview(client, self.V2, skillId=sid)
        diff = preview["instructionsDiff"]
        assert diff["truncated"] is False
        assert "-Line two." in diff["diff"].splitlines()
        assert "+Line 2, edited." in diff["diff"].splitlines()
        assert " Line one." in diff["diff"].splitlines()
        assert preview["descriptionDiff"] == {"diff": "", "truncated": False}

    def test_an_unchanged_refresh_returns_empty_diffs(self, client):
        sid = _create(client, self.V1)["skillId"]
        preview = _preview(client, self.V1, skillId=sid)
        assert preview["instructionsDiff"] == {"diff": "", "truncated": False}
        assert preview["descriptionDiff"] == {"diff": "", "truncated": False}

    def test_the_previous_commit_is_returned_beside_the_new_one(self, client, monkeypatch):
        shas = iter(["a" * 40, "b" * 40])

        async def fake_fetch(location):
            data = make_zip({f"repo-main/{k}": v for k, v in self.V1.items()})
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, "a") as zf:
                zf.comment = next(shas).encode()
            return buf.getvalue()

        monkeypatch.setattr(bundle_import_core, "fetch_archive", fake_fetch)
        url = "https://github.com/example/repo/tree/main/s"
        created = client.post("/skills/mine/import", data={"url": url}).json()["skill"]
        preview = client.post(
            "/skills/mine/import/preview", data={"url": url, "skillId": created["skillId"]}
        ).json()
        assert preview["previousCommitSha"] == "a" * 40
        assert preview["source"]["commitSha"] == "b" * 40

    def test_a_long_diff_is_cut_at_400_lines_and_flagged(self):
        old = "\n".join(f"old {i}" for i in range(500))
        new = "\n".join(f"new {i}" for i in range(500))
        cut = text_diff(old, new, label="SKILL.md")
        assert cut.truncated is True
        assert len(cut.diff.splitlines()) == MAX_DIFF_LINES
        short = text_diff("a\nb", "a\nc", label="SKILL.md")
        assert short.truncated is False


class TestPreviewDiagnostics:
    def test_frontmatter_problems_are_listed(self, client):
        files = {"s/SKILL.md": "---\nname: diag-skill\ndescription: d\nlicense: 7\nx-extra: y\n---\nB"}
        preview = _preview(client, files)
        by_code = {d["code"]: d for d in preview["diagnostics"]}
        # The import keeps a non-string license under metadata rather than refusing,
        # so the preview says so as a warning instead of calling it an error.
        assert by_code["license_invalid"]["level"] == "warning"
        assert "kept and written under metadata" in by_code["license_invalid"]["message"]
        assert by_code["unknown_field"]["field"] == "x-extra"

    def test_no_preview_diagnostic_is_an_error_for_a_bundle_that_imports(self, client):
        """Every frontmatter error an import lets through is repaired, so none says "error"."""
        files = {
            "s/SKILL.md": (
                "---\nname: ZeeLin Auto-PPT\ndescription: d\nlicense: 7\n"
                "compatibility: [a, b]\nmetadata:\n  version: 2\n---\nB"
            )
        }
        preview = _preview(client, files)
        levels = {d["code"]: d["level"] for d in preview["diagnostics"]}
        assert "error" not in levels.values(), levels
        for code in ("name_invalid", "license_invalid", "compatibility_invalid", "metadata_value_not_string"):
            assert levels[code] == "warning", code
        name = next(d for d in preview["diagnostics"] if d["code"] == "name_invalid")
        assert "stored under a valid name instead" in name["message"]
        assert _create(client, files)["skillId"]

    def test_the_colon_repair_is_reported(self, client):
        files = {"s/SKILL.md": "---\nname: colon-skill\ndescription: Use when: asked\n---\nB"}
        preview = _preview(client, files)
        assert preview["skill"]["description"] == "Use when: asked"
        assert "frontmatter_repaired" in _codes(preview, "warning")

    def test_unsafe_characters_in_the_body_and_text_files_name_file_and_position(self, client):
        files = {
            "s/SKILL.md": _skill_md("unsafe-skill", body="ok\nx‮y"),
            "s/references/notes.md": "fine\r\nzero​width",
            "s/assets/logo.png": b"\x89PNG\x00\x01",
        }
        preview = _preview(client, files)
        messages = [d["message"] for d in preview["diagnostics"] if d["code"] == "unsafe_character"]
        assert len(messages) == 2
        assert "SKILL.md" in messages[0] and "U+202E" in messages[0] and "line 2, column 2" in messages[0]
        assert "references/notes.md" in messages[1] and "U+200B" in messages[1]

    def test_a_leading_byte_order_mark_is_not_flagged_but_a_later_one_is(self, client):
        files = {
            "s/SKILL.md": _skill_md("bom-skill"),
            "s/scripts/office/schemas/opc-contentTypes.xsd": "﻿<?xml version='1.0'?>\n<schema/>\n",
            "s/references/notes.md": "﻿intro\nmid﻿word\n",
        }
        preview = _preview(client, files)
        messages = [d["message"] for d in preview["diagnostics"] if d["code"] == "unsafe_character"]
        assert len(messages) == 1
        assert "references/notes.md" in messages[0] and "U+FEFF" in messages[0]
        assert "line 2, column 4" in messages[0]

    def test_only_the_first_20_are_listed_and_the_rest_counted(self, client):
        files = {"s/SKILL.md": _skill_md("many-unsafe", body="​" * 25)}
        preview = _preview(client, files)
        assert _codes(preview).count("unsafe_character") == 20
        (more,) = [d for d in preview["diagnostics"] if d["code"] == "unsafe_character_more"]
        assert more["message"].startswith("5 more")

    def test_a_clean_bundle_has_no_diagnostics(self, client):
        assert _preview(client, MCP)["diagnostics"] == []


def test_create_records_original_name_on_reimport_too(client):
    sid = _create(client, MCP)["skillId"]
    resp = client.post(f"/skills/mine/{sid}/import", **_form(MCP))
    assert resp.status_code == 200
    assert resp.json()["skill"]["importSource"]["originalName"] == "mcp-builder"
    # The old shape still reads: a row without the field validates.
    from apis.shared.skills.models import SkillImportSource

    old = SkillImportSource.model_validate({"type": "zip", "bundleHash": "h", "importedAt": "t"})
    assert old.original_name is None
    assert "originalName" not in old.to_item()
