"""Tests for the admin OAuth create-provider rollback helpers.

Verifies retry + CloudWatch-metric behaviour of
`_rollback_orphaned_provider` and `_emit_orphan_metric`. These run
inside the admin `create_provider` route after a DB write fails; if
both the rollback AND the metric emit fail, the admin's original error
still propagates — these helpers must never raise.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from apis.app_api.admin.oauth import routes


def _client_error(code: str = "ThrottlingException") -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": code}},
        operation_name="DeleteOauth2CredentialProvider",
    )


@pytest.fixture(autouse=True)
def _reset_cw_client_cache():
    routes._cloudwatch_client.cache_clear()
    yield
    routes._cloudwatch_client.cache_clear()


@pytest.fixture
def fast_backoff(monkeypatch):
    """Rollback retries sleep between attempts; zero those out in tests."""
    monkeypatch.setattr(routes, "_ROLLBACK_RETRY_DELAYS_SECONDS", (0.0, 0.0))


class TestRollbackOrphanedProvider:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self, fast_backoff):
        registrar = MagicMock()
        registrar.delete_credential_provider.return_value = None

        await routes._rollback_orphaned_provider(registrar, "google")

        registrar.delete_credential_provider.assert_called_once_with("google")

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self, fast_backoff):
        registrar = MagicMock()
        registrar.delete_credential_provider.side_effect = [
            _client_error("ThrottlingException"),
            None,
        ]

        await routes._rollback_orphaned_provider(registrar, "google")

        assert registrar.delete_credential_provider.call_count == 2

    @pytest.mark.asyncio
    async def test_emits_metric_when_all_retries_exhausted(self, fast_backoff):
        registrar = MagicMock()
        registrar.delete_credential_provider.side_effect = _client_error(
            "InternalServerException"
        )

        with patch.object(routes, "_emit_orphan_metric") as emit:
            await routes._rollback_orphaned_provider(registrar, "google")

        # 1 initial + 2 retries = 3 attempts with zero backoff schedule.
        assert registrar.delete_credential_provider.call_count == 3
        emit.assert_called_once_with("google")

    @pytest.mark.asyncio
    async def test_never_raises_even_when_everything_fails(self, fast_backoff):
        """Rollback runs inside an `except` block that already re-raises
        the admin-facing error. A secondary raise here would shadow it.

        Simulates the worst case: every registrar call fails AND
        CloudWatch is down. `_emit_orphan_metric` swallows its own
        errors, so the overall helper must return cleanly.
        """
        registrar = MagicMock()
        registrar.delete_credential_provider.side_effect = RuntimeError("boom")

        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError("cw boom")

        with patch.object(routes, "_cloudwatch_client", lambda: fake_cw):
            # Must not raise — secondary failures only log.
            await routes._rollback_orphaned_provider(registrar, "google")


class TestEmitOrphanMetric:
    def test_puts_metric_with_provider_dimension(self):
        fake_cw = MagicMock()
        with patch.object(routes, "_cloudwatch_client", lambda: fake_cw):
            routes._emit_orphan_metric("google-workspace")

        fake_cw.put_metric_data.assert_called_once()
        call_kwargs = fake_cw.put_metric_data.call_args.kwargs
        assert call_kwargs["Namespace"] == "Agentcore/OAuth"
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "ProviderOrphaned"
        assert metric["Dimensions"] == [
            {"Name": "ProviderId", "Value": "google-workspace"}
        ]
        assert metric["Value"] == 1
        assert metric["Unit"] == "Count"

    def test_swallows_cloudwatch_failure(self):
        """CloudWatch outage must not shadow the admin's create failure."""
        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError("cw down")

        with patch.object(routes, "_cloudwatch_client", lambda: fake_cw):
            routes._emit_orphan_metric("google")  # no raise
