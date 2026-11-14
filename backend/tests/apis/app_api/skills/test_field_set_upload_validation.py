"""A field set is checked on every path that writes a skill file.

``docs/specs/field-set-authoring.md`` §4.2 (D6). ``SkillBundleFiles.add_resource``
is the one function the user upload, the admin upload and the agent's
``my_skill_write_file`` all go through, and it parses any reference file named
``field-set.<name>.md`` before storing it — whether or not any flag is on. A
field set stored invalid fails later, mid-turn, in front of someone who did not
write it; refused here, it reaches its author with the parser's own message.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_skill_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.extraction.field_sets import FieldSetError
from tests.conftest import override_admin_auth

from .conftest import AWS_REGION, SKILL_RESOURCES_BUCKET

VALID = b"""---
title: Budget
version: 1
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| total | Total | currency | Total requested budget | | no |
"""

# A field with no `find`: the commonest authoring mistake, and one the parser
# names exactly.
MISSING_FIND = b"| name | type |\n|---|---|\n| total | currency |\n"


@pytest.fixture()
def author_client(user_skill_service, author_user, monkeypatch):
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
    app = FastAPI()
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(skill_service, admin_user, monkeypatch):
    monkeypatch.setattr(admin_skill_routes, "get_skill_catalog_service", lambda: skill_service)
    app = FastAPI()
    app.include_router(admin_skill_routes.router)
    override_admin_auth(app, lambda: admin_user)
    return TestClient(app)


def _create_my_skill(client) -> str:
    resp = client.post("/skills/mine", json={"displayName": "Budget Intake", "description": "d"})
    assert resp.status_code == 200, resp.text
    return resp.json()["skillId"]


def _upload(client, path, filename, body, kind="reference"):
    return client.post(
        path,
        files={"file": (filename, body, "text/markdown")},
        data={"kind": kind},
    )


def _stored_keys():
    s3 = boto3.client("s3", region_name=AWS_REGION)
    return [o["Key"] for o in s3.list_objects_v2(Bucket=SKILL_RESOURCES_BUCKET).get("Contents", [])]


class TestUserUpload:
    def test_a_valid_field_set_is_stored(self, author_client):
        skill_id = _create_my_skill(author_client)

        resp = _upload(author_client, f"/skills/mine/{skill_id}/resources", "field-set.budget.md", VALID)

        assert resp.status_code == 200, resp.text
        assert [r["filename"] for r in resp.json()["resources"]] == ["field-set.budget.md"]

    def test_a_broken_field_set_is_refused_and_nothing_is_stored(self, author_client):
        skill_id = _create_my_skill(author_client)

        resp = _upload(
            author_client, f"/skills/mine/{skill_id}/resources", "field-set.budget.md", MISSING_FIND
        )

        assert resp.status_code == 400, resp.text
        assert "'find' is required" in resp.json()["detail"]
        manifest = author_client.get(f"/skills/mine/{skill_id}/resources").json()
        assert manifest["resources"] == []
        assert not any(k.endswith("field-set.budget.md") for k in _stored_keys())

    def test_a_set_name_the_resolver_would_skip_is_refused(self, author_client):
        skill_id = _create_my_skill(author_client)

        resp = _upload(author_client, f"/skills/mine/{skill_id}/resources", "field-set.Budget.md", VALID)

        assert resp.status_code == 400, resp.text
        assert "lowercase words joined by hyphens" in resp.json()["detail"]

    def test_only_references_are_field_sets(self, author_client):
        """The resolver reads field sets from `references/` alone, so an asset
        that happens to share the name is not one and is stored as it is."""
        skill_id = _create_my_skill(author_client)

        resp = _upload(
            author_client,
            f"/skills/mine/{skill_id}/resources",
            "field-set.budget.md",
            MISSING_FIND,
            kind="asset",
        )

        assert resp.status_code == 200, resp.text

    def test_other_markdown_is_not_parsed(self, author_client):
        skill_id = _create_my_skill(author_client)

        resp = _upload(author_client, f"/skills/mine/{skill_id}/resources", "forms.md", MISSING_FIND)

        assert resp.status_code == 200, resp.text


class TestAdminUpload:
    def test_a_broken_field_set_is_refused(self, admin_client):
        created = admin_client.post(
            "/skills/",
            json={
                "skillId": "budget_intake",
                "displayName": "Budget Intake",
                "description": "d",
                "instructions": "# Budget",
            },
        )
        assert created.status_code in (200, 201), created.text

        resp = _upload(admin_client, "/skills/budget_intake/resources", "field-set.budget.md", MISSING_FIND)

        assert resp.status_code == 400, resp.text
        assert "'find' is required" in resp.json()["detail"]


@pytest.mark.asyncio
class TestAgentWrite:
    """``my_skill_write_file`` writes through ``write_text_file``; its tool turns
    a ``ValueError`` into a result the model can act on, so the parser's
    message is what the model reads and corrects."""

    async def test_a_broken_field_set_raises_the_parsers_error(self, user_skill_service, author_user):
        skill = await user_skill_service.create_my_skill(
            author_user, display_name="Budget Intake", description="d"
        )

        with pytest.raises(FieldSetError, match="'find' is required"):
            await user_skill_service.write_text_file(
                skill.skill_id, "reference", "field-set.budget.md", MISSING_FIND.decode(), author_user
            )

    async def test_a_valid_field_set_is_written(self, user_skill_service, author_user):
        skill = await user_skill_service.create_my_skill(
            author_user, display_name="Budget Intake", description="d"
        )

        refs = await user_skill_service.write_text_file(
            skill.skill_id, "reference", "field-set.budget.md", VALID.decode(), author_user
        )

        assert [r.filename for r in refs] == ["field-set.budget.md"]
