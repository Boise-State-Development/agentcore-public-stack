"""Route tests for the user-facing announcement surface.

The filter rules themselves are covered exhaustively (and without moto) in
``test_announcements_visibility.py``. What is tested here is the wiring: that
the endpoint serves the computed feed, that the ack path is monotonic
end-to-end, that an id targeted at another role 404s rather than 403s, and
that the payload does not leak admin metadata.
"""

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.announcements import repository as repo_module
from apis.shared.announcements import service as service_module
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User

AWS_REGION = "us-east-1"
TABLE_NAME = "test-announcements-user-routes"
USERS_TABLE = "test-announcements-users"

PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


def _make_user(user_id: str = "u1", roles=None) -> User:
    return User(
        email=f"{user_id}@example.com",
        user_id=user_id,
        name="Test User",
        roles=roles if roles is not None else ["User"],
    )


@pytest.fixture()
def announcements_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    for name in (TABLE_NAME, USERS_TABLE):
        ddb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    monkeypatch.setenv("DYNAMODB_ANNOUNCEMENTS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    # No users table configured: `_user_created_at` returns None, which the
    # filter reads as "existing user". The new-user rule has its own coverage.
    monkeypatch.delenv("DYNAMODB_USERS_TABLE_NAME", raising=False)
    monkeypatch.setattr(repo_module, "_repository", None)
    monkeypatch.setattr(service_module, "_service", None)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _client(user: User = None) -> TestClient:
    from apis.app_api.announcements.routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user or _make_user()
    return TestClient(app)


async def _seed(**kw):
    """Create + publish one announcement straight through the service."""
    from apis.shared.announcements.models import AnnouncementCreate

    service = service_module.get_announcements_service()
    defaults = dict(title="Skills are here", body_markdown="# Skills", publish_at=PAST)
    defaults.update(kw)
    created = await service.create_announcement(AnnouncementCreate(**defaults))
    return await service.publish(created.announcement_id)


class TestFeed:
    @pytest.mark.asyncio
    async def test_returns_published_announcements(self, announcements_table):
        await _seed()
        body = _client().get("/announcements/").json()

        assert len(body["panel"]) == 1
        assert body["panel"][0]["title"] == "Skills are here"
        assert body["unread_count"] == 1
        assert body["banner"] is None and body["modal"] is None

    @pytest.mark.asyncio
    async def test_drafts_are_invisible(self, announcements_table):
        from apis.shared.announcements.models import AnnouncementCreate

        await service_module.get_announcements_service().create_announcement(
            AnnouncementCreate(title="Draft", body_markdown="x", publish_at=PAST)
        )
        assert _client().get("/announcements/").json()["panel"] == []

    @pytest.mark.asyncio
    async def test_a_banner_announcement_fills_the_banner_slot(self, announcements_table):
        await _seed(surfaces=["panel", "banner"], expires_at=FUTURE)
        body = _client().get("/announcements/").json()

        assert body["banner"] is not None
        assert len(body["panel"]) == 1, "the banner item is also a panel item"

    @pytest.mark.asyncio
    async def test_targeting_scopes_the_feed_per_user(self, announcements_table):
        await _seed(title="Faculty only", target_roles=["faculty"])

        assert _client(_make_user("u1", ["student"])).get("/announcements/").json()["panel"] == []
        assert (
            len(_client(_make_user("u2", ["faculty"])).get("/announcements/").json()["panel"]) == 1
        )

    @pytest.mark.asyncio
    async def test_payload_omits_admin_metadata(self, announcements_table):
        """A user must not learn which roles a notice was aimed at, who wrote
        it, or that it exists in a state they cannot see."""
        await _seed(target_roles=["faculty", "staff"])
        item = _client(_make_user("u1", ["faculty"])).get("/announcements/").json()["panel"][0]

        for leaked in ("target_roles", "state", "created_by", "show_to_new_users", "updated_at"):
            assert leaked not in item, f"{leaked} leaked into the user payload"

    @pytest.mark.asyncio
    async def test_acks_are_scoped_to_the_caller(self, announcements_table):
        announcement = await _seed()
        _client(_make_user("u1")).post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "seen", "surface": "panel"},
        )

        assert _client(_make_user("u1")).get("/announcements/").json()["unread_count"] == 0
        assert _client(_make_user("u2")).get("/announcements/").json()["unread_count"] == 1


class TestNewUserSuppressionWiring:
    """The §D6 rule itself is covered in the visibility tests. What is covered
    here is the *wiring* — that the route actually reads `created_at` off the
    user profile. A bug here (wrong repository, wrong field, an exception
    swallowed too eagerly) would silently disable new-user suppression while
    every unit test still passed."""

    @pytest.fixture()
    def users_table(self, announcements_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_USERS_TABLE_NAME", USERS_TABLE)
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(USERS_TABLE)

    def _put_user(self, table, user_id: str, created_at: str) -> None:
        table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "PROFILE",
                "userId": user_id,
                "email": f"{user_id}@example.com",
                "name": "Test User",
                "emailDomain": "example.com",
                "createdAt": created_at,
                "lastLoginAt": created_at,
            }
        )

    @pytest.mark.asyncio
    async def test_a_user_who_joined_after_publication_sees_nothing(self, users_table):
        await _seed(publish_at="2026-01-01T00:00:00Z")
        self._put_user(users_table, "newbie", "2026-06-01T00:00:00Z")

        body = _client(_make_user("newbie")).get("/announcements/").json()
        assert body["panel"] == []
        assert body["unread_count"] == 0

    @pytest.mark.asyncio
    async def test_a_user_who_joined_earlier_still_sees_it(self, users_table):
        await _seed(publish_at="2026-01-01T00:00:00Z")
        self._put_user(users_table, "oldtimer", "2025-01-01T00:00:00Z")

        assert len(_client(_make_user("oldtimer")).get("/announcements/").json()["panel"]) == 1

    @pytest.mark.asyncio
    async def test_a_missing_profile_fails_toward_showing(self, users_table):
        """Failing toward showing a message is recoverable; failing toward
        silence is not — a directory blip must not decide what a user reads."""
        await _seed(publish_at="2026-01-01T00:00:00Z")

        assert len(_client(_make_user("ghost")).get("/announcements/").json()["panel"]) == 1


class TestAck:
    @pytest.mark.asyncio
    async def test_ack_returns_204_and_clears_unread(self, announcements_table):
        announcement = await _seed()
        client = _client()

        resp = client.post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "seen", "surface": "panel"},
        )
        assert resp.status_code == 204
        assert client.get("/announcements/").json()["unread_count"] == 0

    @pytest.mark.asyncio
    async def test_dismiss_drops_the_banner_but_keeps_the_panel_entry(
        self, announcements_table
    ):
        announcement = await _seed(surfaces=["panel", "banner"], expires_at=FUTURE)
        client = _client()

        client.post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "dismissed", "surface": "banner"},
        )

        body = client.get("/announcements/").json()
        assert body["banner"] is None
        assert len(body["panel"]) == 1

    @pytest.mark.asyncio
    async def test_a_late_seen_does_not_resurrect_a_dismissed_banner(
        self, announcements_table
    ):
        """The §D2 race, end to end through the HTTP surface.

        `seen` is written on render and can land after the user's ✕. If the
        monotonic guard were only in a unit test, this is the path that would
        regress.
        """
        announcement = await _seed(surfaces=["panel", "banner"], expires_at=FUTURE)
        client = _client()
        url = f"/announcements/{announcement.announcement_id}/ack"

        client.post(url, json={"action": "dismissed", "surface": "banner"})
        late = client.post(url, json={"action": "seen", "surface": "banner"})

        assert late.status_code == 204, "a no-op ack is success, not an error"
        assert client.get("/announcements/").json()["banner"] is None

    @pytest.mark.asyncio
    async def test_ack_is_idempotent(self, announcements_table):
        announcement = await _seed()
        client = _client()
        url = f"/announcements/{announcement.announcement_id}/ack"

        assert client.post(url, json={"action": "seen", "surface": "panel"}).status_code == 204
        assert client.post(url, json={"action": "seen", "surface": "panel"}).status_code == 204

    @pytest.mark.asyncio
    async def test_ack_on_an_unknown_id_is_404(self, announcements_table):
        resp = _client().post(
            "/announcements/does-not-exist/ack",
            json={"action": "seen", "surface": "panel"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ack_on_another_roles_announcement_is_404_not_403(
        self, announcements_table
    ):
        """403 would confirm the announcement exists. 404 does not."""
        announcement = await _seed(target_roles=["faculty"])

        resp = _client(_make_user("u1", ["student"])).post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "dismissed", "surface": "panel"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ack_on_a_draft_is_404(self, announcements_table):
        from apis.shared.announcements.models import AnnouncementCreate

        draft = await service_module.get_announcements_service().create_announcement(
            AnnouncementCreate(title="Draft", body_markdown="x", publish_at=PAST)
        )
        resp = _client().post(
            f"/announcements/{draft.announcement_id}/ack",
            json={"action": "seen", "surface": "panel"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_422(self, announcements_table):
        announcement = await _seed()
        resp = _client().post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "skimmed", "surface": "panel"},
        )
        assert resp.status_code == 422


class TestRevision:
    @pytest.mark.asyncio
    async def test_a_revise_makes_a_dismissed_item_unread_and_updated(
        self, announcements_table
    ):
        announcement = await _seed(surfaces=["panel", "banner"], expires_at=FUTURE)
        client = _client()
        client.post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "dismissed", "surface": "banner"},
        )
        assert client.get("/announcements/").json()["banner"] is None

        await service_module.get_announcements_service().revise(announcement.announcement_id)

        body = client.get("/announcements/").json()
        assert body["banner"] is not None, "the revision lapsed the suppression"
        assert body["panel"][0]["is_updated"] is True
        assert body["panel"][0]["revision"] == 2
        assert body["unread_count"] == 1

    @pytest.mark.asyncio
    async def test_an_edit_does_not_re_show_a_dismissed_item(self, announcements_table):
        """A typo fix must not re-fire at everyone who already dismissed."""
        from apis.shared.announcements.models import AnnouncementUpdate

        announcement = await _seed(surfaces=["panel", "banner"], expires_at=FUTURE)
        client = _client()
        client.post(
            f"/announcements/{announcement.announcement_id}/ack",
            json={"action": "dismissed", "surface": "banner"},
        )

        await service_module.get_announcements_service().update_announcement(
            announcement.announcement_id, AnnouncementUpdate(title="Skills are here!")
        )

        body = client.get("/announcements/").json()
        assert body["banner"] is None
        assert body["panel"][0]["title"] == "Skills are here!"


class TestFeatureFlag:
    @pytest.mark.asyncio
    async def test_both_routes_404_when_disabled(self, announcements_table, monkeypatch):
        announcement = await _seed()
        monkeypatch.setenv("ANNOUNCEMENTS_ENABLED", "false")
        client = _client()

        assert client.get("/announcements/").status_code == 404
        assert (
            client.post(
                f"/announcements/{announcement.announcement_id}/ack",
                json={"action": "seen", "surface": "panel"},
            ).status_code
            == 404
        )

    @pytest.mark.asyncio
    async def test_routes_serve_when_the_flag_is_unset(self, announcements_table, monkeypatch):
        await _seed()
        monkeypatch.delenv("ANNOUNCEMENTS_ENABLED", raising=False)
        assert _client().get("/announcements/").status_code == 200


class TestAuth:
    def test_every_route_requires_a_session(self):
        """Cookie session auth, never Bearer — CLAUDE.md's app_api rule."""
        from apis.app_api.announcements import routes as announcement_routes

        unauthenticated = []
        for route in announcement_routes.router.routes:
            names = {
                sub.call.__name__
                for sub in getattr(getattr(route, "dependant", None), "dependencies", [])
                if getattr(sub, "call", None)
            }
            nested = set()
            for sub in getattr(getattr(route, "dependant", None), "dependencies", []):
                nested |= {
                    inner.call.__name__
                    for inner in getattr(sub, "dependencies", [])
                    if getattr(inner, "call", None)
                }
            if "get_current_user_from_session" not in (names | nested):
                unauthenticated.append(getattr(route, "path", "?"))

        assert unauthenticated == []
