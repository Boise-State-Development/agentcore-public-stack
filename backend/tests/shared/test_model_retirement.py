"""Model retirement — docs/specs/model-retirement.md §7.

The lifecycle fields round-trip through storage, the resolver turns a retired
model into its successor (or a denial), and the admin write rules keep a
redirect pointing at something that can run.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import boto3
import pytest

from apis.shared.models.models import (
    ManagedModel,
    ManagedModelCreate,
    ManagedModelUpdate,
    ModelStatus,
)
from apis.shared.models import retirement
from apis.shared.models.retirement import (
    format_retires_on,
    resolve_effective_model,
    resolve_from_catalog,
    retired_model_message,
    validate_lifecycle,
)

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _row(model_id: str, *, status="active", replaced_by=None, provider="bedrock", **kw) -> ManagedModel:
    return ManagedModel(
        id=f"uuid-{model_id}",
        modelId=model_id,
        modelName=kw.pop("name", model_id.title()),
        provider=provider,
        providerName="Amazon Bedrock",
        inputModalities=["text"],
        outputModalities=["text"],
        maxInputTokens=200000,
        enabled=kw.pop("enabled", True),
        inputPricePerMillionTokens=3.0,
        outputPricePerMillionTokens=15.0,
        status=status,
        replacedBy=replaced_by,
        createdAt=NOW,
        updatedAt=NOW,
        **kw,
    )


def _create(model_id="claude-3", **kw) -> ManagedModelCreate:
    defaults = dict(
        modelId=model_id, modelName="Claude 3", provider="bedrock",
        providerName="Amazon Bedrock", inputModalities=["text"],
        outputModalities=["text"], maxInputTokens=100000, maxOutputTokens=4096,
        inputPricePerMillionTokens=3.0, outputPricePerMillionTokens=15.0,
    )
    defaults.update(kw)
    return ManagedModelCreate(**defaults)


class TestResolveFromCatalog:
    def test_unknown_id_runs_as_requested(self):
        effective = resolve_from_catalog("custom.model", [])
        assert effective.model_id == "custom.model"
        assert not effective.redirected and not effective.denied
        assert effective.record is None

    @pytest.mark.parametrize("status", ["active", "deprecated"])
    def test_non_retired_runs_as_requested(self, status):
        # Deprecation is a picker concern — the runtime never sees it.
        effective = resolve_from_catalog("old", [_row("old", status=status, replaced_by="new"), _row("new")])
        assert effective.model_id == "old"
        assert not effective.redirected

    def test_retired_redirects_to_successor_with_its_provider(self):
        catalog = [
            _row("old", status="retired", replaced_by="new", provider="bedrock"),
            _row("new", provider="bedrock-responses"),
        ]
        effective = resolve_from_catalog("old", catalog)
        assert effective.redirected
        assert effective.model_id == "new"
        # The caller's provider described the retired model; the successor's wins.
        assert effective.provider == "bedrock-responses"
        assert effective.retired.model_id == "old"

    def test_follows_a_chain_of_retired_rows(self):
        catalog = [
            _row("a", status="retired", replaced_by="b"),
            _row("b", status="retired", replaced_by="c"),
            _row("c"),
        ]
        effective = resolve_from_catalog("a", catalog)
        assert effective.model_id == "c"
        assert effective.retired.model_id == "a"

    def test_retired_without_successor_is_denied(self):
        effective = resolve_from_catalog("old", [_row("old", status="retired")])
        assert effective.denied
        assert effective.model_id == "old"
        assert effective.retired.model_id == "old"

    def test_chain_ending_in_a_dead_end_is_denied(self):
        catalog = [_row("a", status="retired", replaced_by="b"), _row("b", status="retired")]
        effective = resolve_from_catalog("a", catalog)
        assert effective.denied
        # The message names the model the caller asked for.
        assert effective.retired.model_id == "a"

    def test_cycle_is_denied_not_looped(self):
        catalog = [
            _row("a", status="retired", replaced_by="b"),
            _row("b", status="retired", replaced_by="a"),
        ]
        assert resolve_from_catalog("a", catalog).denied

    def test_successor_without_a_row_still_runs(self):
        # Validated on write, so a missing row is a later deletion; it behaves
        # like any other id with no row rather than failing the turn here.
        effective = resolve_from_catalog("old", [_row("old", status="retired", replaced_by="gone")])
        assert effective.model_id == "gone"
        assert effective.record is None
        assert effective.provider is None


class TestResolveEffectiveModel:
    @pytest.mark.asyncio
    async def test_none_passes_through(self):
        assert await resolve_effective_model(None) is None
        assert await resolve_effective_model("") is None

    @pytest.mark.asyncio
    async def test_catalog_failure_fails_open(self, monkeypatch):
        monkeypatch.setattr(
            retirement, "list_all_managed_models", AsyncMock(side_effect=RuntimeError("no table"))
        )
        effective = await resolve_effective_model("old")
        assert effective.model_id == "old"
        assert not effective.redirected and not effective.denied

    @pytest.mark.asyncio
    async def test_reads_the_catalog(self, monkeypatch):
        monkeypatch.setattr(
            retirement,
            "list_all_managed_models",
            AsyncMock(return_value=[_row("old", status="retired", replaced_by="new"), _row("new")]),
        )
        assert (await resolve_effective_model("old")).model_id == "new"


class TestValidateLifecycle:
    def test_active_default_ok(self):
        validate_lifecycle(model_id="m", status=ModelStatus.ACTIVE, replaced_by=None, is_default=True, catalog=[])

    @pytest.mark.parametrize("status", [ModelStatus.DEPRECATED, ModelStatus.RETIRED])
    def test_non_active_default_rejected(self, status):
        with pytest.raises(ValueError, match="cannot be the default"):
            validate_lifecycle(model_id="m", status=status, replaced_by=None, is_default=True, catalog=[])

    def test_self_replacement_rejected(self):
        with pytest.raises(ValueError, match="itself"):
            validate_lifecycle(
                model_id="m", status=ModelStatus.RETIRED, replaced_by="m", is_default=False, catalog=[_row("m")]
            )

    def test_unknown_replacement_rejected(self):
        with pytest.raises(ValueError, match="not in the model catalog"):
            validate_lifecycle(
                model_id="m", status=ModelStatus.RETIRED, replaced_by="nope", is_default=False, catalog=[]
            )

    def test_non_active_replacement_rejected(self):
        with pytest.raises(ValueError, match="must be active"):
            validate_lifecycle(
                model_id="m",
                status=ModelStatus.RETIRED,
                replaced_by="n",
                is_default=False,
                catalog=[_row("n", status="deprecated")],
            )

    def test_disabled_replacement_rejected(self):
        with pytest.raises(ValueError, match="enabled"):
            validate_lifecycle(
                model_id="m",
                status=ModelStatus.RETIRED,
                replaced_by="n",
                is_default=False,
                catalog=[_row("n", enabled=False)],
            )

    def test_active_replacement_ok(self):
        validate_lifecycle(
            model_id="m", status=ModelStatus.DEPRECATED, replaced_by="n", is_default=False, catalog=[_row("n")]
        )


class TestMessages:
    def test_format_retires_on_does_not_shift_the_day(self):
        assert format_retires_on("2026-10-31") == "October 31, 2026"
        assert format_retires_on("2026-10-01") == "October 1, 2026"
        assert format_retires_on(None) is None
        assert format_retires_on("soon") is None

    def test_plain_chat_message_appends_the_note(self):
        msg = retired_model_message(_row("old", name="Claude Old", status="retired", retirementNote="Use Sonnet 5."))
        assert msg.startswith("**Claude Old** has been retired")
        assert msg.endswith("Use Sonnet 5.")

    def test_agent_message_points_at_the_owner(self):
        msg = retired_model_message(_row("old", name="Claude Old", status="retired"), agent=True)
        assert "This agent runs on **Claude Old**" in msg
        assert "owner" in msg


class TestFieldContract:
    def test_absent_status_reads_active(self):
        row = _row("m")
        assert ManagedModel.model_validate({**row.model_dump(by_alias=True), "status": None}).status == ModelStatus.ACTIVE

    def test_unknown_status_reads_active_rather_than_failing(self):
        # A row that fails to parse drops out of the catalog, and a model with
        # no row runs unmetered — so an unknown value must never raise.
        row = _row("m")
        assert ManagedModel.model_validate({**row.model_dump(by_alias=True), "status": "sunset"}).status == ModelStatus.ACTIVE

    def test_create_blank_strings_are_unset(self):
        data = _create(retiresOn=" ", retirementNote="", replacedBy="  ")
        assert data.retires_on is None and data.retirement_note is None and data.replaced_by is None

    def test_update_empty_string_survives_as_the_clear_sentinel(self):
        # On an update None means "leave it alone"; '' is the only way to clear.
        dumped = ManagedModelUpdate(retiresOn="", retirementNote="  ").model_dump(exclude_none=True, by_alias=True)
        assert dumped == {"retiresOn": "", "retirementNote": ""}

    @pytest.mark.parametrize("value", ["soon", "9/30/26", "2026-13-45"])
    def test_retires_on_must_be_an_iso_date(self, value):
        with pytest.raises(ValueError):
            ManagedModelUpdate(retiresOn=value)

    def test_note_is_capped_on_write(self):
        with pytest.raises(ValueError):
            _create(retirementNote="x" * 301)


class TestStorage:
    @pytest.fixture(autouse=True)
    def _patch_dynamodb(self, managed_models_table, monkeypatch):
        import apis.shared.models.managed_models as mm

        monkeypatch.setattr(mm, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))

    @pytest.mark.asyncio
    async def test_lifecycle_round_trips_and_clears(self):
        from apis.shared.models.managed_models import create_managed_model, get_managed_model, update_managed_model

        await create_managed_model(_create("new"))
        model = await create_managed_model(_create("old"))
        assert model.status == ModelStatus.ACTIVE

        await update_managed_model(
            model.id,
            ManagedModelUpdate(
                status="retired", replacedBy="new", retiresOn="2026-10-31", retirementNote="Moved to New."
            ),
        )
        stored = await get_managed_model(model.id)
        assert stored.status == ModelStatus.RETIRED
        assert (stored.replaced_by, stored.retires_on, stored.retirement_note) == (
            "new", "2026-10-31", "Moved to New."
        )

        await update_managed_model(model.id, ManagedModelUpdate(replacedBy="", retiresOn="", retirementNote=""))
        cleared = await get_managed_model(model.id)
        assert (cleared.replaced_by, cleared.retires_on, cleared.retirement_note) == (None, None, None)
        # Cleared means removed, not stored as '' — the row looks like one that never had it.
        raw = boto3.resource("dynamodb", region_name="us-east-1").Table("test-managed-models").get_item(
            Key={"PK": f"MODEL#{model.id}", "SK": f"MODEL#{model.id}"}
        )["Item"]
        assert "replacedBy" not in raw and "retiresOn" not in raw and "retirementNote" not in raw
        assert raw["status"] == "retired"

    @pytest.mark.asyncio
    async def test_resolver_reads_the_live_catalog(self):
        from apis.shared.models.managed_models import create_managed_model

        await create_managed_model(_create("new", provider="bedrock-responses"))
        await create_managed_model(_create("old", status="retired", replacedBy="new"))
        effective = await resolve_effective_model("old")
        assert effective.model_id == "new"
        assert effective.provider == "bedrock-responses"
