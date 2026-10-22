"""``GSI7_*`` (KbWorkIndex) is never written by the generic assistant update.

Managed-KB migration work is discovered through a sparse index whose keys are written
only while a knowledge base is actually eligible for background work
(.kiro/specs/managed-kb-migration, Requirements 15.13/15.14). The dispatcher that reads
it creates and deletes billed AWS resources, so a stray key is not a cosmetic bug: it
hands the dispatcher a knowledge base nobody asked to migrate.

``Assistant`` is ``extra="allow"`` and reads hydrate straight from the raw DynamoDB
item, so any attribute present on the row round-trips as an extra model field and the
generic update path would write it back. That is exactly how ``GSI5_*`` could
re-publish a taken-down agent before it was listed immutable — see
``test_stale_edit_racing_a_takedown_does_not_resurrect_the_directory_key``.

``GSI7_*`` differs from ``GSI5_*`` in one respect worth stating, because it changes what
this test is actually worth: the work keys live on a **separate** item
(``SK = KB#{app_kb_id}``), not on the ``METADATA`` row this path writes, so today the
generic update cannot reach them by the normal route. These tests therefore pin the
invariant rather than reproduce a live bug — they fail if someone removes ``GSI7_*``
from ``immutable_fields``, or if a future refactor moves work state onto the assistant
row without re-establishing the guard.
"""

import pytest


async def _create(owner_id: str = "u1"):
    from apis.shared.assistants.service import create_assistant

    return await create_assistant(
        owner_id=owner_id,
        owner_name="Alice",
        name="Bot",
        description="d",
        instructions="hi",
    )


def _item(table_name: str, assistant_id: str):
    import boto3

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)
    return table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})["Item"]


class TestKbWorkIndexGuard:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_a_stale_edit_cannot_resurrect_a_cleared_work_key(self, assistants_table):
        """The headline guard, exercised through the real mechanism and the real sequence.

        Two details make or break this test:

        1. The keys must be read back through ``get_assistant`` so they hydrate into
           ``__pydantic_extra__`` and reach ``model_dump``. Simulating that with
           ``object.__setattr__`` bypasses the extras dict, so the attribute never reaches
           the update payload and the test passes even with the guard removed.
        2. The keys must be **cleared from the row** before the stale write lands.
           ``immutable_fields`` prevents the generic update from *writing* an attribute;
           it does not delete one that is already there. Without the clear, the key is
           still on the item afterwards for the trivial reason that nothing removed it,
           and the assertion passes or fails for reasons unrelated to the guard.

        That is the true shape of the exposure: work finishes and the dispatcher clears
        the key, an author edit that began earlier still carries it, and the write lands.
        """
        import boto3

        from apis.shared.assistants.service import _update_assistant_cloud, get_assistant

        created = await _create()
        table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-assistants")
        key = {"PK": f"AST#{created.assistant_id}", "SK": "METADATA"}

        # 1. KB is enrolled: work keys present on the row.
        table.update_item(
            Key=key,
            UpdateExpression="SET GSI7_PK = :p, GSI7_SK = :s",
            ExpressionAttributeValues={":p": "KBWORK#shadow", ":s": "2026-08-17T00:00:00Z"},
        )

        # 2. An author's request reads it while still enrolled.
        stale = await get_assistant(created.assistant_id, "u1")
        assert stale.model_dump(by_alias=True).get("GSI7_PK") == "KBWORK#shadow", (
            "precondition: the key must hydrate as an extra field, or this test is vacuous"
        )

        # 3. Work completes and the dispatcher clears the key.
        table.update_item(Key=key, UpdateExpression="REMOVE GSI7_PK, GSI7_SK")
        assert "GSI7_PK" not in _item("test-assistants", created.assistant_id)

        # 4. The in-flight author edit lands, still carrying the stale key.
        stale.description = "An unrelated tweak"
        await _update_assistant_cloud(stale, "test-assistants")

        item = _item("test-assistants", created.assistant_id)
        assert item["description"] == "An unrelated tweak", "the legitimate edit must land"
        assert "GSI7_PK" not in item, "a stale edit re-enrolled a KB that had left the queue"
        assert "GSI7_SK" not in item, "a stale edit restored a work-discovery sort key"

    @pytest.mark.asyncio
    async def test_the_guard_lists_both_work_key_attributes(self):
        """Pins the guard itself, so removing one half of the pair fails loudly.

        Asserted against the source set rather than through behaviour because a missing
        *sort* key is invisible to a query-free test: DynamoDB accepts a partial GSI key
        by simply not indexing the item.
        """
        import inspect

        from apis.shared.assistants import service

        src = inspect.getsource(service._update_assistant_cloud)
        immutable = src.split("immutable_fields = {", 1)[1].split("}", 1)[0]
        assert '"GSI7_PK"' in immutable
        assert '"GSI7_SK"' in immutable

    @pytest.mark.asyncio
    async def test_a_normal_create_writes_no_work_key(self, assistants_table):
        """Absence of the key is the "not enrolled" state, so a fresh agent must have none.

        This is what makes the index sparse: enrolment is an explicit write, never a
        side effect of creating an assistant.
        """
        created = await _create()
        item = _item("test-assistants", created.assistant_id)
        assert "GSI7_PK" not in item
        assert "GSI7_SK" not in item
