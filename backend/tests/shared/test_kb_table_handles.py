"""The knowledge base search's DynamoDB handles are process-cached.

A fresh ``boto3.resource`` per read paid ~45–55ms for its first request in the
Runtime (a new connection pool) against ~4ms on a reused one, measured from the
botocore spans of a managed-KB follow-up turn on dev (2026-10-02). The search read
the KB_Record and the document statuses on four fresh resources, all on the turn's
critical path.
"""

from unittest.mock import MagicMock, patch

import pytest

from apis.shared.assistants import rag_service
from apis.shared.kb_backend import records


@pytest.fixture
def resource():
    dynamo = MagicMock()
    with patch("boto3.resource", return_value=dynamo) as factory, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "assistants"}
    ):
        yield factory, dynamo


class TestTheKbRecordTable:
    def test_reads_share_one_resource(self, resource):
        factory, dynamo = resource

        records._table()
        records._table()

        assert factory.call_count == 1
        dynamo.Table.assert_called_with("assistants")


class TestTheDocumentStatusTable:
    def test_lookups_share_one_resource(self, resource):
        factory, dynamo = resource

        rag_service._documents_table("assistants", "us-west-2")
        rag_service._documents_table("assistants", "us-west-2")

        assert factory.call_count == 1
        dynamo.Table.assert_called_with("assistants")

    def test_shares_the_kb_record_reads_resource(self, resource):
        """Same table, same region: the record read and the status lookups ride one
        connection pool, so the status lookup finds the record read's connection
        already open."""
        factory, _ = resource

        with patch.dict("os.environ", {"AWS_REGION": "us-west-2"}):
            records._table()
        rag_service._documents_table("assistants", "us-west-2")

        assert factory.call_count == 1
