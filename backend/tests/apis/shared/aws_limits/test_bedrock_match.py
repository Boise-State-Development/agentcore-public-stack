"""The managed-model to Bedrock quota matcher (GitHub issue #12)."""

import pytest

from apis.shared.aws_limits.bedrock_match import (
    ModelRef,
    filter_quotas_for_models,
    is_inference_quota,
    model_id_tokens,
    model_matches_quota,
    tokenize,
)

SONNET_45 = ModelRef(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0", model_name="Claude Sonnet 4.5")
SONNET_4 = ModelRef(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0", model_name="Claude Sonnet 4")
SONNET_46_GLOBAL = ModelRef(model_id="global.anthropic.claude-sonnet-4-6", model_name="Claude Sonnet 4.6")
HAIKU_45 = ModelRef(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", model_name="Claude Haiku 4.5")
NOVA_PRO = ModelRef(model_id="amazon.nova-pro-v1:0", model_name="Nova Pro")
LLAMA_31_70B = ModelRef(model_id="meta.llama3-1-70b-instruct-v1:0", model_name="Llama 3.1 70B Instruct")
GPT_54 = ModelRef(model_id="openai.gpt-5.4", model_name="GPT-5.4", provider="mantle")


def test_tokenize_splits_letters_from_digits():
    assert tokenize("Llama3.1 70B") == ["llama", "3", "1", "70", "b"]
    assert tokenize("Claude Sonnet 4.5 V1 1M Context Length") == [
        "claude",
        "sonnet",
        "4",
        "5",
        "v",
        "1",
        "1",
        "m",
        "context",
        "length",
    ]


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", ["claude", "sonnet", "4", "5"]),
        ("global.anthropic.claude-sonnet-4-6", ["claude", "sonnet", "4", "6"]),
        ("anthropic.claude-3-haiku-20240307-v1:0", ["claude", "3", "haiku"]),
        ("amazon.nova-pro-v1:0", ["nova", "pro"]),
        ("meta.llama3-1-70b-instruct-v1:0", ["llama", "3", "1", "70", "b", "instruct"]),
        ("openai.gpt-5.4", ["gpt", "5", "4"]),
        (
            "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
            ["claude", "haiku", "4", "5"],
        ),
    ],
)
def test_model_id_tokens_strip_region_vendor_and_version(model_id, expected):
    assert model_id_tokens(model_id) == expected


@pytest.mark.parametrize(
    "name",
    [
        "On-demand model inference requests per minute for Anthropic Claude Sonnet 4.5",
        "On-demand model inference tokens per minute for Amazon Nova Pro",
        "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5",
        "Global cross-region model inference requests per minute for Anthropic Claude Sonnet 4.6",
        "[bedrock-mantle endpoint] Output tokens per minute for GPT-5.4",
        "[bedrock-mantle endpoint] Input tokens per minute for GPT-5.4",
    ],
)
def test_inference_quotas_are_recognised(name):
    assert is_inference_quota(name)


@pytest.mark.parametrize(
    "name",
    [
        "Batch inference job size (in GB) for Claude Sonnet 4.5",
        "Model invocation max tokens per day for Anthropic Claude Sonnet 4.5 V1 1M Context Length (doubled for cross-region calls)",
        "Global cross-region model inference tokens per day for Anthropic Claude Sonnet 4.6",
        "(Knowledge Bases) RetrieveAndGenerate requests per second",
        "Minimum number of records per batch inference job for Claude Sonnet 4.6",
    ],
)
def test_other_bedrock_quotas_are_not_inference_quotas(name):
    assert not is_inference_quota(name)


def test_matches_on_the_model_id_and_on_the_display_name():
    name = "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 4.5"
    assert model_matches_quota(SONNET_45, name)
    assert model_matches_quota(ModelRef(model_id="custom-alias", model_name="Claude Sonnet 4.5"), name)
    assert model_matches_quota(ModelRef(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"), name)


def test_a_major_version_does_not_claim_its_point_releases():
    four_five = "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 4.5"
    four = "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 4 V1"
    assert not model_matches_quota(SONNET_4, four_five)
    assert model_matches_quota(SONNET_4, four)
    assert not model_matches_quota(SONNET_45, four)


def test_a_context_length_variant_still_belongs_to_its_model():
    name = "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 4.5 V1 1M Context Length"
    assert model_matches_quota(SONNET_45, name)


def test_global_llama_nova_and_mantle_models():
    assert model_matches_quota(
        SONNET_46_GLOBAL,
        "Global cross-region model inference tokens per minute for Anthropic Claude Sonnet 4.6",
    )
    assert model_matches_quota(LLAMA_31_70B, "On-demand model inference tokens per minute for Meta Llama 3.1 70B Instruct")
    assert not model_matches_quota(LLAMA_31_70B, "On-demand model inference tokens per minute for Meta Llama 3.1 8B Instruct")
    assert model_matches_quota(NOVA_PRO, "On-demand model inference requests per minute for Amazon Nova Pro")
    assert not model_matches_quota(NOVA_PRO, "On-demand model inference requests per minute for Amazon Nova Premier")
    assert model_matches_quota(GPT_54, "[bedrock-mantle endpoint] Output tokens per minute for GPT-5.4")


def test_non_aws_providers_never_match():
    openai = ModelRef(model_id="gpt-5.4", model_name="GPT-5.4", provider="openai")
    assert not model_matches_quota(openai, "[bedrock-mantle endpoint] Output tokens per minute for GPT-5.4")


def test_one_generic_token_is_not_enough():
    pro = ModelRef(model_id="x", model_name="Pro")
    assert not model_matches_quota(pro, "On-demand model inference requests per minute for Amazon Nova Pro")


def test_filter_keeps_only_matching_inference_quotas():
    names = [
        "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5",
        "Cross-region model inference tokens per minute for Anthropic Claude Opus 4.1",
        "Batch inference job size (in GB) for Claude Haiku 4.5",
    ]
    assert filter_quotas_for_models(names, [HAIKU_45]) == [names[0]]
