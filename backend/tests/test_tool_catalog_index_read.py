"""`list_tools` reads EntityTypeIndex, and must never answer "no tools" wrongly.

The Query is the optimization; the fallbacks are the reason it is safe to ship.
An empty tool catalog is not a degraded experience — every user loses every
tool — and both ways this index can fail to answer produce exactly that unless
something catches them:

* the index is absent (deploy race), which raises; and
* the index is present but unpopulated (backfill not run), which does NOT raise —
  a sparse index simply matches nothing.

These tests exist for the fallbacks more than for the happy path.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from apis.shared.caching import config_cache
from apis.shared.tools.models import ENTITY_TYPE_TOOL, ToolDefinition
from apis.shared.tools.repository import ENTITY_TYPE_INDEX, ToolCatalogRepository

REGION = "us-east-1"
TABLE = "test-app-roles-index-read"

KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]
ATTRS = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
    {"AttributeName": "GSI5PK", "AttributeType": "S"},
    {"AttributeName": "GSI5SK", "AttributeType": "S"},
    {"AttributeName": "GSI1PK", "AttributeType": "S"},
    {"AttributeName": "GSI1SK", "AttributeType": "S"},
]

# The category path queries this one; it exists on the real table and is
# deliberately untouched by this change.
CATEGORY_INDEX = {
    "IndexName": "JwtRoleMappingIndex",
    "KeySchema": [
        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}
INDEX = {
    "IndexName": ENTITY_TYPE_INDEX,
    "KeySchema": [
        {"AttributeName": "GSI5PK", "KeyType": "HASH"},
        {"AttributeName": "GSI5SK", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


def _make_table(with_index: bool):
    # Without EntityTypeIndex the table still has the category index, matching
    # the real pre-deploy shape rather than a table with no indexes at all.
    attrs = ATTRS if with_index else [a for a in ATTRS if not a["AttributeName"].startswith("GSI5")]
    kwargs = {
        "TableName": TABLE,
        "KeySchema": KEY_SCHEMA,
        "AttributeDefinitions": attrs,
        "BillingMode": "PAY_PER_REQUEST",
        "GlobalSecondaryIndexes": [CATEGORY_INDEX] + ([INDEX] if with_index else []),
    }
    boto3.client("dynamodb", region_name=REGION).create_table(**kwargs)
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _tool(tool_id: str, stamped: bool = True) -> dict:
    item = ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id.title(),
        description="d",
        category="utility",
        protocol="local",
    ).to_dynamo_item()
    if not stamped:
        # A row written before the keys existed — the pre-backfill state.
        item.pop("GSI5PK", None)
        item.pop("GSI5SK", None)
    return item


def _seed_noise(table):
    """The other tenants of this shared table. None may reach the catalog."""
    table.put_item(Item={"PK": "SKILL#research", "SK": "METADATA"})
    table.put_item(Item={"PK": "ROLE#student", "SK": "DEFINITION"})
    table.put_item(Item={"PK": "USER#u1", "SK": "TOOL_PREFERENCES"})
    table.put_item(Item={"PK": "TOOL#calculator", "SK": "CAPABILITIES"})


def _repo(monkeypatch):
    monkeypatch.setenv("DYNAMODB_APP_ROLES_TABLE_NAME", TABLE)
    config_cache.get_config_cache().clear()
    return ToolCatalogRepository(table_name=TABLE)


class TestQueryPath:
    @pytest.mark.asyncio
    async def test_reads_the_catalog_off_the_index(self, aws, monkeypatch):
        table = _make_table(with_index=True)
        _seed_noise(table)
        for t in ("calculator", "web_search"):
            table.put_item(Item=_tool(t))

        tools = await _repo(monkeypatch).list_tools()

        assert sorted(t.tool_id for t in tools) == ["calculator", "web_search"]

    @pytest.mark.asyncio
    async def test_index_excludes_the_shared_table_noise(self, aws, monkeypatch):
        """The whole point: skills, roles and per-user rows never come back."""
        table = _make_table(with_index=True)
        _seed_noise(table)
        table.put_item(Item=_tool("calculator"))

        tools = await _repo(monkeypatch).list_tools()

        assert [t.tool_id for t in tools] == ["calculator"]

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_catalog_is_empty(self, aws, monkeypatch):
        table = _make_table(with_index=True)
        _seed_noise(table)

        assert await _repo(monkeypatch).list_tools() == []


class TestMissingIndexFallback:
    @pytest.mark.asyncio
    async def test_serves_the_catalog_when_the_index_does_not_exist(
        self, aws, monkeypatch
    ):
        """The deploy race: backend ships before platform. Must NOT go empty —
        we have a correct answer available, so serve it."""
        table = _make_table(with_index=False)
        _seed_noise(table)
        for t in ("calculator", "web_search"):
            table.put_item(Item=_tool(t))

        tools = await _repo(monkeypatch).list_tools()

        assert sorted(t.tool_id for t in tools) == ["calculator", "web_search"]

    @pytest.mark.asyncio
    async def test_a_real_query_error_still_propagates(self, aws, monkeypatch):
        """Only 'index missing' is absorbed. Throttling must not read as
        'there are no tools' — that is a lie about the data."""
        _make_table(with_index=True)
        repo = _repo(monkeypatch)

        def boom(**_kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "slow down"}},
                "Query",
            )

        monkeypatch.setattr(repo._table, "query", boom)

        with pytest.raises(ClientError):
            await repo.list_tools()


class TestUnbackfilledIndexFallback:
    @pytest.mark.asyncio
    async def test_serves_the_catalog_when_the_index_is_unpopulated(
        self, aws, monkeypatch
    ):
        """The silent case: index exists, backfill never ran, Query succeeds with
        zero rows and raises nothing. Without the guard every user loses every
        tool and no error is logged anywhere."""
        table = _make_table(with_index=True)
        _seed_noise(table)
        for t in ("calculator", "web_search"):
            table.put_item(Item=_tool(t, stamped=False))

        tools = await _repo(monkeypatch).list_tools()

        assert sorted(t.tool_id for t in tools) == ["calculator", "web_search"]

    @pytest.mark.asyncio
    async def test_says_loudly_that_the_backfill_has_not_run(
        self, aws, monkeypatch, caplog
    ):
        table = _make_table(with_index=True)
        table.put_item(Item=_tool("calculator", stamped=False))

        with caplog.at_level("ERROR"):
            await _repo(monkeypatch).list_tools()

        assert "backfill_tool_catalog_index.py" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_table_logs_nothing_alarming(self, aws, monkeypatch, caplog):
        """A fresh install before seeding is not a broken backfill."""
        _make_table(with_index=True)

        with caplog.at_level("ERROR"):
            assert await _repo(monkeypatch).list_tools() == []

        assert "backfill" not in caplog.text


class TestCategoryPathUnchanged:
    @pytest.mark.asyncio
    async def test_category_filter_still_uses_its_own_index(self, aws, monkeypatch):
        """The category read is a separate bounded GSI query and is deliberately
        untouched — it must not start returning the whole catalog."""
        table = _make_table(with_index=True)
        table.put_item(Item=_tool("calculator"))

        repo = _repo(monkeypatch)
        called = {"n": 0}
        original = repo._query_tool_items_by_category

        def spy(category):
            called["n"] += 1
            return original(category)

        monkeypatch.setattr(repo, "_query_tool_items_by_category", spy)
        await repo.list_tools(category="utility")

        assert called["n"] == 1
