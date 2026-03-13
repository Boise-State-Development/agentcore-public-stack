"""Unit tests for fine-tuning FastAPI dependencies."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException

from apis.app_api.fine_tuning.dependencies import require_fine_tuning_access


class TestRequireFineTuningAccess:

    @pytest.mark.asyncio
    async def test_returns_grant_for_whitelisted_user(self, make_user):
        user = make_user(email="allowed@example.com")
        repo = MagicMock()
        repo.check_and_reset_quota.return_value = {
            "email": "allowed@example.com",
            "granted_by": "admin@example.com",
            "granted_at": "2026-01-01T00:00:00Z",
            "monthly_quota_hours": 10.0,
            "current_month_usage_hours": 2.0,
            "quota_period": "2026-03",
        }

        result = await require_fine_tuning_access(user=user, repo=repo)

        assert result["email"] == "allowed@example.com"
        assert result["monthly_quota_hours"] == 10.0
        repo.check_and_reset_quota.assert_called_once_with("allowed@example.com")

    @pytest.mark.asyncio
    async def test_raises_403_for_non_whitelisted_user(self, make_user):
        user = make_user(email="denied@example.com")
        repo = MagicMock()
        repo.check_and_reset_quota.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await require_fine_tuning_access(user=user, repo=repo)

        assert exc_info.value.status_code == 403
        assert "do not have access" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_triggers_lazy_quota_reset(self, make_user):
        user = make_user(email="user@example.com")
        repo = MagicMock()
        repo.check_and_reset_quota.return_value = {
            "email": "user@example.com",
            "granted_by": "admin@example.com",
            "granted_at": "2026-01-01T00:00:00Z",
            "monthly_quota_hours": 10.0,
            "current_month_usage_hours": 0.0,
            "quota_period": "2026-03",
        }

        await require_fine_tuning_access(user=user, repo=repo)

        # Verify check_and_reset_quota was called (which includes lazy reset logic)
        repo.check_and_reset_quota.assert_called_once_with("user@example.com")
