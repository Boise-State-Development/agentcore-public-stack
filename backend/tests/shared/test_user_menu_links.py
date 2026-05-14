"""Tests for the user-menu links shared module (repository + service)."""

import boto3
import pytest
from pydantic import ValidationError

from apis.shared.user_menu_links.models import (
    UserMenuLink,
    UserMenuLinkCreate,
    UserMenuLinkUpdate,
)
from apis.shared.user_menu_links.repository import UserMenuLinksRepository
from apis.shared.user_menu_links.service import UserMenuLinksService

AWS_REGION = "us-west-2"


@pytest.fixture()
def user_menu_links_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-user-menu-links"
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
    monkeypatch.setenv("DYNAMODB_USER_MENU_LINKS_TABLE_NAME", name)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture()
def repo(user_menu_links_table):
    return UserMenuLinksRepository(table_name="test-user-menu-links", region=AWS_REGION)


@pytest.fixture()
def service(repo):
    return UserMenuLinksService(repo)


def _external(**kw):
    defaults = dict(label="Privacy policy", kind="external", url="https://x.example/p")
    defaults.update(kw)
    return UserMenuLinkCreate(**defaults)


def _modal(**kw):
    defaults = dict(label="About", kind="modal", body_markdown="# Hi")
    defaults.update(kw)
    return UserMenuLinkCreate(**defaults)


# ----------------------------------------------------------------------
# Pydantic validation
# ----------------------------------------------------------------------


class TestCreateValidation:
    def test_external_requires_url(self):
        with pytest.raises(ValidationError):
            UserMenuLinkCreate(label="X", kind="external")

    def test_modal_requires_body_markdown(self):
        with pytest.raises(ValidationError):
            UserMenuLinkCreate(label="X", kind="modal")

    def test_external_ok_with_url(self):
        link = UserMenuLinkCreate(label="X", kind="external", url="https://x.example")
        assert link.kind == "external"

    def test_modal_ok_with_body(self):
        link = UserMenuLinkCreate(label="X", kind="modal", body_markdown="hi")
        assert link.kind == "modal"

    def test_order_bounds(self):
        with pytest.raises(ValidationError):
            UserMenuLinkCreate(label="X", kind="external", url="https://x.example", order=-1)
        with pytest.raises(ValidationError):
            UserMenuLinkCreate(label="X", kind="external", url="https://x.example", order=10_001)

    @pytest.mark.parametrize(
        "bad_url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "//example.com/x",
            "example.com/x",
        ],
    )
    def test_external_rejects_non_http_url(self, bad_url):
        with pytest.raises(ValidationError):
            UserMenuLinkCreate(label="X", kind="external", url=bad_url)

    def test_external_accepts_http_and_https(self):
        UserMenuLinkCreate(label="X", kind="external", url="http://x.example")
        UserMenuLinkCreate(label="X", kind="external", url="HTTPS://X.example")

    def test_update_rejects_non_http_url(self):
        with pytest.raises(ValidationError):
            UserMenuLinkUpdate(url="javascript:alert(1)")


# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------


class TestRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, repo):
        created = await repo.create_link(_external(), created_by="admin@x")
        assert created.link_id
        fetched = await repo.get_link(created.link_id)
        assert fetched is not None
        assert fetched.label == "Privacy policy"
        assert fetched.created_by == "admin@x"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, repo):
        assert await repo.get_link("nope") is None

    @pytest.mark.asyncio
    async def test_list_returns_all_then_enabled_only(self, repo):
        a = await repo.create_link(_external(label="A", order=2))
        b = await repo.create_link(_modal(label="B", order=1, enabled=False))
        all_links = await repo.list_links()
        assert {link.link_id for link in all_links} == {a.link_id, b.link_id}
        enabled = await repo.list_links(enabled_only=True)
        assert [link.link_id for link in enabled] == [a.link_id]

    @pytest.mark.asyncio
    async def test_list_sorted_by_order_then_label(self, repo):
        await repo.create_link(_external(label="Beta", order=10))
        await repo.create_link(_external(label="Alpha", order=10))
        await repo.create_link(_external(label="First", order=0))
        labels = [link.label for link in await repo.list_links()]
        assert labels == ["First", "Alpha", "Beta"]

    @pytest.mark.asyncio
    async def test_update_partial_merges(self, repo):
        created = await repo.create_link(_external())
        updated = await repo.update_link(
            created.link_id, UserMenuLinkUpdate(label="Renamed")
        )
        assert updated is not None
        assert updated.label == "Renamed"
        assert updated.url == "https://x.example/p"  # untouched

    @pytest.mark.asyncio
    async def test_update_to_modal_requires_body(self, repo):
        created = await repo.create_link(_external())
        # Switching to modal without supplying body_markdown should raise:
        # the merged record has kind=modal but no body.
        with pytest.raises(ValueError, match="modal links require body_markdown"):
            await repo.update_link(created.link_id, UserMenuLinkUpdate(kind="modal"))

    @pytest.mark.asyncio
    async def test_update_to_modal_with_body_succeeds(self, repo):
        created = await repo.create_link(_external())
        updated = await repo.update_link(
            created.link_id,
            UserMenuLinkUpdate(kind="modal", body_markdown="hi"),
        )
        assert updated is not None
        assert updated.kind == "modal"
        assert updated.body_markdown == "hi"

    @pytest.mark.asyncio
    async def test_update_missing_returns_none(self, repo):
        assert await repo.update_link("nope", UserMenuLinkUpdate(label="x")) is None

    @pytest.mark.asyncio
    async def test_delete(self, repo):
        created = await repo.create_link(_external())
        assert await repo.delete_link(created.link_id) is True
        assert await repo.get_link(created.link_id) is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, repo):
        assert await repo.delete_link("nope") is False

    @pytest.mark.asyncio
    async def test_update_rejects_non_http_url_on_merge(self, repo):
        """Defense-in-depth: even if a bypass somehow stored a bad URL,
        an update that merges it back through the repo must reject it.
        We bypass Pydantic by mutating the existing record directly."""
        created = await repo.create_link(_external())
        # Force a bad URL into the persisted item to simulate corruption /
        # an out-of-band write.
        repo._table.update_item(
            Key={"PK": "USER_MENU_LINKS", "SK": f"LINK#{created.link_id}"},
            UpdateExpression="SET #u = :u",
            ExpressionAttributeNames={"#u": "url"},
            ExpressionAttributeValues={":u": "javascript:alert(1)"},
        )
        with pytest.raises(ValueError, match="url must start with"):
            await repo.update_link(
                created.link_id, UserMenuLinkUpdate(label="Renamed")
            )


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------


class TestService:
    @pytest.mark.asyncio
    async def test_create_then_list(self, service):
        await service.create_link(_external())
        links = await service.list_links()
        assert len(links) == 1

    @pytest.mark.asyncio
    async def test_enabled_only_filter(self, service):
        await service.create_link(_external(label="A"))
        await service.create_link(_external(label="B", enabled=False))
        enabled = await service.list_links(enabled_only=True)
        labels = [link.label for link in enabled]
        assert labels == ["A"]


# ----------------------------------------------------------------------
# Dataclass round-trip
# ----------------------------------------------------------------------


class TestDynamoRoundTrip:
    def test_to_and_from_dynamo_item(self):
        original = UserMenuLink(
            link_id="abc",
            label="Test",
            kind="modal",
            enabled=True,
            order=5,
            body_markdown="# Hi",
            created_at="2026-05-14T00:00:00Z",
            updated_at="2026-05-14T00:00:00Z",
        )
        item = original.to_dynamo_item()
        round_tripped = UserMenuLink.from_dynamo_item(item)
        assert round_tripped == original

    def test_from_dynamo_item_requires_timestamps(self):
        # Corrupted records (missing createdAt/updatedAt) should raise rather
        # than silently substituting "now".
        with pytest.raises(ValueError, match="missing required timestamp"):
            UserMenuLink.from_dynamo_item(
                {"linkId": "x", "label": "x", "kind": "external", "url": "https://x.example"}
            )
