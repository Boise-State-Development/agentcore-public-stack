"""strands-agents 1.63.0 reads two things from its bundled ``model_info.json``.

Both change a request without raising, so both are pinned here against the
real ``BedrockModel`` on our production config path:

1. **maxTokens** (#5645). With ``max_tokens`` absent from the config, Strands
   now sends the model's output limit from its data. That data gives Claude
   Sonnet 4.6 128,000 where the Bedrock model card says 64K, so we opt out
   with an explicit ``None`` and leave the ceiling to the catalog, or to
   Bedrock's documented omitted default (the model's maximum).
2. **Cache strategy.** ``"auto"`` now reads ``prompt_caching`` from the data
   instead of a claude/anthropic name test. Our hand-placed system and tools
   points read ``bedrock_cache_points_supported()``, which is still a name
   test, so the two must agree on every id we run or the message point and
   the static points disagree about one request.
"""

import pytest

from agents.main_agent.core.model_config import ModelConfig

# The curated Converse ids (us.* and the global.* ids prod uses), Haiku 5.5
# ahead of its catalog row, and the side-channel Nova ids.
CACHE_CAPABLE_IDS = [
    "us.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
    "us.anthropic.claude-opus-4-7",
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-sonnet-5",
    "us.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "us.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-haiku-5-5",
]
NO_CACHE_POINT_IDS = [
    "amazon.nova-micro-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    "qwen.qwen3-coder-30b-a3b-instruct",
]


def _bedrock_model(config: ModelConfig):
    from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

    return CountTokensBedrockModel(native_projection=False, **config.to_bedrock_config())


def _format(model) -> dict:
    return model.format_request(
        [{"role": "user", "content": [{"text": "hello"}]}],
        None,
        system_prompt_content=[{"text": "You are a helpful assistant."}],
    )


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")


class TestMaxTokensDefault:
    @pytest.mark.parametrize("model_id", CACHE_CAPABLE_IDS + NO_CACHE_POINT_IDS)
    def test_unset_max_tokens_is_omitted_not_filled_from_model_info(self, model_id):
        request = _format(_bedrock_model(ModelConfig(model_id=model_id)))
        assert "maxTokens" not in request.get("inferenceConfig", {})

    def test_catalog_max_tokens_still_reaches_the_request(self):
        config = ModelConfig(model_id="us.anthropic.claude-sonnet-4-6", inference_params={"max_tokens": 8192})
        assert _format(_bedrock_model(config))["inferenceConfig"]["maxTokens"] == 8192

    @pytest.mark.parametrize("strict", [True, False])
    def test_extraction_model_omits_max_tokens(self, strict):
        from apis.shared.extraction.extractor import _build_model

        model = _build_model("us.anthropic.claude-sonnet-4-6", strict=strict)
        assert model.get_config()["max_tokens"] is None


class TestResolvedConfigMemo:
    """CountTokensBedrockModel resolves get_config/model_info once per config change."""

    def test_repeated_reads_reuse_one_resolution(self):
        model = _bedrock_model(ModelConfig(model_id="us.anthropic.claude-sonnet-4-6"))
        assert model.get_config() is model.get_config()
        assert model.model_info is model.model_info

    def test_update_config_drops_the_memo(self):
        model = _bedrock_model(ModelConfig(model_id="us.anthropic.claude-sonnet-4-6", caching_enabled=True))
        assert model._cache_strategy == "anthropic"
        assert model.get_config()["max_tokens"] is None

        model.update_config(model_id="us.amazon.nova-micro-v1:0", max_tokens=512)

        assert model._cache_strategy is None
        assert model.get_config()["model_id"] == "us.amazon.nova-micro-v1:0"
        assert model.get_config()["max_tokens"] == 512
        assert model.model_info.limits.context_window == 128_000


class TestCacheStrategyAgreesWithOurPredicate:
    @pytest.mark.parametrize("model_id", CACHE_CAPABLE_IDS)
    def test_claude_ids_cache_on_both_sides(self, model_id):
        config = ModelConfig(model_id=model_id, caching_enabled=True)
        assert config.bedrock_cache_points_supported() is True
        assert _bedrock_model(config)._cache_strategy == "anthropic"

    @pytest.mark.parametrize("model_id", NO_CACHE_POINT_IDS)
    def test_other_ids_cache_on_neither_side(self, model_id):
        config = ModelConfig(model_id=model_id, caching_enabled=True)
        assert config.bedrock_cache_points_supported() is False
        assert _bedrock_model(config)._cache_strategy is None
