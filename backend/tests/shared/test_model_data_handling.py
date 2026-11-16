"""Data handling on catalog rows (apis/shared/models/data_handling.py).

A covered model (Fable 5.x, Mythos 5.x) is served only to an account in the
`aws_review` data-retention mode, so its row is `provider_review` and users are
told before they pick it. These pin the floor, the notice and its digest, the
write rules, and the storage round trip.
"""

from datetime import datetime, timezone

import boto3
import pytest

from apis.shared.models.data_handling import (
    DEFAULT_PROVIDER_REVIEW_NOTICE,
    DataHandling,
    effective_notice,
    enable_needs_acknowledgement,
    notice_digest,
    requires_provider_review,
    resolve_data_handling,
    validate_data_handling,
)
from apis.shared.models.models import ManagedModel, ManagedModelCreate, ManagedModelUpdate

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
STD, REVIEW = DataHandling.STANDARD, DataHandling.PROVIDER_REVIEW


def _row(model_id: str, **kw) -> ManagedModel:
    return ManagedModel(
        id=f"uuid-{model_id}", modelId=model_id, modelName=model_id, provider="bedrock",
        providerName="Anthropic", inputModalities=["TEXT"], outputModalities=["TEXT"],
        maxInputTokens=1_000_000, enabled=kw.pop("enabled", True),
        inputPricePerMillionTokens=8.0, outputPricePerMillionTokens=40.0,
        createdAt=NOW, updatedAt=NOW, **kw,
    )


def _create(model_id: str, **kw) -> ManagedModelCreate:
    data = dict(
        modelId=model_id, modelName=model_id, provider="bedrock", providerName="Anthropic",
        inputModalities=["TEXT"], outputModalities=["TEXT"], maxInputTokens=1_000_000,
        maxOutputTokens=128_000, inputPricePerMillionTokens=8.0, outputPricePerMillionTokens=40.0,
    )
    data.update(kw)
    return ManagedModelCreate(**data)


class TestCoveredModels:
    @pytest.mark.parametrize("model_id", [
        "global.anthropic.claude-fable-5-2",
        "us.anthropic.claude-fable-5-2",
        "anthropic.claude-fable-5-1",
        "anthropic.claude-fable-5",
        "us.anthropic.claude-mythos-5-2",
        "anthropic.claude-mythos-5-1",
        "anthropic.claude-fable-6",
        "GLOBAL.ANTHROPIC.CLAUDE-FABLE-5-2",
    ])
    def test_covered(self, model_id):
        assert requires_provider_review(model_id)
        assert resolve_data_handling(model_id, STD) == REVIEW

    @pytest.mark.parametrize("model_id", [
        "us.anthropic.claude-opus-5-5",
        "us.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-haiku-5-5",
        "openai.gpt-oss-120b-1:0",
        "",
        None,
    ])
    def test_not_covered(self, model_id):
        assert not requires_provider_review(model_id)
        assert resolve_data_handling(model_id, None) == STD

    def test_an_admin_can_mark_any_model(self):
        assert resolve_data_handling("us.anthropic.claude-opus-5-5", REVIEW) == REVIEW


class TestNotice:
    def test_standard_has_no_notice(self):
        assert effective_notice(STD, "anything") is None
        assert notice_digest(None) is None

    def test_blank_falls_back_to_the_default(self):
        assert effective_notice(REVIEW, None) == DEFAULT_PROVIDER_REVIEW_NOTICE
        assert effective_notice(REVIEW, "   ") == DEFAULT_PROVIDER_REVIEW_NOTICE

    def test_the_digest_changes_with_the_wording(self):
        a, b = notice_digest("one"), notice_digest("two")
        assert a and b and a != b and notice_digest("one") == a

    def test_a_row_written_before_the_floor_reads_as_provider_review(self):
        row = _row("global.anthropic.claude-fable-5-2")
        assert row.data_handling == STD  # what was stored
        dumped = row.model_dump(by_alias=True)
        assert dumped["effectiveDataHandling"] == REVIEW
        assert dumped["dataHandlingNoticeText"] == DEFAULT_PROVIDER_REVIEW_NOTICE
        assert dumped["dataHandlingNoticeDigest"] == notice_digest(DEFAULT_PROVIDER_REVIEW_NOTICE)

    def test_a_standard_row_carries_no_notice(self):
        dumped = _row("us.anthropic.claude-opus-5-5").model_dump(by_alias=True)
        assert dumped["effectiveDataHandling"] == STD
        assert dumped["dataHandlingNoticeText"] is None and dumped["dataHandlingNoticeDigest"] is None

    def test_an_unknown_stored_value_reads_as_standard(self):
        assert _row("x", dataHandling="something-new").data_handling == STD

    def test_the_notice_is_capped_on_write(self):
        with pytest.raises(ValueError):
            _create("x", dataHandlingNotice="a" * 1_001)


class TestWriteRules:
    def test_provider_review_cannot_be_the_default(self):
        with pytest.raises(ValueError, match="cannot be the default"):
            validate_data_handling(model_id="f", data_handling=REVIEW, is_default=True, replaced_by=None, catalog=[])

    def test_no_model_retires_into_a_provider_review_one(self):
        catalog = [_row("global.anthropic.claude-fable-5-2")]
        with pytest.raises(ValueError, match="provider review"):
            validate_data_handling(
                model_id="old", data_handling=STD, is_default=False,
                replaced_by="global.anthropic.claude-fable-5-2", catalog=catalog,
            )

    def test_a_standard_successor_is_fine(self):
        validate_data_handling(
            model_id="old", data_handling=STD, is_default=False,
            replaced_by="us.anthropic.claude-opus-5-5", catalog=[_row("us.anthropic.claude-opus-5-5")],
        )

    @pytest.mark.parametrize("before_enabled, before, after_enabled, after, needed", [
        (False, REVIEW, True, REVIEW, True),    # enabling a covered model
        (True, STD, True, REVIEW, True),        # marking an enabled model
        (True, REVIEW, True, REVIEW, False),    # already reachable
        (True, REVIEW, False, REVIEW, False),   # disabling
        (False, STD, True, STD, False),         # an ordinary model
    ])
    def test_when_an_acknowledgement_is_needed(self, before_enabled, before, after_enabled, after, needed):
        assert enable_needs_acknowledgement(
            before_enabled=before_enabled, before_handling=before,
            after_enabled=after_enabled, after_handling=after,
        ) is needed


class TestStorage:
    @pytest.fixture(autouse=True)
    def _patch_dynamodb(self, managed_models_table, monkeypatch):
        import apis.shared.models.managed_models as mm

        monkeypatch.setattr(mm, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))

    def _raw(self, row_id):
        return boto3.resource("dynamodb", region_name="us-east-1").Table("test-managed-models").get_item(
            Key={"PK": f"MODEL#{row_id}", "SK": f"MODEL#{row_id}"}
        )["Item"]

    @pytest.mark.asyncio
    async def test_a_covered_model_is_stored_provider_review_whatever_was_sent(self):
        from apis.shared.models.managed_models import create_managed_model, get_managed_model, update_managed_model

        model = await create_managed_model(_create("global.anthropic.claude-fable-5-2", dataHandling="standard"))
        assert self._raw(model.id)["dataHandling"] == "provider_review"

        await update_managed_model(model.id, ManagedModelUpdate(dataHandling="standard"))
        assert (await get_managed_model(model.id)).data_handling == REVIEW

    @pytest.mark.asyncio
    async def test_the_notice_round_trips_and_clears(self):
        from apis.shared.models.managed_models import create_managed_model, get_managed_model, update_managed_model

        model = await create_managed_model(_create("us.anthropic.claude-opus-5-5"))
        assert self._raw(model.id)["dataHandling"] == "standard"

        await update_managed_model(
            model.id, ManagedModelUpdate(dataHandling="provider_review", dataHandlingNotice="Our wording.")
        )
        stored = await get_managed_model(model.id)
        assert (stored.data_handling, stored.data_handling_notice_text) == (REVIEW, "Our wording.")

        await update_managed_model(model.id, ManagedModelUpdate(dataHandlingNotice=""))
        cleared = await get_managed_model(model.id)
        assert cleared.data_handling_notice_text == DEFAULT_PROVIDER_REVIEW_NOTICE
        assert "dataHandlingNotice" not in self._raw(model.id)

    @pytest.mark.asyncio
    async def test_the_acknowledgement_flag_is_never_stored(self):
        from apis.shared.models.managed_models import create_managed_model, update_managed_model

        model = await create_managed_model(_create("x-model", acknowledgeDataHandling=True))
        await update_managed_model(model.id, ManagedModelUpdate(acknowledgeDataHandling=True, modelName="X"))
        assert "acknowledgeDataHandling" not in self._raw(model.id)
