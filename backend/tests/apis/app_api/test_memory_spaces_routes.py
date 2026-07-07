"""Route tests for the Memory Spaces user surface (`/memory/spaces/*`, A2).

Pins the flag gate (404 while off), CRUD happy paths, and identity-based
access (403 for a non-member, 404 for a missing space). Backed by a REAL
`MemorySpaceService` on moto (DynamoDB + S3), so the routes are exercised
end-to-end through the shared service.
"""

from __future__ import annotations

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
