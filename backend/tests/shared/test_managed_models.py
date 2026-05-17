"""Task 9: Managed models CRUD tests (moto DynamoDB)."""

import boto3
import pytest


def _make_model_data(model_id="claude-3", **kw):
    from apis.shared.models.models import ManagedModelCreate
    defaults = dict(
        modelId=model_id, modelName="Claude 3", provider="bedrock",
        providerName="Amazon Bedrock", inputModalities=["text"],
        outputModalities=["text"], maxInputTokens=100000, maxOutputTokens=4096,
        inputPricePerMillionTokens=3.0, outputPricePerMillionTokens=15.0,
    )
    defaults.update(kw)
    return ManagedModelCreate(**defaults)


class TestManagedModels:
    @pytest.fixture(autouse=True)
    def _patch_dynamodb(self, managed_models_table, monkeypatch):
        """Re-create the module-level dynamodb resource inside the active mock_aws context."""
        import apis.shared.models.managed_models as mm
        monkeypatch.setattr(mm, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))

    @pytest.mark.asyncio
    async def test_create_and_get(self):
        from apis.shared.models.managed_models import create_managed_model, get_managed_model
        data = _make_model_data()
        model = await create_managed_model(data)
        assert model.model_id == "claude-3"
        result = await get_managed_model(model.id)
        assert result is not None
        assert result.model_name == "Claude 3"

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self):
        from apis.shared.models.managed_models import create_managed_model
        await create_managed_model(_make_model_data())
        with pytest.raises(Exception):
            await create_managed_model(_make_model_data())

    @pytest.mark.asyncio
    async def test_list_all(self):
        from apis.shared.models.managed_models import create_managed_model, list_all_managed_models
        await create_managed_model(_make_model_data("m1"))
        await create_managed_model(_make_model_data("m2"))
        models = await list_all_managed_models()
        assert len(models) == 2

    @pytest.mark.asyncio
    async def test_update(self):
        from apis.shared.models.managed_models import create_managed_model, update_managed_model
        from apis.shared.models.models import ManagedModelUpdate
        model = await create_managed_model(_make_model_data())
        updates = ManagedModelUpdate(modelName="Claude 3.5")
        updated = await update_managed_model(model.id, updates)
        assert updated is not None
        assert updated.model_name == "Claude 3.5"

    @pytest.mark.asyncio
    async def test_delete(self):
        from apis.shared.models.managed_models import create_managed_model, delete_managed_model, get_managed_model
        model = await create_managed_model(_make_model_data())
        deleted = await delete_managed_model(model.id)
        assert deleted is True
        assert await get_managed_model(model.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        from apis.shared.models.managed_models import delete_managed_model
        assert await delete_managed_model("nope") is False

    @pytest.mark.asyncio
    async def test_default_model_switching(self):
        from apis.shared.models.managed_models import create_managed_model, get_managed_model
        m1 = await create_managed_model(_make_model_data("m1", isDefault=True))
        assert m1.is_default is True
        m2 = await create_managed_model(_make_model_data("m2", isDefault=True))
        assert m2.is_default is True
        m1_refreshed = await get_managed_model(m1.id)
        assert m1_refreshed.is_default is False

    @pytest.mark.asyncio
    async def test_supports_caching_default_bedrock(self):
        from apis.shared.models.managed_models import create_managed_model
        model = await create_managed_model(_make_model_data(provider="bedrock"))
        assert model.supports_caching is True

    @pytest.mark.asyncio
    async def test_supports_caching_default_openai(self):
        from apis.shared.models.managed_models import create_managed_model
        model = await create_managed_model(_make_model_data("gpt4", provider="openai"))
        assert model.supports_caching is False


class TestMaxTokensCeiling:
    """max_tokens spec must not exceed the model's declared output ceiling."""

    def test_default_above_ceiling_rejected(self):
        # Default 8192 is within the (absent) row bounds but exceeds the
        # model's 4096 ceiling — only the cross-field rule should fire.
        with pytest.raises(Exception):
            _make_model_data(
                maxOutputTokens=4096,
                supportedParams={"params": {"max_tokens": {"supported": True, "default": 8192}}},
            )

    def test_max_above_ceiling_rejected(self):
        with pytest.raises(Exception):
            _make_model_data(
                maxOutputTokens=4096,
                supportedParams={"params": {"max_tokens": {"supported": True, "max": 8192}}},
            )

    def test_within_ceiling_ok(self):
        m = _make_model_data(
            maxOutputTokens=8192,
            supportedParams={"params": {"max_tokens": {"supported": True, "max": 8192, "default": 8192}}},
        )
        assert m.max_output_tokens == 8192

    def test_unsupported_row_not_ceiling_checked(self):
        m = _make_model_data(
            maxOutputTokens=4096,
            supportedParams={"params": {"max_tokens": {"supported": False, "max": 999999, "default": 999999}}},
        )
        assert m.max_output_tokens == 4096

    def test_update_payload_enforced(self):
        from apis.shared.models.models import ManagedModelUpdate
        with pytest.raises(Exception):
            ManagedModelUpdate(
                maxOutputTokens=4096,
                supportedParams={"params": {"max_tokens": {"supported": True, "default": 8192}}},
            )
