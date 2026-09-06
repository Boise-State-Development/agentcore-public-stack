"""Tests for artifacts inside a shared conversation.

Sharing a conversation shares the artifacts it produced. The mechanism
is that the **conversation share is the grant** — there is no parallel
artifact-share record — and the snapshot pins the versions.

Two things carry the weight here:

* `resolve_shared_artifact` is the *whole* access boundary. The mint it
  feeds does no checking of its own, and the token's `sub` is a
  DynamoDB partition address, so a gap here is "read any artifact by
  id" against the owner's partition.
* The snapshot's artifact list is the allowlist. A recipient must not be
  able to name an artifact the share does not carry.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.shares.models import CreateShareRequest
from apis.app_api.shares.service import (
    AccessDeniedError,
    ShareNotFoundError,
    ShareService,
)
from apis.app_api.shares.snapshot_store import ShareSnapshotStore
from apis.shared.auth.models import User

AWS_REGION = "us-east-1"
BUCKET = "test-shared-conversations"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def s3_client(aws_env):
    client = boto3.client("s3", region_name=AWS_REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture()
def service(s3_client, monkeypatch):
    monkeypatch.setenv("SHARED_CONVERSATIONS_TABLE_NAME", "shares-table")
    store = ShareSnapshotStore(bucket_name=BUCKET, s3_client=s3_client)
    with patch("boto3.resource"):
        svc = ShareService(snapshot_store=store)
    svc._table = MagicMock()
    return svc


def _owner() -> User:
    return User(
        email="owner@example.com", user_id="owner-1", name="Owner", roles=[]
    )


def _viewer(email: str = "friend@example.com") -> User:
    return User(email=email, user_id="friend-1", name="Friend", roles=[])


def _patch_sources(heads: list[dict] | Exception):
    """Patch the three things `create_share` reads."""
    meta = MagicMock()
    meta.model_dump.return_value = {"title": "Chat"}
    messages = MagicMock(
        messages=[
            MagicMock(
                model_dump=MagicMock(
                    return_value={
                        "id": "m0",
                        "role": "user",
                        "content": [{"type": "text", "text": "hi"}],
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                )
            )
        ]
    )

    list_service = MagicMock()
    if isinstance(heads, Exception):
        list_service.heads_for_session.side_effect = heads
    else:
        list_service.heads_for_session.return_value = heads

    return (
        patch(
            "apis.app_api.shares.service.get_session_metadata",
            new=AsyncMock(return_value=meta),
        ),
        patch(
            "apis.app_api.shares.service.get_messages",
            new=AsyncMock(return_value=messages),
        ),
        patch(
            "apis.app_api.artifacts.service.get_artifact_list_service",
            return_value=list_service,
        ),
    )


def _head(artifact_id: str = "art-1", version: int = 3) -> dict:
    return {
        "artifact_id": artifact_id,
        "version": version,
        "title": "Quarterly Deck",
        "content_type": "text/html; charset=utf-8",
        "produced_by_message_index": 2,
    }


async def _create(service, heads, *, access="public", emails=None):
    meta_p, msgs_p, arts_p = _patch_sources(heads)
    with meta_p, msgs_p, arts_p:
        return await service.create_share(
            "sess-1",
            _owner(),
            CreateShareRequest(accessLevel=access, allowedEmails=emails),
        )


def _written_item(service) -> dict:
    """The DynamoDB item create_share wrote, as the read path sees it."""
    return service._table.put_item.call_args[1]["Item"]


def _snapshot_body(service, s3_client) -> dict:
    key = _written_item(service)["body_ref"]["bucket_key"]
    obj = s3_client.get_object(Bucket=BUCKET, Key=key)
    return json.loads(obj["Body"].read())


# ------------------------------------------------------------------
# Capture
# ------------------------------------------------------------------


class TestSnapshotCapture:
    @pytest.mark.asyncio
    async def test_pins_each_artifact_at_its_current_version(
        self, service, s3_client
    ):
        await _create(service, [_head(version=3)])

        artifacts = _snapshot_body(service, s3_client)["artifacts"]
        assert len(artifacts) == 1
        # The version at share time, not a moving HEAD pointer: a
        # recipient reading a frozen conversation must not be shown an
        # artifact the transcript around it never describes.
        assert artifacts[0]["version"] == 3
        assert artifacts[0]["artifact_id"] == "art-1"
        assert artifacts[0]["produced_by_message_index"] == 2

    @pytest.mark.asyncio
    async def test_a_conversation_with_no_artifacts_shares_fine(
        self, service, s3_client
    ):
        await _create(service, [])
        assert _snapshot_body(service, s3_client)["artifacts"] == []

    @pytest.mark.asyncio
    async def test_artifact_failure_does_not_fail_the_share(
        self, service, s3_client
    ):
        """Sharing a conversation must not break because the artifacts
        feature is off here, or because its table hiccuped. A share with
        no artifacts is what every share was before this existed."""
        await _create(service, RuntimeError("artifacts table is gone"))

        body = _snapshot_body(service, s3_client)
        assert body["artifacts"] == []
        # The conversation itself still made it.
        assert body["messages"][0]["content"][0]["text"] == "hi"


# ------------------------------------------------------------------
# Read
# ------------------------------------------------------------------


class TestSharedConversationResponse:
    @pytest.mark.asyncio
    async def test_returns_artifacts_with_the_conversation(self, service):
        await _create(service, [_head()])
        service._get_share_item = MagicMock(return_value=_written_item(service))

        resp = await service.get_shared_conversation(
            share_id=_written_item(service)["share_id"], requester=_viewer()
        )

        assert len(resp.artifacts) == 1
        assert resp.artifacts[0].artifact_id == "art-1"
        assert resp.artifacts[0].version == 3
        # Anchoring data, so the shared view can place the card under the
        # same turn the owner sees it under.
        assert resp.artifacts[0].produced_by_message_index == 2

    @pytest.mark.asyncio
    async def test_a_share_predating_artifacts_reads_as_empty(
        self, service, s3_client
    ):
        """Conversation sharing is already in production, so a body with
        no `artifacts` key is the common case, not an error. There is no
        migration and none is needed."""
        await _create(service, [_head()])
        item = _written_item(service)

        # Overwrite in place, at the key the item already points at, so
        # this reads back through the real resolution path rather than a
        # second object nothing references.
        s3_client.put_object(
            Bucket=BUCKET,
            Key=item["body_ref"]["bucket_key"],
            Body=json.dumps(
                {"metadata": {"title": "Chat"}, "messages": []}
            ).encode(),
        )
        service._get_share_item = MagicMock(return_value=item)

        resp = await service.get_shared_conversation(
            share_id=item["share_id"], requester=_viewer()
        )
        assert resp.artifacts == []

    @pytest.mark.asyncio
    async def test_a_legacy_inline_share_reads_as_empty(self, service):
        """Inline shares predate the S3 offload entirely — and artifacts
        by a wider margin. They must still open."""
        item = {
            "share_id": "s-legacy",
            "session_id": "sess-1",
            "owner_id": "owner-1",
            "owner_email": "owner@example.com",
            "access_level": "public",
            "created_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"title": "Old Chat"},
            "messages": [],
        }
        service._get_share_item = MagicMock(return_value=item)

        resp = await service.get_shared_conversation(
            share_id="s-legacy", requester=_viewer()
        )
        assert resp.artifacts == []
        assert resp.title == "Old Chat"


# ------------------------------------------------------------------
# The access boundary
# ------------------------------------------------------------------


class TestResolveSharedArtifact:
    @pytest.mark.asyncio
    async def test_returns_the_owner_and_the_pinned_version(self, service):
        await _create(service, [_head(version=3)])
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        owner_id, version = service.resolve_shared_artifact(
            share_id=item["share_id"],
            artifact_id="art-1",
            requester=_viewer(),
        )

        # The owner id is a DynamoDB partition address for the mint, not
        # an identity assertion. See mint_for_conversation_share.
        assert owner_id == "owner-1"
        assert version == 3

    @pytest.mark.asyncio
    async def test_an_artifact_outside_the_snapshot_is_404(self, service):
        """The load-bearing test.

        The snapshot list is the allowlist. Without this check, any valid
        share id plus a guessed artifact id would read the owner's whole
        artifact partition, because `sub` on the minted token is an
        address rather than an identity. 404 rather than 403, so it also
        reveals nothing about what the owner has."""
        await _create(service, [_head("art-1")])
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        with pytest.raises(ShareNotFoundError):
            service.resolve_shared_artifact(
                share_id=item["share_id"],
                artifact_id="art-somebody-elses",
                requester=_viewer(),
            )

    @pytest.mark.asyncio
    async def test_a_viewer_outside_the_allowlist_is_denied(self, service):
        await _create(
            service,
            [_head()],
            access="specific",
            emails=["invited@example.com"],
        )
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        with pytest.raises(AccessDeniedError):
            service.resolve_shared_artifact(
                share_id=item["share_id"],
                artifact_id="art-1",
                requester=_viewer("uninvited@example.com"),
            )

    @pytest.mark.asyncio
    async def test_access_follows_the_conversation_share(self, service):
        """The point of having no separate artifact-share record.

        Narrowing the conversation's allowlist has to lock the artifacts
        down in the same write — with parallel artifact shares this is
        where a missed cascade would leave them readable."""
        await _create(
            service,
            [_head()],
            access="specific",
            emails=["friend@example.com"],
        )
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        # Allowed while on the list.
        assert service.resolve_shared_artifact(
            share_id=item["share_id"],
            artifact_id="art-1",
            requester=_viewer(),
        )

        # Owner edits the allowlist — one write, no cascade.
        item["allowed_emails"] = ["someone.else@example.com"]

        with pytest.raises(AccessDeniedError):
            service.resolve_shared_artifact(
                share_id=item["share_id"],
                artifact_id="art-1",
                requester=_viewer(),
            )

    @pytest.mark.asyncio
    async def test_a_revoked_share_takes_its_artifacts_with_it(self, service):
        await _create(service, [_head()])
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=None)

        with pytest.raises(ShareNotFoundError):
            service.resolve_shared_artifact(
                share_id=item["share_id"],
                artifact_id="art-1",
                requester=_viewer(),
            )

    @pytest.mark.asyncio
    async def test_the_owner_can_resolve_their_own(self, service):
        await _create(
            service, [_head()], access="specific", emails=["a@example.com"]
        )
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        owner_id, _ = service.resolve_shared_artifact(
            share_id=item["share_id"],
            artifact_id="art-1",
            requester=_owner(),
        )
        assert owner_id == "owner-1"


# ------------------------------------------------------------------
# The route
# ------------------------------------------------------------------


class TestMintRoute:
    """The HTTP surface, wired to a real ShareService.

    These exist because the boundary is split across two modules: the
    grant lives here and the minting lives in `artifacts/service.py`.
    Unit tests on either half can both pass while the route wires them
    together wrongly.
    """

    @pytest.fixture()
    def client(self, service, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apis.app_api.shares import routes as share_routes
        from apis.shared.auth import get_current_user_from_session

        monkeypatch.setenv(
            "ARTIFACTS_RENDER_TOKEN_SECRET_ARN", "arn:aws:secret:test"
        )
        monkeypatch.setenv("ARTIFACTS_ORIGIN", "https://a.test.example.com")

        def make(user: User):
            app = FastAPI()
            app.include_router(share_routes.shared_view_router)
            app.dependency_overrides[get_current_user_from_session] = (
                lambda: user
            )
            monkeypatch.setattr(
                share_routes, "get_share_service", lambda: service
            )
            return TestClient(app)

        return make

    @pytest.mark.asyncio
    async def test_mints_for_a_permitted_viewer(
        self, service, client, monkeypatch
    ):
        await _create(service, [_head(version=3)])
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        seen = {}

        def fake_mint(**kwargs):
            seen.update(kwargs)
            return "https://a.test.example.com/?t=jwt", 1800000000

        from apis.app_api.shares import routes as share_routes

        monkeypatch.setattr(
            share_routes,
            "get_render_token_service",
            lambda: MagicMock(mint_for_conversation_share=fake_mint),
        )

        resp = client(_viewer()).post(
            f"/shared/{item['share_id']}/artifacts/art-1/render-token"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["url"].endswith("?t=jwt")
        # The route must hand the mint the OWNER (a partition address)
        # and the PINNED version — not the viewer, and not HEAD.
        assert seen["owner_id"] == "owner-1"
        assert seen["version"] == 3
        assert seen["viewer"].user_id == "friend-1"
        assert seen["conversation_share_id"] == item["share_id"]

    @pytest.mark.asyncio
    async def test_an_artifact_outside_the_snapshot_is_404(
        self, service, client
    ):
        await _create(service, [_head("art-1")])
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        resp = client(_viewer()).post(
            f"/shared/{item['share_id']}/artifacts/art-other/render-token"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a_viewer_outside_the_allowlist_is_403(
        self, service, client
    ):
        await _create(
            service,
            [_head()],
            access="specific",
            emails=["invited@example.com"],
        )
        item = _written_item(service)
        service._get_share_item = MagicMock(return_value=item)

        resp = client(_viewer("uninvited@example.com")).post(
            f"/shared/{item['share_id']}/artifacts/art-1/render-token"
        )
        assert resp.status_code == 403
