"""Reading a project's bound skills (``GET /projects/{id}/skills/{skillId}`` and its files).

A member reads exactly what their own turns in the project would load: a bound skill
that resolves for them through the harness (``resolve_invocable_skill_ids`` with the
project's adders), at the version the binding pins. The case the route exists for is a
private skill the editor who added it wrote, which is in no other member's own list.
"""

from __future__ import annotations

import asyncio
from typing import Dict

import pytest

import apis.app_api.projects.skill_content as skill_content
import apis.shared.skills.pinning as pinning
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus

from tests.shared.test_project_harness_rules import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    project,
    projects_table,
)
from tests.shared.test_project_settings import client, pid, skills, validation  # noqa: F401 (fixtures)

AUTHORED = "eds_redline"


class FakeStore:
    """The skill-resources bucket: live bundle keys, and the frozen copies a pin makes."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def freeze(self, *, skill_id: str, s3_key: str, content_hash: str, content_type: str) -> str:
        frozen = f"skills/{skill_id}/_versions/{content_hash}"
        self.objects[frozen] = self.objects[s3_key]
        return frozen

    def get(self, s3_key: str) -> bytes:
        return self.objects[s3_key]


def _file(filename: str, kind: str, content_hash: str) -> SkillResourceRef:
    directory = {"reference": "references", "script": "scripts"}[kind]
    return SkillResourceRef(
        filename=filename,
        content_hash=content_hash,
        size=12,
        content_type="text/markdown" if filename.endswith(".md") else "text/x-python",
        s3_key=f"skills/{AUTHORED}/{directory}/{filename}",
        kind=kind,
    )


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(pinning, "get_skill_resource_store", lambda: fake)
    monkeypatch.setattr(skill_content, "get_skill_resource_store", lambda: fake)
    return fake


@pytest.fixture()
def authored(skills, store):
    """A private skill the editor wrote, with a reference file and a script."""
    store.objects[f"skills/{AUTHORED}/references/playbook.md"] = b"# Playbook v1"
    store.objects[f"skills/{AUTHORED}/scripts/redline.py"] = b"print('redline')"
    asyncio.run(skills.create_skill(SkillDefinition(
        skill_id=AUTHORED,
        display_name="Ed's redline",
        description="Redlines a contract.",
        instructions="Follow the playbook.",
        owner_id=EDITOR.user_id,
        resources=[_file("playbook.md", "reference", "h1"), _file("redline.py", "script", "h2")],
    )))
    return skills


def _bind(pid: str, *refs: str, user=EDITOR) -> dict:
    response = client(user).put(f"/projects/{pid}/skills", json={"bindings": [{"ref": r} for r in refs]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "principal,expected", [(OWNER, 200), (EDITOR, 200), (VIEWER, 200), (STRANGER, 404)], ids=lambda p: getattr(p, "user_id", p)
)
def test_every_member_reads_an_editors_private_skill_and_no_one_else_does(pid, authored, principal, expected):
    _bind(pid, AUTHORED)
    skill = client(principal).get(f"/projects/{pid}/skills/{AUTHORED}")
    file = client(principal).get(f"/projects/{pid}/skills/{AUTHORED}/files/playbook.md")
    assert (skill.status_code, file.status_code) == (expected, expected), skill.text


def test_the_read_is_the_pinned_content_without_owner_or_storage_details(pid, authored):
    _bind(pid, AUTHORED)
    body = client(VIEWER).get(f"/projects/{pid}/skills/{AUTHORED}").json()
    assert body == {
        "skillId": AUTHORED,
        "displayName": "Ed's redline",
        "description": "Redlines a contract.",
        "instructions": "Follow the playbook.",
        "version": 1,
        "compose": [],
        "allowedTools": [],
        "files": [
            {"filename": "playbook.md", "kind": "reference", "size": 12, "contentType": "text/markdown"},
            {"filename": "redline.py", "kind": "script", "size": 12, "contentType": "text/x-python"},
        ],
        "isOwned": False,
    }
    # Only the author is told they can edit it.
    assert client(EDITOR).get(f"/projects/{pid}/skills/{AUTHORED}").json()["isOwned"] is True
    assert client(OWNER).get(f"/projects/{pid}/skills/{AUTHORED}").json()["isOwned"] is False


def test_a_changed_skill_still_reads_as_pinned_until_someone_updates_it(pid, authored, store):
    _bind(pid, AUTHORED)
    store.objects[f"skills/{AUTHORED}/references/playbook.md"] = b"# Playbook v2"
    asyncio.run(authored.update_skill(AUTHORED, {"instructions": "Follow the new playbook."}))
    # The live row now points at new bytes under the same key; a re-upload changes the hash.
    live = asyncio.run(authored.get_skill(AUTHORED))
    asyncio.run(authored.update_skill(AUTHORED, {
        "resources": [r.model_copy(update={"content_hash": "h3"}) if r.filename == "playbook.md" else r for r in live.resources]
    }))

    viewer = client(VIEWER)
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}").json()["instructions"] == "Follow the playbook."
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}/files/playbook.md").content == b"# Playbook v1"

    client(EDITOR).post(f"/projects/{pid}/skills/{AUTHORED}/pin")
    updated = viewer.get(f"/projects/{pid}/skills/{AUTHORED}").json()
    assert (updated["version"], updated["instructions"]) == (2, "Follow the new playbook.")
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}/files/playbook.md").content == b"# Playbook v2"


def test_a_file_is_served_as_an_inert_attachment(pid, authored):
    _bind(pid, AUTHORED)
    response = client(VIEWER).get(f"/projects/{pid}/skills/{AUTHORED}/files/redline.py")
    assert response.content == b"print('redline')"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


def test_only_bound_skills_and_their_own_files_are_served(pid, authored):
    viewer = client(VIEWER)
    # Not bound yet: the project can't be used to read a skill it doesn't run.
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}").status_code == 404
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}/files/playbook.md").status_code == 404
    _bind(pid, AUTHORED)
    assert viewer.get(f"/projects/{pid}/skills/{AUTHORED}/files/other.md").status_code == 404
    assert viewer.get(f"/projects/{pid}/skills/no_such_skill").status_code == 404


def test_a_catalog_skill_the_members_roles_dont_grant_is_refused(pid, authored):
    # The editor's save is validated elsewhere (faked here); a catalog skill is never
    # widened by who added it, so a member without the role gets it dropped from
    # their turns, and can't read it through the project either.
    _bind(pid, "skill_one", AUTHORED)
    assert client(VIEWER).get(f"/projects/{pid}/skills/skill_one").status_code == 403


def test_a_disabled_skill_is_gone(pid, authored):
    _bind(pid, AUTHORED)
    asyncio.run(authored.update_skill(AUTHORED, {"status": SkillStatus.DISABLED.value}))
    assert client(VIEWER).get(f"/projects/{pid}/skills/{AUTHORED}").status_code == 404


def test_the_list_names_the_skills_the_caller_can_read(pid, authored):
    _bind(pid, "skill_one", AUTHORED)
    bindings = {b["ref"]: b for b in client(VIEWER).get(f"/projects/{pid}/skills").json()["bindings"]}
    assert (bindings[AUTHORED]["available"], bindings[AUTHORED]["displayName"]) == (True, "Ed's redline")
    assert bindings[AUTHORED]["description"] == "Redlines a contract."
    assert "addedBy" not in bindings[AUTHORED]["config"]
    assert (bindings["skill_one"]["available"], bindings["skill_one"]["displayName"]) == (False, None)


def test_skills_off_reads_nothing(pid, authored, monkeypatch):
    _bind(pid, AUTHORED)
    monkeypatch.setenv("SKILLS_ENABLED", "false")
    assert client(VIEWER).get(f"/projects/{pid}/skills/{AUTHORED}").status_code == 403


def test_projects_off_is_a_404(pid, authored, monkeypatch):
    _bind(pid, AUTHORED)
    monkeypatch.setenv("PROJECTS_ENABLED", "false")
    assert client(VIEWER).get(f"/projects/{pid}/skills/{AUTHORED}").status_code == 404
