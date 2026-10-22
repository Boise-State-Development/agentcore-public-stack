"""Process-cached boto3 clients (docs/specs/turn-latency-preamble.md PR-3).

The saving is small per call — ~1.5ms — and the risk is not. These tests pin
the two things that make it safe: the cache actually caches, and it can be
torn down so a client built under one `moto` backend never serves the next
test.
"""

import pytest

from apis.shared import aws_clients


@pytest.fixture(autouse=True)
def _clean_cache():
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


class TestCaching:
    def test_the_same_service_returns_the_identical_resource(self, aws):
        first = aws_clients.get_resource("dynamodb")
        second = aws_clients.get_resource("dynamodb")

        assert first is second, "the whole point is not rebuilding it"

    def test_clients_cache_independently_of_resources(self, aws):
        resource = aws_clients.get_resource("dynamodb")
        client = aws_clients.get_client("dynamodb")

        assert client is not resource

    def test_region_is_part_of_the_key(self, aws):
        """Two regions are two endpoints; collapsing them would silently send
        writes to the wrong one."""
        default = aws_clients.get_resource("dynamodb")
        west = aws_clients.get_resource("dynamodb", region_name="us-west-2")
        east = aws_clients.get_resource("dynamodb", region_name="us-east-1")

        assert west is not east
        assert default is not west or default is not east

    def test_a_table_handle_is_bound_to_the_cached_resource(self, aws):
        """The Table is deliberately not cached — the resource behind it is."""
        a = aws_clients.get_dynamodb_table("some-table")
        b = aws_clients.get_dynamodb_table("some-table")

        assert a is not b
        assert a.meta.client is b.meta.client


class TestResetProtectsMoto:
    """`mock_aws()` is entered per test. A cached client outliving its mock
    talks to a torn-down backend — or a real AWS endpoint — and the failure is
    order-dependent, which is the worst kind to debug."""

    def test_reset_drops_everything(self, aws):
        before = aws_clients.get_resource("dynamodb")

        aws_clients.reset_cached_clients()

        assert aws_clients.get_resource("dynamodb") is not before

    def test_the_aws_fixture_leaves_no_client_behind(self):
        """Outside any `aws` fixture the cache must be empty, which is what
        stops the previous test's mock backend leaking into the next one."""
        assert aws_clients._resources == {}
        assert aws_clients._clients == {}


class TestSharedSession:
    """One process-wide `boto3.Session` for the SDKs that build their own
    clients (docs/specs/turn-path-ttft.md §5 P2)."""

    def test_the_same_session_every_time(self):
        assert aws_clients.shared_boto_session() is aws_clients.shared_boto_session()

    def test_reset_drops_the_shared_session_too(self):
        """A session built under one moto backend is as stale as a client
        built under it."""
        before = aws_clients.shared_boto_session()

        aws_clients.reset_cached_clients()

        assert aws_clients._shared_session is None
        assert aws_clients.shared_boto_session() is not before

    def test_one_client_per_service_region_and_config(self):
        session = aws_clients.ClientReusingSession()

        first = session.client("sts", region_name="us-west-2")
        again = session.client(service_name="sts", region_name="us-west-2")
        other_region = session.client("sts", region_name="us-east-1")

        assert first is again
        assert first is not other_region
        assert first.meta.config.max_pool_connections == aws_clients.SHARED_SESSION_MAX_POOL_CONNECTIONS

    def test_a_none_keyword_is_not_an_override(self):
        """Strands' BedrockModel passes `endpoint_url=None`; that must still
        land on the shared client, or the arm silently builds a fresh one."""
        session = aws_clients.ClientReusingSession()

        plain = session.client("sts", region_name="us-west-2")
        with_none = session.client("sts", region_name="us-west-2", endpoint_url=None)

        assert with_none is plain

    def test_a_real_override_is_never_shared(self):
        session = aws_clients.ClientReusingSession()

        plain = session.client("sts", region_name="us-west-2")
        custom = session.client("sts", region_name="us-west-2", endpoint_url="https://sts.example")

        assert custom is not plain
        assert session.client("sts", region_name="us-west-2") is plain


class TestMetadataUsesIt:
    @pytest.mark.asyncio
    async def test_session_reads_go_through_one_cached_resource(
        self, sessions_metadata_table
    ):
        """The preamble's point: six helpers, one client build between them."""
        from apis.shared.sessions.metadata import load_session_meta

        aws_clients.reset_cached_clients()

        await load_session_meta("s1", "u1")
        after_first = aws_clients.get_resource("dynamodb")
        await load_session_meta("s2", "u1")

        assert aws_clients.get_resource("dynamodb") is after_first
