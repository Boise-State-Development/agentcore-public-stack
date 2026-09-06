"""Integration tests for FineTuningAccessRepository using moto DynamoDB."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from apis.app_api.fine_tuning.repository import DEFAULT_QUOTA_USD


class TestGrantAccess:

    def test_grant_access_creates_item(self, repository):
        result = repository.grant_access("alice@example.com", "admin@example.com")

        assert result["email"] == "alice@example.com"
        assert result["granted_by"] == "admin@example.com"
        assert result["granted_at"] != ""
        assert result["monthly_quota_usd"] == DEFAULT_QUOTA_USD
        assert result["current_month_usage_usd"] == 0.0
        assert result["quota_period"] != ""

    def test_grant_access_normalizes_email_to_lowercase(self, repository):
        result = repository.grant_access("Alice@Example.COM", "admin@example.com")
        assert result["email"] == "alice@example.com"

    def test_grant_access_default_quota_is_the_dollar_default(self, repository):
        result = repository.grant_access("user@example.com", "admin@example.com")
        assert result["monthly_quota_usd"] == DEFAULT_QUOTA_USD

    def test_grant_access_custom_quota(self, repository):
        result = repository.grant_access("user@example.com", "admin@example.com", monthly_quota_usd=25.0)
        assert result["monthly_quota_usd"] == 25.0

    def test_grant_access_raises_on_duplicate(self, repository):
        repository.grant_access("dup@example.com", "admin@example.com")

        with pytest.raises(ValueError, match="Access already granted"):
            repository.grant_access("dup@example.com", "admin@example.com")


class TestGetAccess:

    def test_get_access_returns_none_for_nonexistent(self, repository):
        result = repository.get_access("nobody@example.com")
        assert result is None

    def test_get_access_returns_item_for_existing(self, repository):
        repository.grant_access("found@example.com", "admin@example.com")
        result = repository.get_access("found@example.com")
        assert result is not None
        assert result["email"] == "found@example.com"

    def test_get_access_is_case_insensitive(self, repository):
        repository.grant_access("CaseUser@Example.com", "admin@example.com")
        result = repository.get_access("CASEUSER@EXAMPLE.COM")
        assert result is not None
        assert result["email"] == "caseuser@example.com"


class TestListAccess:

    def test_list_access_empty_table(self, repository):
        result = repository.list_access()
        assert result == []

    def test_list_access_returns_all_grants(self, repository):
        repository.grant_access("a@example.com", "admin@example.com")
        repository.grant_access("b@example.com", "admin@example.com")
        repository.grant_access("c@example.com", "admin@example.com")

        result = repository.list_access()
        emails = {g["email"] for g in result}
        assert emails == {"a@example.com", "b@example.com", "c@example.com"}


class TestUpdateQuota:

    def test_update_quota_changes_monthly_dollars(self, repository):
        repository.grant_access("user@example.com", "admin@example.com", monthly_quota_usd=10.0)

        result = repository.update_quota("user@example.com", 50.0)
        assert result is not None
        assert result["monthly_quota_usd"] == 50.0

    def test_update_quota_returns_none_for_nonexistent(self, repository):
        result = repository.update_quota("nobody@example.com", 50.0)
        assert result is None


class TestRevokeAccess:

    def test_revoke_access_deletes_item(self, repository):
        repository.grant_access("gone@example.com", "admin@example.com")

        assert repository.revoke_access("gone@example.com") is True
        assert repository.get_access("gone@example.com") is None

    def test_revoke_access_returns_false_for_nonexistent(self, repository):
        assert repository.revoke_access("nobody@example.com") is False


class TestCheckAndResetQuota:

    def test_no_reset_when_same_period(self, repository):
        repository.grant_access("user@example.com", "admin@example.com")

        # Manually set usage to non-zero
        repository.increment_usage("user@example.com", 3.5)

        result = repository.check_and_reset_quota("user@example.com")
        assert result is not None
        assert result["current_month_usage_usd"] == 3.5

    def test_resets_usage_when_new_month(self, repository):
        repository.grant_access("user@example.com", "admin@example.com")
        repository.increment_usage("user@example.com", 5.0)

        # Patch _current_period to simulate a new month
        with patch.object(
            type(repository), "_current_period", staticmethod(lambda: "2099-12")
        ):
            result = repository.check_and_reset_quota("user@example.com")

        assert result is not None
        assert result["current_month_usage_usd"] == 0.0
        assert result["quota_period"] == "2099-12"

    def test_returns_none_for_nonexistent(self, repository):
        result = repository.check_and_reset_quota("nobody@example.com")
        assert result is None


class TestIncrementUsage:

    def test_increment_adds_dollars_atomically(self, repository):
        repository.grant_access("user@example.com", "admin@example.com")

        result = repository.increment_usage("user@example.com", 2.5)
        assert result is not None
        assert result["current_month_usage_usd"] == 2.5

    def test_increment_accumulates_across_calls(self, repository):
        repository.grant_access("user@example.com", "admin@example.com")

        repository.increment_usage("user@example.com", 1.0)
        repository.increment_usage("user@example.com", 2.5)
        result = repository.increment_usage("user@example.com", 0.5)

        assert result["current_month_usage_usd"] == pytest.approx(4.0)

    def test_increment_returns_none_for_nonexistent(self, repository):
        result = repository.increment_usage("nobody@example.com", 1.0)
        assert result is None


class TestLegacyQuotaMigration:
    """Grants written before the quota moved to dollars must keep working.

    The conversion is lazy: a record is read in the new shape and rewritten on
    the next check_and_reset_quota, so no environment needs a backfill run.
    """

    def _seed_legacy(self, repository, email, hours, used_hours, period):
        from decimal import Decimal

        repository._table.put_item(
            Item={
                "PK": f"EMAIL#{email}",
                "SK": "ACCESS",
                "email": email,
                "granted_by": "admin@example.com",
                "granted_at": "2026-01-01T00:00:00+00:00",
                "monthly_quota_hours": Decimal(str(hours)),
                "current_month_usage_hours": Decimal(str(used_hours)),
                "quota_period": period,
            }
        )

    def _period(self):
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def test_reads_a_legacy_grant_as_dollars(self, repository):
        from apis.app_api.fine_tuning.repository import LEGACY_HOURS_TO_USD

        self._seed_legacy(repository, "old@example.com", 10, 2.5, self._period())

        grant = repository.get_access("old@example.com")

        assert grant["monthly_quota_usd"] == round(10 * LEGACY_HOURS_TO_USD, 4)
        assert grant["current_month_usage_usd"] == round(2.5 * LEGACY_HOURS_TO_USD, 4)
        assert "monthly_quota_hours" not in grant

    def test_migration_preserves_spend_within_the_period(self, repository):
        """A user must not be able to zero their own spend by being migrated."""
        self._seed_legacy(repository, "old@example.com", 10, 2.5, self._period())

        grant = repository.check_and_reset_quota("old@example.com")

        assert grant["current_month_usage_usd"] > 0

    def test_migration_rewrites_the_stored_record(self, repository):
        self._seed_legacy(repository, "old@example.com", 10, 2.5, self._period())

        repository.check_and_reset_quota("old@example.com")

        item = repository._table.get_item(
            Key={"PK": "EMAIL#old@example.com", "SK": "ACCESS"}
        )["Item"]
        assert "monthly_quota_usd" in item
        assert "monthly_quota_hours" not in item
        assert "current_month_usage_hours" not in item

    def test_a_new_month_still_resets_a_legacy_grant(self, repository):
        self._seed_legacy(repository, "old@example.com", 10, 9.0, "2020-01")

        grant = repository.check_and_reset_quota("old@example.com")

        assert grant["current_month_usage_usd"] == 0.0
        assert grant["quota_period"] == self._period()

    def test_update_quota_clears_the_legacy_field(self, repository):
        self._seed_legacy(repository, "old@example.com", 10, 0, self._period())

        repository.update_quota("old@example.com", 42.0)

        item = repository._table.get_item(
            Key={"PK": "EMAIL#old@example.com", "SK": "ACCESS"}
        )["Item"]
        assert float(item["monthly_quota_usd"]) == 42.0
        assert "monthly_quota_hours" not in item
