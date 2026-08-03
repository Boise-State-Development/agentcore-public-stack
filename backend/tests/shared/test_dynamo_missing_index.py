"""A missing GSI degrades user-facing reads to empty instead of 500ing.

This regression exists because it shipped: on 2026-08-01, release 1.12.0's CloudFormation
deploy rolled back on an unrelated DynamoDB limit while the backend and frontend shipped
anyway, so ``AgentDirectoryIndex`` and ``AgentReportsIndex`` were absent in production.
The agent store — GA'd to every user in that same release — returned a 500 on every page
load until the infrastructure was repaired two releases later.

Two properties are worth pinning, and they pull in opposite directions:

* **The reads degrade.** A missing index must produce an empty shelf / empty queue, not an
  exception. A refactor that drops the ``except`` block is the regression.
* **The match stays narrow.** It would be trivially easy to "fix" this by catching every
  ``ClientError``, at which point a throttle or a malformed key condition also produces an
  empty result — a permanently silent surface nobody would think to debug. The narrowness
  tests below fail on that "fix".

Both wire spellings are covered. Moto raises ``ResourceNotFoundException`` ("Invalid index:
X for table: Y"); real DynamoDB raises ``ValidationException`` ("The table does not have
the specified index: X"). Only moto's is reachable by actually dropping the index from the
fixture, so the production spelling is injected directly — testing only what moto emits
would leave the code path that actually fired in production unexercised.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from apis.shared.assistants.listing_repository import query_store
from apis.shared.assistants.reports import (
    count_open_reports,
    list_open_reports,
    submit_report,
)
from apis.shared.dynamo_errors import is_missing_index_error

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT = "ast-001"
CATEGORY = "productivity"


def _client_error(code: str, message: str, operation: str = "Query") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


# The two messages that actually mean "that index is not there".
_REAL_DDB = _client_error(
    "ValidationException",
    "The table does not have the specified index: AgentDirectoryIndex",
)
_MOTO = _client_error(
    "ResourceNotFoundException",
    "Invalid index: AgentDirectoryIndex for table: test-rag-assistants. "
    "Available indexes are: ",
)


# ── the matcher: what it accepts ─────────────────────────────────────────────────────
@pytest.mark.parametrize("error", [_REAL_DDB, _MOTO], ids=["real-dynamodb", "moto"])
def test_both_wire_spellings_of_a_missing_index_are_recognized(error):
    assert is_missing_index_error(error) is True


# ── the matcher: what it must NOT accept ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "error,why",
    [
        (
            _client_error(
                "ValidationException",
                "Query condition missed key schema element: GSI5_PK",
            ),
            "a malformed key condition is a code bug, not a deploy state",
        ),
        (
            _client_error(
                "ValidationException",
                "Invalid attribute value type",
            ),
            "a bad ExclusiveStartKey must not silently empty the shelf",
        ),
        (
            _client_error(
                "ValidationException",
                "Attribute name is a reserved keyword; reserved keyword: state",
            ),
            "an unescaped reserved word is a code bug",
        ),
        (
            _client_error("ResourceNotFoundException", "Requested resource not found"),
            "a missing *table* shares the code but is a misconfiguration, not a lag",
        ),
        (
            _client_error(
                "ProvisionedThroughputExceededException",
                "The level of configured provisioned throughput for the table was exceeded",
            ),
            "throttling means 'ask again', never 'there is nothing here'",
        ),
        (
            _client_error("AccessDeniedException", "User is not authorized to perform: dynamodb:Query"),
            "a missing IAM grant must be loud",
        ),
        (
            _client_error("ThrottlingException", "Rate of requests exceeds the allowed throughput"),
            "same as above",
        ),
    ],
)
def test_narrow_match_leaves_every_other_failure_loud(error, why):
    assert is_missing_index_error(error) is False, why


def test_a_non_client_error_is_not_a_missing_index():
    """The helper takes ``Any``, so a stray exception must not read as a deploy lag."""
    assert is_missing_index_error(ValueError("boom")) is False
    assert is_missing_index_error(None) is False


# ── fixtures: the same table, with and without its indexes ───────────────────────────
def _make_table(monkeypatch, *, gsis):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)

    ddb = boto3.resource("dynamodb", region_name=REGION)
    attrs = [
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
    ]
    definitions = {
        "AgentDirectoryIndex": ("GSI5_PK", "GSI5_SK"),
        "AgentReportsIndex": ("GSI6_PK", "GSI6_SK"),
    }
    index_params = []
    for name in gsis:
        pk, sk = definitions[name]
        attrs.extend(
            [
                {"AttributeName": pk, "AttributeType": "S"},
                {"AttributeName": sk, "AttributeType": "S"},
            ]
        )
        index_params.append(
            {
                "IndexName": name,
                "KeySchema": [
                    {"AttributeName": pk, "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        )

    params = dict(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=attrs,
        BillingMode="PAY_PER_REQUEST",
    )
    if index_params:
        params["GlobalSecondaryIndexes"] = index_params
    ddb.create_table(**params)
    return ddb.Table(TABLE)


@pytest.fixture()
def indexed_table(monkeypatch):
    """The table as production is *supposed* to have it — both GSIs present."""
    with mock_aws():
        yield _make_table(monkeypatch, gsis=["AgentDirectoryIndex", "AgentReportsIndex"])


@pytest.fixture()
def unindexed_table(monkeypatch):
    """The table as production actually had it on 2026-08-01 — neither GSI built."""
    with mock_aws():
        yield _make_table(monkeypatch, gsis=[])


def _seed_shelf_row(table, agent_id: str = AGENT, created_at: str = "2026-01-01T00:00:00.000000"):
    """A published ``VERSION#`` row carrying the GSI5 keys browse reads."""
    table.put_item(
        Item={
            "PK": f"AST#{agent_id}",
            "SK": "VERSION#000001",
            "assistantId": agent_id,
            "name": "An Agent",
            "GSI5_PK": f"LISTED#{CATEGORY}",
            "GSI5_SK": f"CREATED#{created_at}",
        }
    )


# ── the store browse ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_store_browse_returns_the_shelf_when_the_index_exists(indexed_table):
    """The normal path, so a degradation that always fires cannot pass as a fix."""
    _seed_shelf_row(indexed_table)

    items, cursor = await query_store(CATEGORY)

    assert [item["assistantId"] for item in items] == [AGENT]
    assert cursor is None


@pytest.mark.asyncio
async def test_store_browse_serves_an_empty_shelf_when_the_index_is_missing(unindexed_table):
    """The 2026-08-01 incident: this returned a 500 to every user on the store's GA."""
    _seed_shelf_row(unindexed_table)

    items, cursor = await query_store(CATEGORY)

    assert items == []
    assert cursor is None


@pytest.mark.asyncio
async def test_store_browse_logs_the_index_name_when_it_degrades(unindexed_table, caplog):
    """An empty shelf with no log is indistinguishable from an empty store."""
    with caplog.at_level("WARNING"):
        await query_store(CATEGORY)

    assert "AgentDirectoryIndex" in caplog.text


@pytest.mark.asyncio
async def test_store_browse_degrades_on_the_production_error_spelling(indexed_table, monkeypatch):
    """Moto cannot produce ``ValidationException``, which is what actually fired in prod."""
    import apis.shared.assistants.listing_repository as repo

    class _Raises:
        def query(self, **_kwargs):
            raise _REAL_DDB

    monkeypatch.setattr(repo, "_table", lambda: _Raises())

    assert await query_store(CATEGORY) == ([], None)


@pytest.mark.asyncio
async def test_store_browse_still_raises_on_an_unrelated_failure(indexed_table, monkeypatch):
    """The narrowness property, at the call site rather than only in the matcher."""
    import apis.shared.assistants.listing_repository as repo

    throttled = _client_error(
        "ProvisionedThroughputExceededException",
        "The level of configured provisioned throughput for the table was exceeded",
    )

    class _Raises:
        def query(self, **_kwargs):
            raise throttled

    monkeypatch.setattr(repo, "_table", lambda: _Raises())

    with pytest.raises(ClientError):
        await query_store(CATEGORY)


# ── the admin report queue ───────────────────────────────────────────────────────────
async def _file_report(agent_id: str = AGENT):
    return await submit_report(
        agent_id,
        reporter_id="user-reporter",
        reporter_name="Rae Reporter",
        reason="broken",
        note="It errors out",
    )


@pytest.mark.asyncio
async def test_report_queue_returns_open_reports_when_the_index_exists(indexed_table):
    await _file_report()

    assert [r.agent_id for r in await list_open_reports()] == [AGENT]
    assert await count_open_reports() == 1


@pytest.mark.asyncio
async def test_report_queue_is_empty_rather_than_broken_when_the_index_is_missing(
    unindexed_table,
):
    """The write still succeeds — only the indexed *read* degrades."""
    report, replaced = await _file_report()
    assert report.agent_id == AGENT and replaced is False

    assert await list_open_reports() == []
    assert await count_open_reports() == 0

    # …and the report is genuinely stored, so it joins the queue once GSI6 exists.
    stored = unindexed_table.get_item(
        Key={"PK": f"AST#{AGENT}", "SK": f"REPORT#{report.report_id}"}
    ).get("Item")
    assert stored is not None and stored["state"] == "open"


@pytest.mark.asyncio
async def test_report_queue_logs_the_index_name_when_it_degrades(unindexed_table, caplog):
    with caplog.at_level("WARNING"):
        await list_open_reports()
        await count_open_reports()

    assert caplog.text.count("AgentReportsIndex") == 2


@pytest.mark.asyncio
async def test_report_queue_degrades_on_the_production_error_spelling(
    indexed_table, monkeypatch
):
    import apis.shared.assistants.reports as reports_module

    missing = _client_error(
        "ValidationException",
        "The table does not have the specified index: AgentReportsIndex",
    )

    class _Raises:
        def query(self, **_kwargs):
            raise missing

    monkeypatch.setattr(reports_module, "_table", lambda: _Raises())

    assert await list_open_reports() == []
    assert await count_open_reports() == 0


@pytest.mark.asyncio
async def test_report_queue_still_raises_on_an_unrelated_failure(indexed_table, monkeypatch):
    import apis.shared.assistants.reports as reports_module

    denied = _client_error(
        "AccessDeniedException", "User is not authorized to perform: dynamodb:Query"
    )

    class _Raises:
        def query(self, **_kwargs):
            raise denied

    monkeypatch.setattr(reports_module, "_table", lambda: _Raises())

    with pytest.raises(ClientError):
        await list_open_reports()
    with pytest.raises(ClientError):
        await count_open_reports()
