"""Route tests for the Memory Spaces user surface (`/memory/spaces/*`, A2).

Pins the flag gate (404 while off), CRUD happy paths, and identity-based
access (403 for a non-member, 404 for a missing space). Backed by a REAL
`MemorySpaceService` on moto (DynamoDB + S3), so the routes are exercised
end-to-end through the shared service.
"""

from __future__ import annotations

import io
import json
import zipfile

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.memory.repository import MemorySpaceRepository
from apis.shared.memory.service import MemorySpaceService
from apis.shared.memory.store import MemorySpaceStore

from apis.app_api.memory_spaces import routes as mem_routes

REGION = "us-east-1"
BUCKET = "test-memory-spaces"
TABLE = "test-memory-spaces"

OWNER = User(user_id="user-owner", email="owner@example.edu", name="O", roles=["default"])
STRANGER = User(
    user_id="user-stranger", email="stranger@example.edu", name="S", roles=["default"]
)


def _make_table():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "MemberIndex",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    return MemorySpaceRepository(table_name=TABLE)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("MEMORY_SPACES_ENABLED", "true")
    with mock_aws():
        yield


@pytest.fixture()
def service(env):
    repo = _make_table()
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    return MemorySpaceService(
        repository=repo, store=MemorySpaceStore(bucket_name=BUCKET, s3_client=s3)
    )


def _client(service, monkeypatch, user: User = OWNER, authed: bool = True):
    monkeypatch.setattr(mem_routes, "_service", service)
    app = FastAPI()
    app.include_router(mem_routes.router)
    if authed:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------


class TestFlagGate:
    def test_404_when_flag_off(self, service, monkeypatch):
        monkeypatch.setenv("MEMORY_SPACES_ENABLED", "false")
        client = _client(service, monkeypatch)
        assert client.get("/memory/spaces").status_code == 404


class TestSpaceCrud:
    def test_create_list_get(self, service, monkeypatch):
        client = _client(service, monkeypatch)

        resp = client.post(
            "/memory/spaces", json={"name": "My Brain", "template": "chief-of-staff"}
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "My Brain"
        assert body["role"] == "owner"
        space_id = body["spaceId"]

        listed = client.get("/memory/spaces").json()
        assert {s["spaceId"] for s in listed["spaces"]} == {space_id}
        assert any(t["templateId"] == "chief-of-staff" for t in listed["templates"])

        detail = client.get(f"/memory/spaces/{space_id}").json()
        assert detail["role"] == "owner"
        assert "Strategic priorities" in detail["index"]
        assert detail["entries"] == []

    def test_create_rejects_unknown_template(self, service, monkeypatch):
        client = _client(service, monkeypatch)
        resp = client.post("/memory/spaces", json={"name": "X", "template": "nope"})
        assert resp.status_code == 400

    def test_create_rejects_blank_name(self, service, monkeypatch):
        client = _client(service, monkeypatch)
        resp = client.post("/memory/spaces", json={"name": "   "})
        assert resp.status_code in (400, 422)

    def test_get_missing_space_404(self, service, monkeypatch):
        client = _client(service, monkeypatch)
        assert client.get("/memory/spaces/spc_missing").status_code == 404

    def test_owner_deletes(self, service, monkeypatch):
        client = _client(service, monkeypatch)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        assert client.delete(f"/memory/spaces/{sid}").status_code == 204
        assert client.get(f"/memory/spaces/{sid}").status_code == 404


class TestIndexAndEntries:
    def _make_space(self, service, monkeypatch):
        client = _client(service, monkeypatch)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        return client, sid

    def test_index_read_update(self, service, monkeypatch):
        client, sid = self._make_space(service, monkeypatch)
        r = client.put(f"/memory/spaces/{sid}/index", json={"content": "# New\n"})
        assert r.status_code == 200
        assert client.get(f"/memory/spaces/{sid}/index").json()["content"] == "# New\n"

    def test_entry_upsert_read_list_delete(self, service, monkeypatch):
        client, sid = self._make_space(service, monkeypatch)

        up = client.put(
            f"/memory/spaces/{sid}/entries/jane-doe",
            json={
                "body": "# Jane\nVP Research",
                "type": "entity",
                "description": "VP Research",
                "indexed": {"status": "active"},
            },
        )
        assert up.status_code == 200
        assert up.json()["slug"] == "jane-doe"
        assert up.json()["type"] == "entity"

        got = client.get(f"/memory/spaces/{sid}/entries/jane-doe").json()
        assert "VP Research" in got["content"]

        listed = client.get(f"/memory/spaces/{sid}/entries").json()["entries"]
        assert [e["slug"] for e in listed] == ["jane-doe"]

        typed = client.get(f"/memory/spaces/{sid}/entries?type=fact").json()["entries"]
        assert typed == []

        assert client.delete(f"/memory/spaces/{sid}/entries/jane-doe").status_code == 204
        assert client.get(f"/memory/spaces/{sid}/entries/jane-doe").status_code == 404


class TestAccessControl:
    def test_stranger_cannot_read_space(self, service, monkeypatch):
        owner_client = _client(service, monkeypatch, user=OWNER)
        sid = owner_client.post("/memory/spaces", json={"name": "Private"}).json()[
            "spaceId"
        ]
        stranger_client = _client(service, monkeypatch, user=STRANGER)
        assert stranger_client.get(f"/memory/spaces/{sid}").status_code == 403

    def test_stranger_cannot_write_entry(self, service, monkeypatch):
        owner_client = _client(service, monkeypatch, user=OWNER)
        sid = owner_client.post("/memory/spaces", json={"name": "P"}).json()["spaceId"]
        stranger_client = _client(service, monkeypatch, user=STRANGER)
        r = stranger_client.put(
            f"/memory/spaces/{sid}/entries/x", json={"body": "hi"}
        )
        assert r.status_code == 403

    def test_stranger_space_not_in_their_list(self, service, monkeypatch):
        owner_client = _client(service, monkeypatch, user=OWNER)
        owner_client.post("/memory/spaces", json={"name": "P"})
        stranger_client = _client(service, monkeypatch, user=STRANGER)
        assert stranger_client.get("/memory/spaces").json()["spaces"] == []

    def test_stranger_cannot_export_space(self, service, monkeypatch):
        owner_client = _client(service, monkeypatch, user=OWNER)
        sid = owner_client.post("/memory/spaces", json={"name": "P"}).json()["spaceId"]
        stranger_client = _client(service, monkeypatch, user=STRANGER)
        assert stranger_client.get(f"/memory/spaces/{sid}/export").status_code == 403

    def test_member_leaves_via_delete(self, service, monkeypatch):
        owner_client = _client(service, monkeypatch, user=OWNER)
        sid = owner_client.post("/memory/spaces", json={"name": "Shared"}).json()[
            "spaceId"
        ]
        # share directly through the service (sharing endpoints land in A4)
        service.share(sid, OWNER.user_id, OWNER.email, STRANGER.email, "viewer")
        member_client = _client(service, monkeypatch, user=STRANGER)
        listed = member_client.get("/memory/spaces").json()["spaces"]
        assert {s["spaceId"] for s in listed} == {sid}
        # the list reports the member's actual grant, not a placeholder
        assert listed[0]["role"] == "viewer"
        # member DELETE = leave, not destroy
        assert member_client.delete(f"/memory/spaces/{sid}").status_code == 204
        assert member_client.get(f"/memory/spaces/{sid}").status_code == 403
        # the space still exists for the owner
        assert owner_client.get(f"/memory/spaces/{sid}").status_code == 200


class TestSharing:
    def _make_space(self, service, monkeypatch):
        owner = _client(service, monkeypatch, user=OWNER)
        sid = owner.post("/memory/spaces", json={"name": "Shared"}).json()["spaceId"]
        return owner, sid

    def test_owner_shares_lists_updates_revokes(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)

        added = owner.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": STRANGER.email, "permission": "viewer"},
        )
        assert added.status_code == 201
        assert added.json()["email"] == STRANGER.email
        assert added.json()["permission"] == "viewer"

        listed = owner.get(f"/memory/spaces/{sid}/shares").json()["members"]
        assert [(m["email"], m["permission"]) for m in listed] == [
            (STRANGER.email, "viewer")
        ]
        created_at = listed[0]["createdAt"]

        upgraded = owner.patch(
            f"/memory/spaces/{sid}/shares/{STRANGER.email}",
            json={"permission": "editor"},
        )
        assert upgraded.status_code == 200
        assert upgraded.json()["permission"] == "editor"
        # PATCH preserves the original grant timestamp.
        assert upgraded.json()["createdAt"] == created_at

        assert (
            owner.delete(f"/memory/spaces/{sid}/shares/{STRANGER.email}").status_code
            == 204
        )
        assert owner.get(f"/memory/spaces/{sid}/shares").json()["members"] == []

    def test_shared_member_gains_access(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)
        owner.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": STRANGER.email, "permission": "editor"},
        )
        member = _client(service, monkeypatch, user=STRANGER)
        # editor can now read and write entries
        assert member.get(f"/memory/spaces/{sid}").status_code == 200
        assert (
            member.put(
                f"/memory/spaces/{sid}/entries/note", json={"body": "hi"}
            ).status_code
            == 200
        )

    def test_non_owner_cannot_share(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)
        owner.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": STRANGER.email, "permission": "editor"},
        )
        # an editor is not an owner — cannot manage grants
        member = _client(service, monkeypatch, user=STRANGER)
        r = member.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": "third@example.edu", "permission": "viewer"},
        )
        assert r.status_code == 403

    def test_viewer_cannot_list_members(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)
        owner.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": STRANGER.email, "permission": "viewer"},
        )
        member = _client(service, monkeypatch, user=STRANGER)
        # listing members requires editor+
        assert member.get(f"/memory/spaces/{sid}/shares").status_code == 403

    def test_patch_unknown_member_404(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)
        r = owner.patch(
            f"/memory/spaces/{sid}/shares/nobody@example.edu",
            json={"permission": "editor"},
        )
        assert r.status_code == 404

    def test_share_rejects_owner_role(self, service, monkeypatch):
        owner, sid = self._make_space(service, monkeypatch)
        r = owner.post(
            f"/memory/spaces/{sid}/shares",
            json={"email": STRANGER.email, "permission": "owner"},
        )
        # "owner" is not a grantable ShareRole → 422 at the request model
        assert r.status_code == 422


class TestConsolidate:
    def test_owner_consolidate_returns_report(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        client.put(f"/memory/spaces/{sid}/entries/a", json={"body": "one"})
        # leak an orphan object to prove GC runs through the route
        service.store.put(space_id=sid, content=b"leaked", content_type="text/markdown")

        resp = client.post(f"/memory/spaces/{sid}/consolidate", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spaceId"] == sid
        assert body["entryCount"] == 1
        assert body["orphansDeleted"] == 1
        assert body["overCap"] is False
        assert body["duplicateGroups"] == []

    def test_consolidate_no_body(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        assert client.post(f"/memory/spaces/{sid}/consolidate").status_code == 200

    def test_viewer_cannot_consolidate(self, service, monkeypatch):
        owner = _client(service, monkeypatch, user=OWNER)
        sid = owner.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        service.share(sid, OWNER.user_id, OWNER.email, STRANGER.email, "viewer")
        member = _client(service, monkeypatch, user=STRANGER)
        assert member.post(f"/memory/spaces/{sid}/consolidate", json={}).status_code == 403

    def test_consolidate_404_when_flag_off(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        monkeypatch.setenv("MEMORY_SPACES_ENABLED", "false")
        assert client.post(f"/memory/spaces/{sid}/consolidate", json={}).status_code == 404


class TestExport:
    def _seed(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        sid = client.post(
            "/memory/spaces", json={"name": "My Brain", "template": "chief-of-staff"}
        ).json()["spaceId"]
        client.put(
            f"/memory/spaces/{sid}/entries/jane-doe",
            json={
                "body": "---\ntype: entity\n---\n# Jane\nVP Research",
                "type": "entity",
                "description": "VP Research",
            },
        )
        client.put(
            f"/memory/spaces/{sid}/entries/q3-goal",
            json={"body": "# Q3\nShip memory spaces", "type": "fact"},
        )
        return client, sid

    def _open_zip(self, resp) -> zipfile.ZipFile:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert 'filename="My-Brain.zip"' in resp.headers["content-disposition"]
        return zipfile.ZipFile(io.BytesIO(resp.content))

    def test_owner_export_layout_and_contents(self, service, monkeypatch):
        client, sid = self._seed(service, monkeypatch)
        zf = self._open_zip(client.get(f"/memory/spaces/{sid}/export"))

        names = set(zf.namelist())
        assert "My-Brain/MEMORY.md" in names
        assert "My-Brain/entries/entity/jane-doe.md" in names
        assert "My-Brain/entries/fact/q3-goal.md" in names
        assert "My-Brain/metadata.json" in names

        # Entry bytes are verbatim, frontmatter intact.
        assert b"VP Research" in zf.read("My-Brain/entries/entity/jane-doe.md")
        assert b"type: entity" in zf.read("My-Brain/entries/entity/jane-doe.md")
        # The index text is the seeded template's MEMORY.md.
        assert b"Strategic priorities" in zf.read("My-Brain/MEMORY.md")

        meta = json.loads(zf.read("My-Brain/metadata.json"))
        assert meta["spaceId"] == sid
        assert meta["name"] == "My Brain"
        assert meta["template"] == "chief-of-staff"
        assert meta["entryCount"] == 2
        assert meta["owner"]["email"] == OWNER.email
        assert meta["exportedAt"]

    def test_owner_metadata_lists_members(self, service, monkeypatch):
        client, sid = self._seed(service, monkeypatch)
        service.share(sid, OWNER.user_id, OWNER.email, STRANGER.email, "viewer")
        zf = self._open_zip(client.get(f"/memory/spaces/{sid}/export"))
        meta = json.loads(zf.read("My-Brain/metadata.json"))
        assert meta["members"] == [
            {"email": STRANGER.email, "permission": "viewer", "createdAt": meta["members"][0]["createdAt"]}
        ]

    def test_viewer_can_export_without_member_list(self, service, monkeypatch):
        owner_client, sid = self._seed(service, monkeypatch)
        service.share(sid, OWNER.user_id, OWNER.email, STRANGER.email, "viewer")
        viewer_client = _client(service, monkeypatch, user=STRANGER)
        resp = viewer_client.get(f"/memory/spaces/{sid}/export")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        meta = json.loads(zf.read("My-Brain/metadata.json"))
        # A viewer exports the corpus but not the grant list (list_members gate).
        assert meta["members"] == []
        assert meta["entryCount"] == 2

    def test_export_missing_space_404(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        assert client.get("/memory/spaces/spc_missing/export").status_code == 404

    def test_export_404_when_flag_off(self, service, monkeypatch):
        client, sid = self._seed(service, monkeypatch)
        monkeypatch.setenv("MEMORY_SPACES_ENABLED", "false")
        assert client.get(f"/memory/spaces/{sid}/export").status_code == 404

    def test_export_sanitizes_hostile_slug(self, service, monkeypatch):
        client = _client(service, monkeypatch, user=OWNER)
        sid = client.post("/memory/spaces", json={"name": "X"}).json()["spaceId"]
        # Seed a traversal slug directly through the service (the route path
        # converter would never carry one) — the export must not let it escape
        # its archive folder (zip-slip).
        service.write_entry(
            sid, OWNER.user_id, OWNER.email, "../../evil", "nope", entry_type="fact"
        )
        resp = client.get(f"/memory/spaces/{sid}/export")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert all(not n.startswith("..") and "/../" not in n for n in zf.namelist())
        assert "X/entries/fact/evil.md" in zf.namelist()
