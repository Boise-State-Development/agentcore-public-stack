"""The field-set authoring routes, ``/skills/mine/{skill_id}/field-sets``.

``docs/specs/field-set-authoring.md`` §4.1. The routes trade JSON; the backend
reads and writes the file (D2). What these pin, in order of what would hurt:

* a structured save never deletes what the form does not show (a note column,
  prose, frontmatter) — §5.4;
* a no-op save writes nothing, so it cannot move the version or the grammar — §6;
* a stored file that no longer parses opens as 400 + its markdown, never a 500;
* ownership is ``UserSkillService``'s, and the flag 404s only after auth.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session

NOTED = """---
title: Grant Intake
owner: Research Office
version: 2
---

Read by the grants office.

| name | type | find | note |
|---|---|---|---|
| deadline | date | The full proposal deadline | ask Dana |
| amount | currency | The maximum award | |

Questions go to the office.
"""

STRUCTURED = {
    "title": "Budget",
    "version": "1",
    "fields": [
        {"name": "total", "type": "currency", "find": "Total requested budget"},
        {
            "name": "cost_share",
            "title": "Cost sharing",
            "type": "enum",
            "find": "  Whether   cost sharing is required  ",
            "enum": ["required", "not required"],
            "optional": True,
        },
    ],
}


def _client(app_user, service, monkeypatch, *, enabled=True):
    monkeypatch.setenv("FIELD_SET_AUTHORING_ENABLED", "true" if enabled else "")
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: service)
    app = FastAPI()
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: app_user
    return TestClient(app)


@pytest.fixture()
def client(user_skill_service, author_user, monkeypatch):
    with _client(author_user, user_skill_service, monkeypatch) as c:
        yield c


@pytest.fixture()
def skill_id(client) -> str:
    resp = client.post("/skills/mine", json={"displayName": "Grant Intake", "description": "d"})
    assert resp.status_code == 200, resp.text
    return resp.json()["skillId"]


def _upload(client, skill_id, filename, body: str, kind="reference"):
    return client.post(
        f"/skills/mine/{skill_id}/resources",
        files={"file": (filename, body.encode(), "text/markdown")},
        data={"kind": kind},
    )


def _put(client, skill_id, name, body, **params):
    return client.put(f"/skills/mine/{skill_id}/field-sets/{name}", json=body, params=params)


def _resources(client, skill_id):
    return client.get(f"/skills/mine/{skill_id}/resources").json()["resources"]


class TestFlag:
    def test_off_is_404_for_every_route(self, user_skill_service, author_user, monkeypatch, skill_id):
        with _client(author_user, user_skill_service, monkeypatch, enabled=False) as off:
            assert off.get(f"/skills/mine/{skill_id}/field-sets").status_code == 404
            assert off.get(f"/skills/mine/{skill_id}/field-sets/budget").status_code == 404
            assert _put(off, skill_id, "budget", STRUCTURED).status_code == 404

    def test_off_still_refuses_a_broken_upload(self, user_skill_service, author_user, monkeypatch, skill_id):
        """The flag takes away the editor, never the check (§4.2)."""
        with _client(author_user, user_skill_service, monkeypatch, enabled=False) as off:
            resp = _upload(off, skill_id, "field-set.budget.md", "| name |\n|---|\n| x |\n")

        assert resp.status_code == 400

    def test_auth_runs_before_the_flag(self, user_skill_service, monkeypatch):
        """Signed out is 401 whether or not the feature exists."""
        monkeypatch.setenv("FIELD_SET_AUTHORING_ENABLED", "")
        monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
        app = FastAPI()
        app.include_router(my_skill_routes.router)
        with TestClient(app) as anonymous:
            assert anonymous.get("/skills/mine/x/field-sets").status_code == 401


class TestList:
    def test_lists_only_reference_field_sets(self, client, skill_id):
        assert _upload(client, skill_id, "field-set.intake.md", NOTED).status_code == 200
        assert _upload(client, skill_id, "forms.md", "notes").status_code == 200
        assert _upload(client, skill_id, "field-set.asset.md", "anything", kind="asset").status_code == 200

        body = client.get(f"/skills/mine/{skill_id}/field-sets").json()

        assert [s["name"] for s in body["fieldSets"]] == ["intake"]
        assert body["fieldSets"][0]["filename"] == "field-set.intake.md"
        assert body["maxFields"] == 14

    def test_another_users_skill_is_not_found(
        self, user_skill_service, other_user, monkeypatch, skill_id
    ):
        with _client(other_user, user_skill_service, monkeypatch) as other:
            assert other.get(f"/skills/mine/{skill_id}/field-sets").status_code == 404
            assert _put(other, skill_id, "budget", STRUCTURED).status_code == 404


class TestRead:
    def test_reports_what_the_form_cannot_show(self, client, skill_id):
        _upload(client, skill_id, "field-set.intake.md", NOTED)

        body = client.get(f"/skills/mine/{skill_id}/field-sets/intake").json()

        assert (body["title"], body["version"]) == ("Grant Intake", "2")
        assert [f["name"] for f in body["fields"]] == ["deadline", "amount"]
        assert body["unmanagedColumns"] == ["note"]
        assert [f["unmanagedValues"] for f in body["fields"]] == [["ask Dana"], [""]]
        assert body["preamble"] == "Read by the grants office."
        assert body["postscript"] == "Questions go to the office."
        assert body["frontmatter"] == ["owner: Research Office"]
        assert body["markdown"] == NOTED

    def test_a_missing_set_is_404(self, client, skill_id):
        assert client.get(f"/skills/mine/{skill_id}/field-sets/nope").status_code == 404

    def test_a_stored_broken_file_opens_as_400_with_its_markdown(
        self, client, skill_id, monkeypatch
    ):
        """Only a file written before every path checked can be broken, and it
        is exactly the one its author needs to open."""
        import apis.shared.skills.bundle_files as bundle_files

        broken = "| name | type |\n|---|---|\n| total | currency |\n"
        with monkeypatch.context() as m:
            # Store it the way a pre-check deployment did.
            m.setattr(bundle_files, "validate_field_set_upload", lambda *_: None)
            assert _upload(client, skill_id, "field-set.legacy.md", broken).status_code == 200

        resp = client.get(f"/skills/mine/{skill_id}/field-sets/legacy")

        assert resp.status_code == 400
        assert "'find' is required" in resp.json()["detail"]
        assert resp.json()["markdown"] == broken


class TestStructuredWrite:
    def test_writes_the_canonical_file_and_reads_back(self, client, skill_id):
        resp = _put(client, skill_id, "budget", STRUCTURED)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fields"][0]["title"] == "Total"  # the parser's default
        # Ends trimmed, inside untouched (§6).
        assert body["fields"][1]["find"] == "Whether   cost sharing is required"
        stored = client.get(f"/skills/mine/{skill_id}/resources/field-set.budget.md").text
        assert stored == body["markdown"]
        assert stored.startswith("---\ntitle: Budget\nversion: 1\n---\n")

    def test_validate_writes_nothing(self, client, skill_id):
        resp = _put(client, skill_id, "budget", STRUCTURED, validate="true")

        assert resp.status_code == 200
        assert "| total | Total | currency |" in resp.json()["markdown"]
        assert _resources(client, skill_id) == []

    def test_an_invalid_set_is_refused_with_the_parsers_message(self, client, skill_id):
        body = {"fields": [{"name": "total", "type": "currency", "find": ""}]}

        resp = _put(client, skill_id, "budget", body)

        assert resp.status_code == 400
        assert "'find' is required" in resp.json()["detail"]
        assert _resources(client, skill_id) == []

    def test_fifteen_fields_get_the_ceiling_message(self, client, skill_id):
        fields = [{"name": f"f{i}", "find": f"field {i}"} for i in range(15)]

        resp = _put(client, skill_id, "wide", {"fields": fields})

        assert resp.status_code == 400
        assert "exceeds the limit of 14" in resp.json()["detail"]

    def test_a_bad_set_name_is_refused(self, client, skill_id):
        resp = _put(client, skill_id, "Budget_Two", STRUCTURED)

        assert resp.status_code == 400
        assert "lowercase words joined by hyphens" in resp.json()["detail"]

    def test_a_save_from_the_form_keeps_what_the_form_does_not_show(self, client, skill_id):
        """Open, edit one phrase, save: the note column, the prose and the
        frontmatter line all survive (§5.4)."""
        _upload(client, skill_id, "field-set.intake.md", NOTED)
        opened = client.get(f"/skills/mine/{skill_id}/field-sets/intake").json()
        opened["fields"][1]["find"] = "The maximum total award"
        body = {k: opened[k] for k in (
            "title", "version", "fields", "rules", "preamble", "postscript",
            "frontmatter", "unmanagedColumns",
        )}

        saved = _put(client, skill_id, "intake", body)

        assert saved.status_code == 200, saved.text
        reopened = client.get(f"/skills/mine/{skill_id}/field-sets/intake").json()
        assert reopened["fields"][1]["find"] == "The maximum total award"
        assert reopened["unmanagedColumns"] == ["note"]
        assert reopened["fields"][0]["unmanagedValues"] == ["ask Dana"]
        assert reopened["preamble"] == "Read by the grants office."
        assert reopened["postscript"] == "Questions go to the office."
        assert reopened["frontmatter"] == ["owner: Research Office"]
        assert reopened["version"] == "2"  # never bumped by a save (§6)

    def test_extra_values_with_no_extra_columns_are_refused(self, client, skill_id):
        body = {"fields": [{"name": "total", "find": "x", "unmanagedValues": ["a"]}]}

        resp = _put(client, skill_id, "budget", body)

        assert resp.status_code == 400
        assert "no extra columns" in resp.json()["detail"]


class TestNoOpSave:
    def test_an_unchanged_save_writes_nothing(self, client, skill_id, user_skill_service, monkeypatch):
        first = _put(client, skill_id, "budget", STRUCTURED)
        assert first.status_code == 200

        writes = []
        original = user_skill_service.add_resource

        async def counting(*args, **kwargs):
            writes.append(kwargs.get("filename"))
            return await original(*args, **kwargs)

        monkeypatch.setattr(user_skill_service, "add_resource", counting)
        again = _put(client, skill_id, "budget", STRUCTURED)

        assert again.status_code == 200
        assert writes == []

    def test_a_changed_save_writes(self, client, skill_id):
        _put(client, skill_id, "budget", STRUCTURED)
        before = _resources(client, skill_id)[0]["contentHash"]

        changed = {**STRUCTURED, "version": "2"}
        assert _put(client, skill_id, "budget", changed).status_code == 200

        assert _resources(client, skill_id)[0]["contentHash"] != before


class TestMarkdownWrite:
    def test_stores_the_bytes_as_sent(self, client, skill_id):
        resp = _put(client, skill_id, "intake", {"markdown": NOTED})

        assert resp.status_code == 200, resp.text
        stored = client.get(f"/skills/mine/{skill_id}/resources/field-set.intake.md").text
        assert stored == NOTED

    def test_broken_markdown_is_refused(self, client, skill_id):
        resp = _put(client, skill_id, "intake", {"markdown": "no table here"})

        assert resp.status_code == 400
        assert "no field table found" in resp.json()["detail"]
        assert _resources(client, skill_id) == []

    def test_markdown_and_fields_together_are_refused(self, client, skill_id):
        resp = _put(client, skill_id, "intake", {"markdown": NOTED, "title": "x"})

        assert resp.status_code == 422

    def test_neither_is_refused(self, client, skill_id):
        assert _put(client, skill_id, "intake", {"title": "x"}).status_code == 422


class TestNameCollision:
    def test_an_asset_of_the_same_name_is_not_overwritten(self, client, skill_id):
        assert _upload(client, skill_id, "field-set.budget.md", "x", kind="asset").status_code == 200

        resp = _put(client, skill_id, "budget", STRUCTURED)

        assert resp.status_code == 409
        assert [r["kind"] for r in _resources(client, skill_id)] == ["asset"]
