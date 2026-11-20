"""The two "Import SKILL.md" parse routes and the colon-quoting retry behind them.

What these pin: both routes return the same fields from the same parser and
mapping as bundle import (nested ``metadata``, ``license``, ``compatibility`` and
``allowed-tools`` intact); they refuse only a file that cannot be read (over
200 KiB, not UTF-8, a frontmatter the bounded parser refuses) and report every
other problem as a diagnostic; authentication runs before the body is read;
the admin route needs the ``admin.skills`` scope; the user route is not behind
the bundle-import flag; and neither is shadowed by a parameterised route.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_skill_routes
from apis.app_api.skills import bundle_import_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.app_api.skills import skill_md_parse
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth import rbac
from apis.shared.skills import skill_md
from apis.shared.skills.skill_md import SkillMdError, parse_skill_md, quote_colon_values
from apis.shared.skills.skill_md_import import MAX_SKILL_MD_UPLOAD_BYTES
from tests.conftest import override_admin_auth

NESTED = """---
name: pdf-tools
description: Fill and merge PDFs.
license: Apache-2.0
compatibility: Needs network access and uv.
allowed-tools: Read Bash(python:*)
metadata:
  author: example-org
  version: "1.2"
x-custom: kept
---

# PDF tools

Use pypdf.
"""

EXPECTED_METADATA = {
    "license": "Apache-2.0",
    "compatibility": "Needs network access and uv.",
    "metadata": {"author": "example-org", "version": "1.2"},
    "x-custom": "kept",
}

USER_PATH = "/skills/mine/import/parse"
ADMIN_PATH = "/admin/skills/import/parse"


def _file(content, filename="SKILL.md"):
    data = content.encode("utf-8") if isinstance(content, str) else content
    return {"file": (filename, data, "text/markdown")}


def _user_app(user, *, bundle_flag: str = "", monkeypatch):
    """The user surface as ``main.py`` mounts it: bundle import first, then My Skills."""
    monkeypatch.setenv("SKILL_BUNDLE_IMPORT_ENABLED", bundle_flag)
    app = FastAPI()
    app.include_router(bundle_import_routes.router)
    app.include_router(my_skill_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


def _admin_app(user=None, *, authorize: bool = True):
    app = FastAPI()
    app.include_router(admin_skill_routes.router, prefix="/admin")
    if authorize:
        override_admin_auth(app, lambda: user)
    elif user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


@pytest.fixture()
def user_client(author_user, monkeypatch):
    with _user_app(author_user, monkeypatch=monkeypatch) as client:
        yield client


@pytest.fixture()
def admin_client(admin_user):
    with _admin_app(admin_user) as client:
        yield client


@pytest.fixture(params=["user", "admin"])
def either(request, author_user, admin_user, monkeypatch):
    """Each parse route, so a contract is pinned on both at once."""
    if request.param == "user":
        with _user_app(author_user, monkeypatch=monkeypatch) as client:
            yield client, USER_PATH
    else:
        with _admin_app(admin_user) as client:
            yield client, ADMIN_PATH


def _codes(body, level=None):
    return [d["code"] for d in body["diagnostics"] if level is None or d["level"] == level]


# =============================================================================
# The parser's colon-quoting retry
# =============================================================================


class TestColonRetry:
    def test_an_unquoted_colon_parses_on_retry_with_a_warning(self):
        parsed = parse_skill_md(
            "---\nname: x\ndescription: Use when: the user asks for X\n---\nBody\n"
        )
        assert parsed.frontmatter["description"] == "Use when: the user asks for X"
        (note,) = parsed.diagnostics
        assert note.code == "frontmatter_repaired"
        assert note.level == "warning"
        assert note.field == "description"
        assert "double quotes" in note.message

    def test_quotes_and_backslashes_survive_the_repair(self):
        parsed = parse_skill_md('---\ndescription: Say: "hi" \\ bye\n---\n')
        assert parsed.frontmatter["description"] == 'Say: "hi" \\ bye'

    def test_only_plain_scalars_are_rewritten(self):
        block = 'a: "q: x"\nb: [c: d]\nc: |\n  e: f\nd: plain: yes\n'
        fixed, keys = quote_colon_values(block)
        assert keys == ["d"]
        assert 'a: "q: x"' in fixed and "b: [c: d]" in fixed and "c: |" in fixed

    def test_a_valid_document_is_parsed_once(self, monkeypatch):
        calls = []
        real = skill_md._load_frontmatter
        monkeypatch.setattr(skill_md, "_load_frontmatter", lambda b: calls.append(b) or real(b))
        parse_skill_md("---\nname: x\ndescription: fine\n---\n")
        assert len(calls) == 1

    def test_an_alias_is_refused_and_not_retried(self, monkeypatch):
        calls = []
        real = skill_md._load_frontmatter
        monkeypatch.setattr(skill_md, "_load_frontmatter", lambda b: calls.append(b) or real(b))
        with pytest.raises(SkillMdError, match="anchor"):
            parse_skill_md("---\na: &x one\nb: *x\ndescription: Use when: x\n---\n")
        assert len(calls) == 1

    def test_an_alias_reached_only_after_the_repair_is_still_refused(self):
        with pytest.raises(SkillMdError, match="alias|anchor"):
            parse_skill_md("---\ndescription: Use when: x\na: &x one\nb: *x\n---\n")

    def test_a_document_the_repair_cannot_fix_is_still_invalid_yaml(self):
        with pytest.raises(SkillMdError, match="not valid YAML"):
            parse_skill_md("---\nname: [unclosed: x\n---\n")

    def test_the_retry_is_bounded_like_the_first_load(self, monkeypatch):
        monkeypatch.setattr(skill_md, "MAX_FRONTMATTER_NODES", 5)
        many = "\n".join(f"k{i}: v" for i in range(6))
        with pytest.raises(SkillMdError, match="more than 5"):
            parse_skill_md(f"---\ndescription: Use when: x\n{many}\n---\n")


# =============================================================================
# The routes
# =============================================================================


class TestParse:
    def test_nested_frontmatter_comes_back_intact(self, either):
        client, path = either
        resp = client.post(path, files=_file(NESTED))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "pdf-tools"
        assert body["displayName"] == "Pdf tools"
        assert body["description"] == "Fill and merge PDFs."
        assert body["instructions"] == "# PDF tools\n\nUse pypdf."
        assert body["allowedTools"] == ["Read", "Bash(python:*)"]
        assert body["skillMetadata"] == EXPECTED_METADATA
        # Only the non-standard key is worth a note.
        assert body["diagnostics"] == [
            {
                "level": "warning",
                "field": "x-custom",
                "code": "unknown_field",
                "message": "x-custom is not an agentskills.io frontmatter field. "
                "It is kept and written under metadata.",
            }
        ]

    def test_the_mapping_is_bundle_imports(self, either):
        """One mapping: the parse route and a bundle fill the same fields."""
        from apis.shared.skills.bundle_import import parse_skill_md as bundle_parse
        from apis.shared.skills.bundle_import import skill_fields_from_frontmatter

        client, path = either
        frontmatter, body = bundle_parse(NESTED)
        fields = skill_fields_from_frontmatter(frontmatter, body)
        resp = client.post(path, files=_file(NESTED)).json()
        assert resp["allowedTools"] == fields.allowed_tools
        assert resp["skillMetadata"] == fields.skill_metadata

    def test_the_colon_slip_is_a_warning_not_a_refusal(self, either):
        client, path = either
        resp = client.post(
            path, files=_file("---\nname: demo-skill\ndescription: Use when: the user asks for X\n---\nB")
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Use when: the user asks for X"
        assert "frontmatter_repaired" in _codes(resp.json(), "warning")

    def test_a_bidi_override_in_the_description_is_an_error_diagnostic(self, either):
        client, path = either
        resp = client.post(path, files=_file("---\nname: x-skill\ndescription: Safe ‮edoc\n---\nB"))
        assert resp.status_code == 200, resp.text
        errors = [d for d in resp.json()["diagnostics"] if d["level"] == "error"]
        assert [(d["field"], d["code"]) for d in errors] == [("description", "unsafe_character")]
        assert "U+202E" in errors[0]["message"]

    def test_an_unsafe_character_in_the_body_is_a_warning_with_its_position(self, either):
        client, path = either
        resp = client.post(path, files=_file("---\nname: x-skill\ndescription: d\n---\nline one\nab​c"))
        (warning,) = [d for d in resp.json()["diagnostics"] if d["code"] == "unsafe_character"]
        assert warning["level"] == "warning"
        assert "U+200B" in warning["message"] and "line 2, column 3" in warning["message"]

    def test_validation_problems_are_diagnostics(self, either):
        client, path = either
        resp = client.post(path, files=_file("---\nname: Bad_Name\n---\nBody only"))
        assert resp.status_code == 200
        assert set(_codes(resp.json(), "error")) == {"name_invalid", "description_missing"}

    def test_no_frontmatter_is_all_body(self, either):
        client, path = either
        body = client.post(path, files=_file("Just a body.")).json()
        assert body["instructions"] == "Just a body."
        assert body["name"] == "" and body["displayName"] == ""

    def test_one_byte_over_200_kib_is_refused(self, either):
        client, path = either
        assert MAX_SKILL_MD_UPLOAD_BYTES == 200 * 1024
        at_cap = "---\nname: x-skill\ndescription: d\n---\n"
        at_cap += "a" * (MAX_SKILL_MD_UPLOAD_BYTES - len(at_cap))
        assert client.post(path, files=_file(at_cap)).status_code == 200
        resp = client.post(path, files=_file(at_cap + "a"))
        assert resp.status_code == 400
        assert "200 KiB" in resp.json()["detail"]

    def test_the_read_is_bounded(self, either, monkeypatch):
        """At most one byte past the cap is read, however large the upload."""
        client, path = either
        sizes = []
        real = skill_md_parse.read_skill_md_upload
        monkeypatch.setattr(
            skill_md_parse, "read_skill_md_upload", lambda raw: sizes.append(len(raw)) or real(raw)
        )
        client.post(path, files=_file(b"a" * (MAX_SKILL_MD_UPLOAD_BYTES * 3)))
        assert sizes == [MAX_SKILL_MD_UPLOAD_BYTES + 1]

    def test_non_utf8_is_refused(self, either):
        client, path = either
        resp = client.post(path, files=_file(b"---\nname: caf\xe9\n---\n"))
        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "frontmatter, message",
        [
            ("a: &x 1\nb: *x", "anchor"),
            ("- just\n- a list", "key: value"),
            ("name: [unclosed", "not valid YAML"),
        ],
    )
    def test_an_unreadable_frontmatter_is_refused(self, either, frontmatter, message):
        client, path = either
        resp = client.post(path, files=_file(f"---\n{frontmatter}\n---\nB"))
        assert resp.status_code == 400
        assert message in resp.json()["detail"]

    def test_the_parse_runs_in_a_worker_thread(self, either, monkeypatch):
        client, path = either
        ran = []
        real = skill_md_parse.run_blocking

        async def spy(fn, *args, **kw):
            ran.append(fn.__name__)
            return await real(fn, *args, **kw)

        monkeypatch.setattr(skill_md_parse, "run_blocking", spy)
        assert client.post(path, files=_file(NESTED)).status_code == 200
        assert ran == ["read_skill_md_upload"]


class TestAccess:
    def test_unauthenticated_is_refused_before_the_body_is_read(self, monkeypatch):
        read = []
        monkeypatch.setattr(skill_md_parse, "read_skill_md_upload", lambda raw: read.append(raw))
        with _user_app(None, monkeypatch=monkeypatch) as anonymous:
            assert anonymous.post(USER_PATH, files=_file(NESTED)).status_code == 401
        with _admin_app(authorize=False) as anonymous:
            assert anonymous.post(ADMIN_PATH, files=_file(NESTED)).status_code == 401
        assert read == []

    def test_the_admin_route_refuses_a_non_admin(self, author_user, monkeypatch):
        async def no_scope(user, scope):
            assert scope == "admin.skills"
            return False

        monkeypatch.setattr(rbac, "has_admin_scope", no_scope)
        with _admin_app(author_user, authorize=False) as client:
            assert client.post(ADMIN_PATH, files=_file(NESTED)).status_code == 403

    def test_the_user_route_is_not_behind_the_bundle_import_flag(self, author_user, monkeypatch):
        with _user_app(author_user, bundle_flag="", monkeypatch=monkeypatch) as client:
            assert client.post(USER_PATH, files=_file(NESTED)).status_code == 200


class TestRouting:
    """Neither path is read as ``/{skill_id}/...`` by a parameterised route."""

    def test_the_user_route_wins_over_every_mine_route(self, user_client):
        resp = user_client.post(USER_PATH, files=_file(NESTED))
        assert resp.status_code == 200
        assert resp.json()["name"] == "pdf-tools"
        assert _first_match(user_client.app, "POST", USER_PATH) == "parse_my_skill_md"

    def test_the_admin_route_wins_over_every_skill_id_route(self, admin_client):
        resp = admin_client.post(ADMIN_PATH, files=_file(NESTED))
        assert resp.status_code == 200
        assert resp.json()["name"] == "pdf-tools"
        assert _first_match(admin_client.app, "POST", ADMIN_PATH) == "admin_parse_skill_md"


def _first_match(app, method: str, path: str) -> str:
    from starlette.routing import Match

    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint.__name__
    raise AssertionError(f"nothing matches {method} {path}")
