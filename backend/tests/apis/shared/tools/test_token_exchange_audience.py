"""
Normalisation of `token_exchange_audience`.

Regression test for a real failure. A pasted audience reached DynamoDB with a
leading space:

    ' cc5aa8a0-90f7-427a-bf9d-60678a980215'   (37 chars, not 36)

The token service compares the requested audience against its per-client
allowlist with an ordinal comparison, so the space made every exchange fail with
"audience is not permitted for this client".

What made it dangerous rather than merely broken: the tool still appeared to
work. The endpoint it called allowed anonymous access, so the request went
through unauthenticated and returned plausible data. A silent downgrade from
delegated user identity to anonymous is the worst failure mode this feature can
have — nothing in the user-visible output indicates the user's identity was
dropped.
"""

import pytest

from apis.shared.tools.models import ToolDefinition, ToolProtocol


def _tool(**kwargs) -> ToolDefinition:
    return ToolDefinition(
        tool_id="t",
        display_name="T",
        description="d",
        category="utility",
        protocol=ToolProtocol.MCP_EXTERNAL,
        **kwargs,
    )


class TestTokenExchangeAudienceNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            " cc5aa8a0-90f7-427a-bf9d-60678a980215",
            "cc5aa8a0-90f7-427a-bf9d-60678a980215 ",
            "  cc5aa8a0-90f7-427a-bf9d-60678a980215  ",
            "\tcc5aa8a0-90f7-427a-bf9d-60678a980215\n",
        ],
    )
    def test_surrounding_whitespace_is_stripped(self, raw: str) -> None:
        assert (
            _tool(token_exchange_audience=raw).token_exchange_audience
            == "cc5aa8a0-90f7-427a-bf9d-60678a980215"
        )

    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n"])
    def test_blank_becomes_none_not_empty_string(self, raw: str) -> None:
        # An empty string must not read as "exchange configured": that would send
        # an empty audience and fail at the token service instead of simply
        # leaving the feature off.
        assert _tool(token_exchange_audience=raw).token_exchange_audience is None

    def test_absent_stays_none(self) -> None:
        assert _tool().token_exchange_audience is None

    def test_valid_value_is_untouched(self) -> None:
        aud = "cc5aa8a0-90f7-427a-bf9d-60678a980215"
        assert _tool(token_exchange_audience=aud).token_exchange_audience == aud

    def test_round_trips_through_dynamodb_shape(self) -> None:
        # The stored form is what the runtime reads back, so normalisation has to
        # survive persistence rather than only existing in memory.
        tool = _tool(token_exchange_audience="  spaced-guid  ")
        item = tool.to_dynamo_item()
        assert item["tokenExchangeAudience"] == "spaced-guid"
        assert (
            ToolDefinition.from_dynamo_item(item).token_exchange_audience
            == "spaced-guid"
        )
